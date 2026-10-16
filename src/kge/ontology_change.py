"""Ontology evolution: diff two ontology versions, analyse what a change does to the
knowledge already committed, and apply it as one audited, versioned migration.

An ontology change is treated like a change to knowledge: it needs a human, it is recorded
in ``ontology_versions`` and the audit log, and its consequences are written as an ordinary
changeset (retractions with reasons), so ``diff``, ``impact`` and as-of queries keep working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kge.models import CandidateState, EpistemicType, is_human_actor
from kge.ontology import Ontology, RuleDef
from kge.pipeline import Engine, ReviewError
from kge.qa import cardinality_violations, schema_problem
from kge.store import CommitResult, Retract
from kge.validate import Validator
from kge.versioning import retract_ops


@dataclass
class Change:
    kind: str  # "type" | "predicate" | "rule"
    name: str
    change: str  # "added" | "removed" | "changed"
    details: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        sign = {"added": "+", "removed": "-", "changed": "~"}[self.change]
        extra = f" ({'; '.join(self.details)})" if self.details else ""
        return f"{sign} {self.kind} {self.name}{extra}"


def _fields(model: Any) -> dict[str, Any]:
    return dict(model.model_dump(mode="json", by_alias=True))


def _compare(kind: str, old: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    out = []
    for name in sorted(old.keys() | new.keys()):
        if name not in new:
            out.append(Change(kind, name, "removed"))
        elif name not in old:
            out.append(Change(kind, name, "added"))
        else:
            a, b = _fields(old[name]), _fields(new[name])
            details = [f"{k}: {a.get(k)!r} → {b.get(k)!r}" for k in sorted(a) if a[k] != b.get(k)]
            if details:
                out.append(Change(kind, name, "changed", details))
    return out


def diff_ontologies(old: Ontology, new: Ontology) -> list[Change]:
    """Structural difference, including the rules compiled from property axioms."""
    return [
        *_compare("type", dict(old.entity_types), dict(new.entity_types)),
        *_compare("predicate", dict(old.predicates), dict(new.predicates)),
        *_compare("rule", {r.id: r for r in old.all_rules()}, {r.id: r for r in new.all_rules()}),
    ]


def _same_rule(a: RuleDef | None, b: RuleDef | None) -> bool:
    return (
        a is not None
        and b is not None
        and (a.if_, a.then, a.confidence_factor) == (b.if_, b.then, b.confidence_factor)
    )


@dataclass
class Finding:
    assertion_id: str
    reason: str


@dataclass
class OntologyPlan:
    from_version: int
    changes: list[Change]
    #: stated knowledge that no longer conforms; retracted only with ``retract_invalid``
    invalid: list[Finding] = field(default_factory=list)
    #: inferences whose rule was removed or changed, or that no longer conform
    stale_inferences: list[Finding] = field(default_factory=list)
    #: everything that would be retracted, cascade included
    retractions: list[Retract] = field(default_factory=list)
    #: assertions that exceed a (new) cardinality limit; reported, never auto-resolved
    conflicts: list[str] = field(default_factory=list)
    #: entities whose types become disjoint
    entity_conflicts: list[str] = field(default_factory=list)
    #: pending candidates whose validation result changes
    candidates: list[Finding] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.changes

    def blocking(self, retract_invalid: bool) -> list[str]:
        reasons = []
        if self.invalid and not retract_invalid:
            reasons.append(
                f"{len(self.invalid)} stated assertion(s) would no longer conform"
                " (pass retract_invalid / --retract-invalid to retract them)"
            )
        if self.entity_conflicts:
            reasons.append("entities would get disjoint types: " + ", ".join(self.entity_conflicts))
        return reasons


def plan(engine: Engine, new: Ontology) -> OntologyPlan:
    """What applying ``new`` would do. Read-only."""
    store = engine.store
    old = store.ontology
    versions = store.ontology_versions()
    result = OntologyPlan(from_version=versions[-1].version, changes=diff_ontologies(old, new))
    if result.empty:
        return result

    active = store.active(include_hypotheses=True)
    to_retract: list[str] = []
    for a in active:
        problem = schema_problem(store, new, a)
        if a.epistemic_type is EpistemicType.INFERENCE:
            assert a.justification is not None
            rid = a.justification.rule_id
            if problem is None and not _same_rule(old.rule(rid), new.rule(rid)):
                problem = f"rule {rid} was {'changed' if new.rule(rid) else 'removed'}"
            if problem:
                result.stale_inferences.append(Finding(a.id, problem))
                to_retract.append(a.id)
        elif problem:
            result.invalid.append(Finding(a.id, problem))
            to_retract.append(a.id)
    result.retractions = retract_ops(store, to_retract, "ontology change")
    for r in result.retractions:  # explain the cascade with the reason of each root
        found = next(
            (
                f
                for f in [*result.invalid, *result.stale_inferences]
                if f.assertion_id == r.assertion_id
            ),
            None,
        )
        if found is not None:
            r.reason = f"ontology v{result.from_version + 1}: {found.reason}"

    gone = {r.assertion_id for r in result.retractions}
    remaining = [a for a in active if a.id not in gone]
    result.conflicts = cardinality_violations(new, remaining)
    for e in store.entities():
        pairs = [(x, y) for i, x in enumerate(e.types) for y in e.types[i + 1 :]]
        if any(new.disjoint(x, y) for x, y in pairs):
            result.entity_conflicts.append(f"{e.id}:{e.label} ({'/'.join(e.types)})")

    validator = Validator(store, new)
    for sc in store.candidates(state=CandidateState.PENDING):
        before = sc.report.codes() if sc.report else []
        after = validator.validate(sc.candidate, exclude=gone).codes()
        if before != after:
            result.candidates.append(
                Finding(sc.id, f"{', '.join(before) or '-'} → {', '.join(after) or '-'}")
            )
    return result


@dataclass
class ApplyResult:
    version: int
    plan: OntologyPlan
    changesets: list[int] = field(default_factory=list)
    retracted: list[str] = field(default_factory=list)
    derived: list[str] = field(default_factory=list)


def apply(
    engine: Engine, new: Ontology, actor: str, reason: str, retract_invalid: bool = False
) -> ApplyResult:
    """Make ``new`` the ontology in effect: record the version, retract what it invalidates
    (cascading to dependent inferences), re-run the reasoner, and re-validate pending
    candidates. All-or-nothing."""
    if not is_human_actor(actor):
        raise ReviewError("ontology changes must be approved by a human: use 'human:<name>'")
    if not reason.strip():
        raise ReviewError("a reason is required")
    p = plan(engine, new)
    if p.empty:
        raise ReviewError("the ontology is unchanged")
    blocking = p.blocking(retract_invalid)
    if blocking:
        raise ReviewError("; ".join(blocking))

    store = engine.store
    previous = store.ontology
    try:
        return _apply(engine, new, actor, reason, p)
    except BaseException:
        # the transaction rolled back; put the in-memory ontology back as well
        store.ontology = previous
        engine.refresh()
        raise


def _apply(engine: Engine, new: Ontology, actor: str, reason: str, p: OntologyPlan) -> ApplyResult:
    store = engine.store
    with store.transaction():
        version = store.record_ontology(new, actor, reason)
        engine.refresh()
        result = ApplyResult(version=version, plan=p)
        if p.retractions:
            cr = store.commit(actor, f"ontology v{version} migration: {reason}", p.retractions)
            result.changesets.append(cr.changeset)
            result.retracted = cr.retracted
        rounds: list[CommitResult] = engine.reasoner.run()
        result.changesets.extend(r.changeset for r in rounds)
        result.derived = [aid for r in rounds for aid in r.created]
        for sc in store.candidates(state=CandidateState.PENDING):
            store.set_report(sc.id, engine.validator.validate(sc.candidate))
    return result
