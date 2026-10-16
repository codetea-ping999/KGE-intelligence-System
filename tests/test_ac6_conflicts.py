"""AC6: contradictions (functional predicates, disjoint types) are detected."""

import pytest

from conftest import bootstrap, cand, ingest_specs
from kge.models import Status
from kge.pipeline import ReviewError


def _report(engine, build, content="台帳"):  # type: ignore[no-untyped-def]
    r = ingest_specs(engine, "x", content, build)
    return [engine.store.get_candidate(c) for c in r.staged]


def test_functional_conflict_in_overlapping_period(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    [sc] = _report(
        engine, lambda s: [cand(s, "田中アリス", "works_for", "Gamma社", valid_from="2024-01-01")]
    )
    assert "CONFLICT_FUNCTIONAL" in sc.report.codes() and sc.state.value == "pending"
    assert len(sc.report.conflicts_with) == 1


def test_no_conflict_when_periods_do_not_overlap(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    [sc] = _report(
        engine, lambda s: [cand(s, "佐藤ボブ", "works_for", "Gamma社", valid_from="2023-01-01")]
    )
    assert sc.report.codes() == [] and sc.state.value == "accepted"


def test_disjoint_type_violation(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    [sc] = _report(engine, lambda s: [cand(s, "日本", "works_for", "Acme社")])
    assert "TYPE_DISJOINT" in sc.report.codes() and sc.state.value == "rejected"


def test_conflict_within_one_batch(engine) -> None:  # type: ignore[no-untyped-def]
    a, b = _report(
        engine,
        lambda s: [
            cand(s, "X", "founded_in", "2001", datatype="xsd:gYear"),
            cand(s, "X", "founded_in", "2002", datatype="xsd:gYear"),
        ],
    )
    assert a.state.value == "accepted"
    assert (
        "CONFLICT_FUNCTIONAL_BATCH" in b.report.codes() or "CONFLICT_FUNCTIONAL" in b.report.codes()
    )
    assert b.state.value == "pending"


def test_conflict_must_be_resolved_explicitly(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    [sc] = _report(
        engine, lambda s: [cand(s, "Acme社", "founded_in", "2006", datatype="xsd:gYear")]
    )
    with pytest.raises(ReviewError, match="conflicts"):
        engine.accept(sc.id, "human:alice", "the article is newer")
    old = sc.report.conflicts_with[0]
    engine.accept(sc.id, "human:alice", "registry was wrong", supersede=[old])
    assert engine.store.get_assertion(old).status is Status.SUPERSEDED
