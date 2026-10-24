"""Continuous quality assurance: invariant checks, metrics and golden regression."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kge import audit
from kge.models import (
    Assertion,
    CandidateState,
    EpistemicType,
    Status,
    is_human_actor,
    is_llm_actor,
    max_concurrent,
)
from kge.ontology import Ontology, literal_ok
from kge.store import Store


def show_term(store: Store, a: Assertion) -> str:
    return store.label(a.object_value) if a.object_kind == "entity" else a.object_value


def export_snapshot(store: Store) -> list[dict[str, Any]]:
    """Id-independent view of accepted knowledge, used for golden regression."""
    rows = []
    for a in store.active(include_hypotheses=True):
        rows.append(
            {
                "subject": store.label(a.subject),
                "predicate": a.predicate,
                "object": show_term(store, a),
                "type": a.epistemic_type.value,
                "confidence": a.confidence,
                "valid_from": a.valid_from.isoformat() if a.valid_from else None,
                "valid_to": a.valid_to.isoformat() if a.valid_to else None,
                "extracted_by": a.extracted_by,
                "approved_by": a.approved_by,
                "evidence": len(a.evidence),
                "rule": a.justification.rule_id if a.justification else None,
            }
        )
    return sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, ensure_ascii=False))


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class QAReport:
    metrics: dict[str, Any]
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def metrics(store: Store, now: dt.datetime) -> dict[str, Any]:
    active = store.active(include_hypotheses=True)
    by_type = {t.value: 0 for t in EpistemicType}
    for a in active:
        by_type[a.epistemic_type.value] += 1
    pending = store.candidates(state=CandidateState.PENDING)
    cand_states = {s.value: len(store.candidates(state=s)) for s in CandidateState}
    oldest = max(((now - c.created_at).total_seconds() / 86400 for c in pending), default=0.0)
    llm = sum(1 for a in active if is_llm_actor(a.extracted_by))
    return {
        "active_assertions": len(active),
        "by_type": by_type,
        "candidates": cand_states,
        "pending_oldest_days": round(oldest, 2),
        "llm_derived_ratio": round(llm / len(active), 3) if active else 0.0,
        "entities": len(store.entities()),
        "changesets": store.head(),
        "audit_entries": audit.verify(store.conn).entries,
    }


def schema_problem(store: Store, onto: Ontology, a: Assertion) -> str | None:
    """Why ``a`` does not conform to ``onto`` (None when it does)."""
    pdef = onto.predicates.get(a.predicate)
    if pdef is None:
        return f"predicate {a.predicate} is not defined"
    subj = store.get_entity(a.subject)
    if subj is None or not onto.conforms(subj.types, pdef.domain):
        types = "/".join(subj.types) if subj else "missing"
        return f"subject is {types}, not {pdef.domain}"
    if pdef.literal_range:
        if a.object_kind != "literal" or a.object_datatype != pdef.range:
            return f"object is not a {pdef.range} literal"
        if not literal_ok(pdef.range, a.object_value):
            return f"{a.object_value!r} is not a valid {pdef.range}"
        return None
    obj = store.get_entity(a.object_value) if a.object_kind == "entity" else None
    if obj is None or not onto.conforms(obj.types, pdef.range):
        types = "/".join(obj.types) if obj else a.object_kind
        return f"object is {types}, not {pdef.range}"
    return None


def cardinality_violations(onto: Ontology, active: list[Assertion]) -> list[str]:
    """Assertions that, together with others holding at the same time, exceed the number of
    values their predicate allows per subject."""
    by_sp: dict[tuple[str, str], list[Assertion]] = {}
    for a in active:
        pdef = onto.predicates.get(a.predicate)
        if pdef and pdef.cardinality_limit is not None:
            by_sp.setdefault((a.subject, a.predicate), []).append(a)
    out = []
    for (_, pred), group in by_sp.items():
        limit = onto.predicates[pred].cardinality_limit
        assert limit is not None
        spans = [(y.object_value, y.valid_from, y.valid_to) for y in group]
        for x in group:
            # distinct values holding together on some day while x holds
            if max_concurrent(spans, (x.valid_from, x.valid_to)) > limit:
                out.append(x.id)
    return out


def checks(store: Store) -> list[Check]:
    out = []
    v = audit.verify(store.conn)
    out.append(Check("audit_chain_intact", v.ok, v.message))

    # Raw SQL on purpose: the model layer would refuse to load such rows at all.
    no_ev = [
        r[0]
        for r in store.conn.execute(
            "SELECT id FROM assertion_versions v WHERE cs_to IS NULL AND status='accepted'"
            " AND epistemic_type='fact'"
            " AND NOT EXISTS (SELECT 1 FROM evidence e WHERE e.assertion_id = v.id)"
        )
    ]
    out.append(Check("facts_have_evidence", not no_ev, ", ".join(no_ev)))
    try:
        active = store.active(include_hypotheses=True)
    except ValueError as exc:  # pydantic.ValidationError is a ValueError
        out.append(Check("model_integrity", False, str(exc).splitlines()[0]))
        return out

    unsupported = []
    for a in active:
        if a.epistemic_type is EpistemicType.INFERENCE:
            assert a.justification is not None
            for pid in a.justification.premise_ids:
                p = store.get_assertion(pid)
                if p is None or p.status is not Status.ACCEPTED:
                    unsupported.append(a.id)
                    break
    out.append(Check("inferences_supported", not unsupported, ", ".join(unsupported)))

    unapproved = [
        a.id for a in active if is_llm_actor(a.extracted_by) and not is_human_actor(a.approved_by)
    ]
    out.append(Check("llm_output_human_approved", not unapproved, ", ".join(unapproved)))

    conflicts = cardinality_violations(store.ontology, active)
    out.append(Check("no_active_conflicts", not conflicts, ", ".join(conflicts)))

    schema = [a.id for a in active if schema_problem(store, store.ontology, a)]
    out.append(Check("schema_conformance", not schema, ", ".join(schema)))

    versions = store.ontology_versions()
    in_sync = bool(versions) and versions[-1].content_hash == store.ontology.content_hash()
    detail = f"v{versions[-1].version}" if versions else "no ontology recorded"
    if store.pending_ontology is not None:
        in_sync, detail = False, detail + "; a changed ontology is waiting to be applied"
    out.append(Check("ontology_versioned", in_sync, detail))
    return out


def golden_check(snapshot: list[dict[str, Any]], golden: Path) -> Check:
    if not golden.exists():
        return Check("golden_regression", False, f"{golden} not found (run with --update-golden)")
    expected = json.loads(golden.read_text(encoding="utf-8"))
    if expected == snapshot:
        return Check("golden_regression", True, f"{len(snapshot)} assertions match")
    exp = {json.dumps(r, sort_keys=True, ensure_ascii=False) for r in expected}
    got = {json.dumps(r, sort_keys=True, ensure_ascii=False) for r in snapshot}
    return Check(
        "golden_regression",
        False,
        f"missing {len(exp - got)}, unexpected {len(got - exp)}: "
        + "; ".join(sorted(got ^ exp)[:3]),
    )


def run(store: Store, now: dt.datetime) -> QAReport:
    return QAReport(metrics=metrics(store, now), checks=checks(store))
