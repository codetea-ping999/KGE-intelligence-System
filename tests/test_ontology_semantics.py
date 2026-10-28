"""Ontology semantics: class hierarchy, property axioms compiled to rules, cardinality, OWL."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import rdflib
from rdflib.namespace import OWL, RDF, RDFS

from conftest import ROOT, bootstrap, cand, ingest_specs, llm
from kge.clock import DEFAULT_FIXED_START, FixedClock
from kge.export import DEFAULT_BASE, to_turtle
from kge.models import EpistemicType, Source, SourceKind, Status
from kge.ontology import Ontology
from kge.pipeline import Engine
from kge.qa import checks

KGO = rdflib.Namespace(DEFAULT_BASE + "ontology/")
KGS = rdflib.Namespace(DEFAULT_BASE + "schema#")

AXIOMS = """
entity_types:
  Person: {disjoint_with: [Organization, Region]}
  Organization: {disjoint_with: [Region]}
  Region: {}
predicates:
  works_for: {domain: Person, range: Organization, temporal: true}
  leads: {domain: Person, range: Organization, temporal: true, subproperty_of: works_for}
  partner_of: {domain: Organization, range: Organization, symmetric: true}
  within: {domain: Region, range: Region, transitive: true}
  speaks: {domain: Person, range: "xsd:string", max_cardinality: 2, temporal: true}
"""


def _engine(tmp_path: Path, yaml_text: str) -> Engine:
    path = tmp_path / "onto.yaml"
    path.write_text(textwrap.dedent(yaml_text), encoding="utf-8")
    return Engine.open(":memory:", path, FixedClock(DEFAULT_FIXED_START))


def _load(yaml_text: str) -> Ontology:
    import yaml

    return Ontology.model_validate(yaml.safe_load(textwrap.dedent(yaml_text)))


def _facts(e: Engine, predicate: str) -> set[tuple[str, str, str]]:
    return {
        (e.store.label(a.subject), e.store.label(a.object_value), a.epistemic_type.value)
        for a in e.store.active(predicate=predicate)
    }


# ------------------------------------------------------------------ class hierarchy


def test_subclass_satisfies_domain_and_range(engine) -> None:  # type: ignore[no-untyped-def]
    r = ingest_specs(
        engine, "x", "台帳", lambda s: [cand(s, "鈴木", "works_for", "Delta社", o_type="Company")]
    )
    [sc] = [engine.store.get_candidate(c) for c in r.staged]
    assert sc.report.codes() == [] and sc.state.value == "accepted"
    delta = engine.store.find_entity_by_label("Delta社")
    assert delta is not None and delta.types == ["Company"]
    assert all(c.ok for c in checks(engine.store))  # schema_conformance accepts subclasses


def test_disjointness_is_inherited(engine) -> None:  # type: ignore[no-untyped-def]
    onto = engine.ontology
    assert onto.disjoint("Company", "Person") and onto.disjoint("University", "Company")
    assert not onto.disjoint("Company", "Organization")
    ingest_specs(
        engine, "x", "台帳", lambda s: [cand(s, "鈴木", "works_for", "Delta社", o_type="Company")]
    )
    r = ingest_specs(engine, "y", "台帳2", lambda s: [cand(s, "Delta社", "works_for", "Acme社")])
    [sc] = [engine.store.get_candidate(c) for c in r.staged]
    assert "TYPE_DISJOINT" in sc.report.codes() and sc.state.value == "rejected"
    r = ingest_specs(
        engine,
        "z",
        "台帳3",
        lambda s: [cand(s, "佐藤", "works_for", "Delta社", o_type="University")],
    )
    [sc] = [engine.store.get_candidate(c) for c in r.staged]
    assert "TYPE_DISJOINT" in sc.report.codes()


def test_general_type_does_not_overwrite_specific_one(engine) -> None:  # type: ignore[no-untyped-def]
    ingest_specs(
        engine, "x", "台帳", lambda s: [cand(s, "鈴木", "works_for", "Delta社", o_type="Company")]
    )
    ingest_specs(engine, "y", "台帳2", lambda s: [cand(s, "佐藤", "works_for", "Delta社")])
    delta = engine.store.find_entity_by_label("Delta社")
    assert delta is not None and delta.types == ["Company"]


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("entity_types: {A: {subclass_of: B}}\npredicates: {}", "unknown superclass"),
        (
            "entity_types: {A: {subclass_of: B}, B: {subclass_of: A}}\npredicates: {}",
            "subclass_of cycle",
        ),
        (
            "entity_types: {A: {disjoint_with: [B]}, B: {}, C: {subclass_of: A}, "
            "D: {subclass_of: C}}\npredicates: {}",
            None,
        ),
        (
            "entity_types: {A: {disjoint_with: [B]}, B: {subclass_of: A}}\npredicates: {}",
            "unsatisfiable",
        ),
        (
            "entity_types: {A: {}, B: {}}\npredicates:\n"
            "  p: {domain: A, range: B}\n  q: {domain: A, range: B, inverse_of: p}",
            "cover the range/domain",
        ),
        (  # q is derived from every p, so q's domain cannot be narrower than p's range
            "entity_types: {P: {}, A: {}, B: {subclass_of: A}}\npredicates:\n"
            "  p: {domain: P, range: A}\n  q: {domain: B, range: P, inverse_of: p}",
            "cover the range/domain",
        ),
        (
            "entity_types: {A: {}, B: {}}\npredicates:\n  p: {domain: A, range: B, symmetric: true}",
            "symmetric",
        ),
        (
            "entity_types: {A: {}, B: {}}\npredicates:\n"
            "  p: {domain: A, range: B, transitive: true}",
            "transitive",
        ),
        (
            "entity_types: {A: {}}\npredicates:\n"
            "  p: {domain: A, range: 'xsd:string', transitive: true}",
            "entity range",
        ),
        (
            "entity_types: {A: {}, B: {}}\npredicates:\n"
            "  p: {domain: A, range: B}\n  q: {domain: B, range: B, subproperty_of: p}",
            "within those",
        ),
        (
            "entity_types: {A: {}}\npredicates:\n"
            "  p: {domain: A, range: A, functional: true, max_cardinality: 2}",
            "functional implies",
        ),
        (
            "entity_types: {A: {}}\npredicates:\n  p: {domain: A, range: A, symmetric: true}\n"
            "rules: [{id: AX_symmetric_p, if: ['?x p ?y'], then: '?y p ?x'}]",
            "reserved",
        ),
    ],
)
def test_inconsistent_ontologies_are_refused(yaml_text: str, message: str | None) -> None:
    if message is None:
        _load(yaml_text)  # deep hierarchies under disjoint classes are fine
        return
    with pytest.raises(ValueError, match=message):
        _load(yaml_text)


# ------------------------------------------------------------------ property axioms


def test_inverse_is_derived_with_justification_and_retracted_with_premise(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    employs = {
        (engine.store.label(a.subject), engine.store.label(a.object_value)): a
        for a in engine.store.active(predicate="employs")
    }
    assert set(employs) == {("Acme社", "田中アリス"), ("Beta社", "佐藤ボブ")}
    inv = employs[("Acme社", "田中アリス")]
    assert inv.epistemic_type is EpistemicType.INFERENCE
    assert inv.justification is not None
    assert inv.justification.rule_id == "AX_inverse_employs"
    [premise_id] = inv.justification.premise_ids
    premise = engine.store.get_assertion(premise_id)
    assert premise is not None and premise.predicate == "works_for"
    assert (inv.valid_from, inv.valid_to) == (premise.valid_from, premise.valid_to)

    assert inv.id in {d.assertion_id for d in engine.impact(premise_id).dependents}
    engine.retract(premise_id, "test", "human:reviewer")
    after = engine.store.get_assertion(inv.id)
    assert after is not None and after.status is Status.RETRACTED


def test_symmetric_transitive_and_subproperty_axioms(tmp_path: Path) -> None:
    e = _engine(tmp_path, AXIOMS)
    ingest_specs(
        e,
        "x",
        "台帳",
        lambda s: [
            cand(s, "鈴木", "leads", "Acme社", valid_from="2020-01-01"),
            cand(s, "Acme社", "partner_of", "Beta社"),
            cand(s, "渋谷", "within", "東京"),
            cand(s, "東京", "within", "関東"),
            cand(s, "関東", "within", "日本"),
        ],
    )
    assert ("鈴木", "Acme社", "inference") in _facts(e, "works_for")
    assert ("Beta社", "Acme社", "inference") in _facts(e, "partner_of")
    within = _facts(e, "within")
    for pair in [("渋谷", "関東"), ("東京", "日本"), ("渋谷", "日本")]:
        assert (*pair, "inference") in within
    assert len(within) == 6  # 3 stated + 3 derived, no duplicates from the symmetric closure
    assert all(c.ok for c in checks(e.store))


def test_symmetric_closure_does_not_loop(tmp_path: Path) -> None:
    e = _engine(tmp_path, AXIOMS)
    ingest_specs(e, "x", "台帳", lambda s: [cand(s, "Acme社", "partner_of", "Beta社")])
    ingest_specs(e, "y", "台帳2", lambda s: [cand(s, "Beta社", "partner_of", "Acme社")])
    # the stated reverse duplicates the earlier inference instead of adding a third statement
    assert len(_facts(e, "partner_of")) == 2


# ------------------------------------------------------------------ cardinality


def test_max_cardinality(tmp_path: Path) -> None:
    e = _engine(tmp_path, AXIOMS)

    def speaks(lang: str, vf: str | None = None, vt: str | None = None):  # type: ignore[no-untyped-def]
        return lambda s: [
            cand(s, "鈴木", "speaks", lang, datatype="xsd:string", valid_from=vf, valid_to=vt)
        ]

    r1 = ingest_specs(e, "a", "a", speaks("ja"))
    r2 = ingest_specs(e, "b", "b", speaks("en"))
    assert len(r1.accepted) == len(r2.accepted) == 1
    r3 = ingest_specs(e, "c", "c", speaks("de"))
    [sc] = [e.store.get_candidate(c) for c in r3.staged]
    assert "CONFLICT_CARDINALITY" in sc.report.codes() and sc.state.value == "pending"
    assert len(sc.report.conflicts_with) == 2
    r4 = ingest_specs(e, "d", "d", speaks("ja"))  # an exact duplicate is not a new value
    [sc] = [e.store.get_candidate(c) for c in r4.staged]
    assert "CONFLICT_CARDINALITY" not in sc.report.codes()


def test_cardinality_counts_values_holding_at_the_same_time(tmp_path: Path) -> None:
    e = _engine(tmp_path, AXIOMS)

    def speaks(lang: str, vf: str, vt: str):  # type: ignore[no-untyped-def]
        return lambda s: [
            cand(s, "鈴木", "speaks", lang, datatype="xsd:string", valid_from=vf, valid_to=vt)
        ]

    ingest_specs(e, "a", "a", speaks("ja", "2024-01-01", "2024-01-31"))
    ingest_specs(e, "b", "b", speaks("en", "2024-02-01", "2024-02-29"))
    # spans both months, but only two values ever hold on the same day
    r = ingest_specs(e, "c", "c", speaks("de", "2024-01-01", "2024-02-29"))
    assert len(r.accepted) == 1 and r.pending == []
    assert all(c.ok for c in checks(e.store))
    r = ingest_specs(e, "d", "d", speaks("fr", "2024-01-15", "2024-01-20"))  # ja + de + fr
    [sc] = [e.store.get_candidate(c) for c in r.staged]
    assert "CONFLICT_CARDINALITY" in sc.report.codes()


def test_cardinality_counts_earlier_candidates_of_the_same_batch(tmp_path: Path) -> None:
    e = _engine(tmp_path, AXIOMS)
    r = ingest_specs(
        e,
        "a",
        "a",
        lambda s: [
            cand(s, "鈴木", "speaks", lang, datatype="xsd:string") for lang in ("ja", "en", "de")
        ],
    )
    assert len(r.accepted) == 2 and len(r.pending) == 1
    assert all(c.ok for c in checks(e.store))


def test_max_cardinality_one_without_functional(tmp_path: Path) -> None:
    e = _engine(tmp_path, AXIOMS.replace("max_cardinality: 2", "max_cardinality: 1"))
    ingest_specs(e, "a", "a", lambda s: [cand(s, "鈴木", "speaks", "ja", datatype="xsd:string")])
    r = ingest_specs(
        e, "b", "b", lambda s: [cand(s, "鈴木", "speaks", "en", datatype="xsd:string")]
    )
    [sc] = [e.store.get_candidate(c) for c in r.staged]
    assert "CONFLICT_CARDINALITY" in sc.report.codes() and sc.state.value == "pending"


# ------------------------------------------------------------------ extraction and export


def test_llm_prompt_lists_only_canonical_direction(engine) -> None:  # type: ignore[no-untyped-def]
    src = Source(
        id="S1",
        version=1,
        uri="u",
        title="t",
        kind=SourceKind.DOCUMENT,
        content="本文",
        content_hash="h",
        retrieved_at=DEFAULT_FIXED_START,
    )
    prompt = llm(engine).prompt(src)
    assert "works_for(Person→Organization)" in prompt and "employs" not in prompt


def test_reciprocal_inverses_keep_one_direction_for_extraction() -> None:
    onto = _load(
        "entity_types: {P: {}, O: {}}\npredicates:\n"
        "  works_for: {domain: P, range: O, inverse_of: employs}\n"
        "  employs: {domain: O, range: P, inverse_of: works_for}"
    )
    assert onto.extractable_predicates() == ["employs"]


def test_ontology_exported_as_owl() -> None:
    e = Engine.open(":memory:", ROOT / "ontology/base.yaml", FixedClock(DEFAULT_FIXED_START))
    g = rdflib.Graph().parse(data=to_turtle(e.store), format="turtle")
    assert (KGO.Company, RDFS.subClassOf, KGO.Organization) in g
    assert (KGO.University, OWL.disjointWith, KGO.Company) in g
    assert (KGO.employs, OWL.inverseOf, KGO.works_for) in g
    assert (KGO.works_for, RDF.type, OWL.ObjectProperty) in g
    assert (KGO.founded_in, RDF.type, OWL.DatatypeProperty) in g
    # works_for is temporal: one value at a time, not one value ever, so not owl:Functional
    assert (KGO.works_for, RDF.type, OWL.FunctionalProperty) not in g
    assert (KGO.works_for, KGS.functionalAtEachTime, rdflib.Literal(True)) in g
    assert (KGO.located_in, RDF.type, OWL.FunctionalProperty) in g  # timeless


def test_functional_super_property_limits_sub_property_values(tmp_path: Path) -> None:
    e = _engine(
        tmp_path,
        AXIOMS.replace(
            "works_for: {domain: Person, range: Organization, temporal: true}",
            "works_for: {domain: Person, range: Organization, temporal: true, functional: true}",
        ),
    )
    r = ingest_specs(
        e,
        "a",
        "a",
        lambda s: [
            cand(s, "鈴木", "leads", "Acme社", valid_from="2024-01-01"),
            cand(s, "鈴木", "leads", "Beta社", valid_from="2024-01-01"),
        ],
    )
    assert len(r.accepted) == 1 and len(r.pending) == 1
    [sc] = [e.store.get_candidate(c) for c in r.pending]
    assert "CONFLICT_CARDINALITY" in sc.report.codes()  # type: ignore[union-attr]
    assert all(c.ok for c in checks(e.store))


def test_axiom_characteristics_exported(tmp_path: Path) -> None:
    e = _engine(tmp_path, AXIOMS)
    g = rdflib.Graph().parse(data=to_turtle(e.store), format="turtle")
    assert (KGO.within, RDF.type, OWL.TransitiveProperty) in g
    assert (KGO.partner_of, RDF.type, OWL.SymmetricProperty) in g
    assert (KGO.leads, RDFS.subPropertyOf, KGO.works_for) in g


def test_ontology_cli(capsys) -> None:  # type: ignore[no-untyped-def]
    from kge.cli import main

    assert main(["ontology"]) == 0
    out = capsys.readouterr().out
    assert "    Company" in out  # indented under Organization
    assert "AX_inverse_employs (axiom:inverse_of)" in out


def test_conflicting_subtypes_in_one_batch_reject_only_the_offender(engine) -> None:  # type: ignore[no-untyped-def]
    r = ingest_specs(
        engine,
        "x",
        "台帳",
        lambda s: [
            cand(s, "鈴木", "works_for", "Delta社", o_type="Company"),
            cand(s, "佐藤", "works_for", "Delta社", o_type="University"),
        ],
    )
    assert len(r.accepted) == 1 and len(r.rejected) == 1  # no rollback of the whole batch
    [bad] = [engine.store.get_candidate(c) for c in r.rejected]
    assert "TYPE_DISJOINT" in bad.report.codes()  # type: ignore[union-attr]


def test_sub_property_before_its_functional_parent_in_one_batch(tmp_path: Path) -> None:
    e = _engine(
        tmp_path,
        AXIOMS.replace(
            "works_for: {domain: Person, range: Organization, temporal: true}",
            "works_for: {domain: Person, range: Organization, temporal: true, functional: true}",
        ),
    )
    r = ingest_specs(
        e,
        "a",
        "a",
        lambda s: [
            cand(s, "鈴木", "leads", "Acme社", valid_from="2024-01-01"),
            cand(s, "鈴木", "works_for", "Beta社", valid_from="2024-01-01"),
        ],
    )
    assert len(r.accepted) == 1 and len(r.pending) == 1
    assert all(c.ok for c in checks(e.store))  # no second works_for is derived


def test_inverse_rule_ids_are_unambiguous() -> None:
    onto = _load(
        """
        entity_types:
          T: {}
        predicates:
          a: {domain: T, range: T}
          a_b: {domain: T, range: T}
          c: {domain: T, range: T, inverse_of: a_b}
          b_c: {domain: T, range: T, inverse_of: a}
        """
    )
    ids = [r.id for r in onto.axiom_rules()]
    assert len(ids) == len(set(ids))
    assert onto.rule("AX_inverse_c").if_ == ["?x a_b ?y"]  # type: ignore[union-attr]


def test_qa_cardinality_counts_only_values_holding_together() -> None:
    import datetime as dt

    from kge.models import Assertion
    from kge.qa import cardinality_violations

    onto = _load(
        """
        entity_types:
          T: {}
        predicates:
          p: {domain: T, range: "xsd:string", temporal: true, max_cardinality: 1}
        """
    )

    def a(i: int, v: str, f: dt.date, t: dt.date | None) -> Assertion:
        return Assertion.model_construct(
            id=f"A{i}", subject="E1", predicate="p", object_value=v, valid_from=f, valid_to=t
        )

    d = dt.date
    group = [
        a(1, "a", d(2020, 1, 1), d(2020, 6, 1)),
        a(2, "b", d(2020, 5, 1), d(2020, 12, 1)),
        a(3, "c", d(2021, 1, 1), None),  # never together with a or b
        *(a(10 + i, "c", d(2021, 1, 2) + dt.timedelta(days=i), None) for i in range(300)),
    ]
    assert sorted(cardinality_violations(onto, group)) == ["A1", "A2"]
