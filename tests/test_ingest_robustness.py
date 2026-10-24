"""Ingestion edge cases: failed extraction, shared support, duplicates and promotions."""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import cand, ingest_specs, rule
from kge.extract import ExtractionFailed, ExtractionResult
from kge.models import EpistemicType, Source, Status
from kge.pipeline import accept_op
from kge.store import UnauthorizedCommit

DOC = "田中アリスはAcme社に勤務している（2019-04-01〜）。\nAcme社は日本に所在する。\n"


class _Unreadable:
    id = "rule:ja-registry-v1"

    def extract(self, source: Source) -> ExtractionResult:
        return ExtractionResult([], [{"error": "unreadable"}], failed=True)


def _located(e) -> list:  # type: ignore[no-untyped-def]
    return e.store.active(predicate="located_in", include_hypotheses=True)


def test_failed_extraction_changes_nothing_and_can_be_retried(engine) -> None:  # type: ignore[no-untyped-def]
    engine.ingest("doc", DOC, rule(engine))
    before = [a.id for a in engine.store.active()]
    v2 = DOC + "Beta社はドイツに所在する。\n"
    with pytest.raises(ExtractionFailed):
        engine.ingest("doc", v2, _Unreadable())
    assert [a.id for a in engine.store.active()] == before  # nothing was taken as deleted
    r = engine.ingest("doc", v2, rule(engine))  # the failed attempt left no trace
    assert not r.skipped and r.version == 2 and len(r.accepted) == 1


def test_removing_two_premises_of_one_inference(engine) -> None:  # type: ignore[no-untyped-def]
    engine.ingest("doc", DOC, rule(engine))
    assert engine.store.active(predicate="works_in_country")
    r = engine.ingest("doc", "# 空\n", rule(engine))
    assert engine.store.active() == [] and len(r.retracted) == len(set(r.retracted))


def test_statement_stays_while_another_source_states_it(engine) -> None:  # type: ignore[no-untyped-def]
    engine.ingest("a", DOC, rule(engine))
    engine.ingest("b", "Acme社は日本に所在する。\n", rule(engine))
    [loc] = _located(engine)
    assert len({ev.source_id for ev in loc.evidence}) == 2
    engine.ingest("a", "田中アリスはAcme社に勤務している（2019-04-01〜）。\n", rule(engine))
    assert [a.id for a in _located(engine)] == [loc.id]  # b still states it
    engine.ingest("b", "# 空\n", rule(engine))
    assert _located(engine) == []  # now neither does


def test_statement_repeated_in_one_document_is_one_assertion(engine) -> None:  # type: ignore[no-untyped-def]
    r = engine.ingest("doc", "Acme社は日本に所在する。\nAcme社は日本に所在する。\n", rule(engine))
    assert len(_located(engine)) == 1 and len(r.accepted) == 1 and r.unchanged == 1


def test_verified_fact_replaces_matching_hypothesis(engine) -> None:  # type: ignore[no-untyped-def]
    ingest_specs(
        engine,
        "memo",
        "Acme社は日本にあるらしい。",
        lambda s: [
            cand(s, "Acme社", "located_in", "日本", etype=EpistemicType.HYPOTHESIS, quote="")
        ],
    )
    [hyp] = _located(engine)
    assert hyp.epistemic_type is EpistemicType.HYPOTHESIS
    engine.ingest("doc", DOC, rule(engine))
    [fact] = engine.store.active(predicate="located_in")
    assert fact.epistemic_type is EpistemicType.FACT
    assert engine.store.history(hyp.id)[-1].status is Status.SUPERSEDED
    assert engine.store.active(predicate="works_in_country")  # the fact feeds the reasoner


def test_commit_must_match_the_validated_candidate(engine) -> None:  # type: ignore[no-untyped-def]
    r = ingest_specs(
        engine,
        "news",
        "Acme社に関する記事。",
        lambda s: [cand(s, "Acme社", "located_in", "日本", by="llm:x", quote="記事にない引用")],
    )
    [cid] = r.pending
    sc = engine.store.get_candidate(cid)
    assert sc is not None and sc.report is not None
    assert sc.report.effective_type is EpistemicType.HYPOTHESIS  # downgraded: quote not found
    op = accept_op(sc, sc.report, "human:alice")
    for tampered in (
        op.model_copy(update={"epistemic_type": EpistemicType.FACT, "confidence": 1.0}),
        op.model_copy(update={"predicate": "part_of"}),
    ):
        with pytest.raises(UnauthorizedCommit, match="differs from what was validated"):
            engine.store.commit("human:alice", "tampered", [tampered])
    engine.store.commit("human:alice", "as validated", [op])


def test_as_of_time_zone_does_not_change_the_answer(engine) -> None:  # type: ignore[no-untyped-def]
    engine.ingest("doc", "Acme社は日本に所在する。\n", rule(engine))
    t = engine.store.changesets()[-1].created_at
    engine.ingest("doc", "Acme社はドイツに所在する。\n", rule(engine))
    tokyo = t.astimezone(dt.timezone(dt.timedelta(hours=9)))
    at_utc = [(a.id, a.object_value) for a in engine.store.snapshot(at=t)]
    assert at_utc and [(a.id, a.object_value) for a in engine.store.snapshot(at=tokyo)] == at_utc
