"""Diffs between changesets and change-impact analysis over justifications."""

from __future__ import annotations

from dataclasses import dataclass, field

from kge.models import Assertion, Status
from kge.store import Retract, Store


@dataclass
class ImpactItem:
    assertion_id: str
    depth: int
    via: str  # the premise through which it is affected


@dataclass
class ImpactReport:
    root: str
    dependents: list[ImpactItem] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)


def impact(store: Store, assertion_id: str) -> ImpactReport:
    """Everything that loses (part of) its justification if `assertion_id` goes away."""
    report = ImpactReport(root=assertion_id)
    seen = {assertion_id}
    frontier = [(assertion_id, 0)]
    entities: set[str] = set()
    root = store.get_assertion(assertion_id)
    if root is not None:
        entities.add(root.subject)
        if root.object_kind == "entity":
            entities.add(root.object_value)
    while frontier:
        aid, depth = frontier.pop(0)
        for dep in store.dependents(aid):
            if dep in seen:
                continue
            a = store.get_assertion(dep)
            if a is None or a.status is not Status.ACCEPTED:
                continue
            seen.add(dep)
            report.dependents.append(ImpactItem(dep, depth + 1, aid))
            entities.add(a.subject)
            if a.object_kind == "entity":
                entities.add(a.object_value)
            frontier.append((dep, depth + 1))
    report.entities = sorted(entities)
    return report


def retract_ops(
    store: Store, ids: list[str], reason: str, status: Status = Status.RETRACTED
) -> list[Retract]:
    """Retract `ids` and, transitively, every inference that depends on them."""
    ops: list[Retract] = []
    done: set[str] = set()
    for aid in ids:
        if aid in done:
            continue
        ops.append(Retract(assertion_id=aid, reason=reason, status=status))
        done.add(aid)
        for item in impact(store, aid).dependents:
            if item.assertion_id not in done:
                ops.append(
                    Retract(
                        assertion_id=item.assertion_id,
                        reason=f"premise {item.via} withdrawn",
                    )
                )
                done.add(item.assertion_id)
    return ops


@dataclass
class Diff:
    base: int
    target: int
    added: list[Assertion]
    removed: list[Assertion]


def diff(store: Store, base: int, target: int, include_hypotheses: bool = True) -> Diff:
    a = {x.id: x for x in store.snapshot(changeset=base, include_hypotheses=include_hypotheses)}
    b = {x.id: x for x in store.snapshot(changeset=target, include_hypotheses=include_hypotheses)}
    return Diff(
        base=base,
        target=target,
        added=[b[k] for k in sorted(b.keys() - a.keys())],
        removed=[a[k] for k in sorted(a.keys() - b.keys())],
    )
