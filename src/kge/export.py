"""Export the knowledge store as RDF (Turtle) using PROV-O for provenance.

Each assertion is reified as an ``rdf:Statement`` that is also a ``prov:Entity``, so its
type (fact / inference / hypothesis), confidence, validity period, sources, premises and
approval stay attached to it. Accepted facts and inferences are additionally asserted as
plain triples, so ordinary SPARQL over the graph sees only confirmed knowledge.
"""

from __future__ import annotations

import datetime as dt
import re

from kge.models import Assertion, EpistemicType, Status
from kge.store import Store

DEFAULT_BASE = "https://example.org/kge/"

_PREFIXES = """\
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
"""

_SAFE_LOCAL = re.compile(r"[^A-Za-z0-9_-]")


def _lit(value: str, lang: str | None = None) -> str:
    esc = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{esc}"@{lang}' if lang else f'"{esc}"'


def _typed(value: str, datatype: str) -> str:
    return f"{_lit(value)}^^{datatype}"


def _dt(value: dt.datetime) -> str:
    return _typed(value.isoformat(), "xsd:dateTime")


def _local(name: str) -> str:
    return _SAFE_LOCAL.sub("_", name)


class _Writer:
    def __init__(self) -> None:
        self.blocks: list[str] = []

    def node(self, subject: str, pairs: list[tuple[str, str]]) -> None:
        body = " ;\n    ".join(f"{p} {o}" for p, o in pairs)
        self.blocks.append(f"{subject}\n    {body} .")


def to_turtle(store: Store, base: str = DEFAULT_BASE, include_history: bool = True) -> str:
    """Serialize entities, sources, changesets and assertions as Turtle.

    With ``include_history`` the current version of every assertion is exported, including
    retracted and superseded ones (with ``prov:invalidatedAtTime``); otherwise only accepted
    knowledge is.
    """
    w = _Writer()
    header = (
        _PREFIXES
        + f"@prefix kge: <{base}> .\n"
        + f"@prefix kgo: <{base}ontology/> .\n"
        + f"@prefix kgs: <{base}schema#> .\n"
    )

    # schema: entity types and predicates from the ontology
    for name, t in sorted(store.ontology.entity_types.items()):
        pairs = [("a", "rdfs:Class")]
        pairs += [("kgs:disjointWith", f"kgo:{other}") for other in t.disjoint_with]
        w.node(f"kgo:{name}", pairs)
    for name, p in sorted(store.ontology.predicates.items()):
        rng = p.range if p.literal_range else f"kgo:{p.range}"
        pairs = [
            ("a", "rdf:Property"),
            ("rdfs:domain", f"kgo:{p.domain}"),
            ("rdfs:range", rng),
            ("kgs:functional", _typed(str(p.functional).lower(), "xsd:boolean")),
        ]
        if p.description:
            pairs.append(("rdfs:comment", _lit(p.description, "ja")))
        w.node(f"kgo:{name}", pairs)
    for rule in store.ontology.rules:
        w.node(
            f"kge:rule_{_local(rule.id)}",
            [
                ("a", "prov:Plan"),
                ("rdfs:label", _lit(rule.id)),
                ("kgs:if", _lit(" . ".join(rule.if_))),
                ("kgs:then", _lit(rule.then)),
            ],
        )

    for e in store.entities():
        pairs = [("a", ", ".join(f"kgo:{t}" for t in e.types)), ("rdfs:label", _lit(e.label))]
        pairs += [("skos:altLabel", _lit(a)) for a in e.aliases]
        w.node(f"kge:{e.id}", pairs)

    for sid, version in store.conn.execute(
        "SELECT id, version FROM sources ORDER BY id, version"
    ).fetchall():
        src = store.get_source(sid, version)
        assert src is not None
        pairs = [
            ("a", "prov:Entity"),
            ("dcterms:title", _lit(src.title)),
            ("prov:atLocation", _lit(src.uri)),
            ("kgs:sourceKind", _lit(src.kind.value)),
            ("kgs:contentHash", _lit(src.content_hash)),
            ("prov:generatedAtTime", _dt(src.retrieved_at)),
        ]
        if version > 1:
            pairs.append(("prov:wasRevisionOf", f"kge:{sid}_v{version - 1}"))
        w.node(f"kge:{sid}_v{version}", pairs)

    agents: set[str] = set()
    for cs in store.changesets():
        agents.add(cs.author)
        pairs = [
            ("a", "prov:Activity"),
            ("rdfs:label", _lit(cs.message)),
            ("prov:startedAtTime", _dt(cs.created_at)),
            ("prov:wasAssociatedWith", f"kge:agent_{_local(cs.author)}"),
        ]
        if cs.parent_id:
            pairs.append(("prov:wasInformedBy", f"kge:changeset_{cs.parent_id}"))
        w.node(f"kge:changeset_{cs.id}", pairs)

    if include_history:
        rows = store.conn.execute(
            "SELECT id FROM assertion_versions WHERE cs_to IS NULL ORDER BY id"
        ).fetchall()
        assertions = [a for (aid,) in rows if (a := store.get_assertion(aid)) is not None]
    else:
        assertions = store.active(include_hypotheses=True)

    for a in assertions:
        agents.update({a.extracted_by, a.approved_by})
        w.node(f"kge:{a.id}", _assertion_pairs(a, store.history(a.id)))
        if a.status is Status.ACCEPTED and a.epistemic_type is not EpistemicType.HYPOTHESIS:
            w.blocks.append(f"kge:{a.subject} kgo:{a.predicate} {_object(a)} .")

    for agent in sorted(agents):
        kind = "prov:Person" if agent.startswith("human:") else "prov:SoftwareAgent"
        w.node(f"kge:agent_{_local(agent)}", [("a", kind), ("rdfs:label", _lit(agent))])

    return header + "\n" + "\n\n".join(w.blocks) + "\n"


def _object(a: Assertion) -> str:
    if a.object_kind == "entity":
        return f"kge:{a.object_value}"
    return _typed(a.object_value, a.object_datatype or "xsd:string")


def _assertion_pairs(a: Assertion, history: list[Assertion]) -> list[tuple[str, str]]:
    """`a` is the current version; `history` all versions, oldest first."""
    first = history[0]
    kind = {
        EpistemicType.FACT: "kgs:Fact",
        EpistemicType.INFERENCE: "kgs:Inference",
        EpistemicType.HYPOTHESIS: "kgs:Hypothesis",
    }[a.epistemic_type]
    pairs = [
        ("a", f"rdf:Statement, prov:Entity, {kind}"),
        ("rdf:subject", f"kge:{a.subject}"),
        ("rdf:predicate", f"kgo:{a.predicate}"),
        ("rdf:object", _object(a)),
        ("kgs:status", _lit(a.status.value)),
        ("kgs:confidence", _typed(repr(a.confidence), "xsd:decimal")),
        ("kgs:confidenceMethod", _lit(a.confidence_method)),
        ("prov:generatedAtTime", _dt(a.recorded_at)),
        ("prov:wasGeneratedBy", f"kge:changeset_{first.changeset}"),
        ("prov:wasAttributedTo", f"kge:agent_{_local(a.extracted_by)}"),
        ("kgs:approvedBy", f"kge:agent_{_local(a.approved_by)}"),
    ]
    if a.valid_from:
        pairs.append(("kgs:validFrom", _typed(a.valid_from.isoformat(), "xsd:date")))
    if a.valid_to:
        pairs.append(("kgs:validTo", _typed(a.valid_to.isoformat(), "xsd:date")))
    if a.status is not Status.ACCEPTED and len(history) > 1:
        closed = history[-2].superseded_at
        assert closed is not None
        pairs.append(("prov:invalidatedAtTime", _dt(closed)))
        pairs.append(("prov:wasInvalidatedBy", f"kge:changeset_{a.changeset}"))
    for ev in a.evidence:
        pairs.append(("prov:wasDerivedFrom", f"kge:{ev.source_id}_v{ev.source_version}"))
        pairs.append(("kgs:quote", _lit(ev.quote)))
    if a.justification:
        pairs.append(("kgs:rule", f"kge:rule_{_local(a.justification.rule_id)}"))
        for pid in a.justification.premise_ids:
            pairs.append(("prov:wasDerivedFrom", f"kge:{pid}"))
    return pairs
