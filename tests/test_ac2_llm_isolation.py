"""AC2: LLM output is quarantined until a human approves it."""

import pytest

from conftest import ROOT, bootstrap, doc, llm
from kge.extract import LLMExtractor, ReplayLLMClient
from kge.models import CandidateState, EpistemicType, SourceKind, is_llm_actor
from kge.pipeline import AUTO, ReviewError, accept_op
from kge.store import AddAssertion, UnauthorizedCommit
from kge.validate import LLM_CONFIDENCE_CAP


def _llm_ingest(engine):  # type: ignore[no-untyped-def]
    bootstrap(engine)
    return engine.ingest("news", doc("news_2026.txt"), llm(engine))


def test_llm_candidates_are_never_auto_accepted(engine) -> None:  # type: ignore[no-untyped-def]
    r = _llm_ingest(engine)
    assert r.accepted == [] and r.changesets == []
    assert not any(
        is_llm_actor(a.extracted_by) for a in engine.store.active(include_hypotheses=True)
    )
    states = {sc.state for sc in engine.store.candidates(source_id=r.source_id)}
    assert states <= {CandidateState.PENDING, CandidateState.REJECTED}


def test_store_refuses_llm_commit_without_human(engine) -> None:  # type: ignore[no-untyped-def]
    r = _llm_ingest(engine)
    sc = engine.store.get_candidate(r.pending[0])
    head = engine.store.head()
    with pytest.raises(UnauthorizedCommit, match="human"):
        engine.store.commit(AUTO, "bypass", [accept_op(sc, sc.report, AUTO)])
    assert engine.store.head() == head  # rolled back


def test_review_requires_human_identity(engine) -> None:  # type: ignore[no-untyped-def]
    r = _llm_ingest(engine)
    with pytest.raises(ReviewError):
        engine.accept(r.pending[0], "policy:auto", "looks fine")
    with pytest.raises(ReviewError):
        engine.accept(r.pending[0], "human:alice", "  ")
    engine.accept(r.pending[0], "human:alice", "checked against the article")
    a = engine.store.get_assertion(engine.store.get_candidate(r.pending[0]).assertion_id)
    assert a.approved_by == "human:alice" and a.epistemic_type is EpistemicType.FACT


def test_no_assertion_without_candidate(engine) -> None:  # type: ignore[no-untyped-def]
    from kge.models import EntityRef

    op = AddAssertion(
        subject=EntityRef(label="X"),
        predicate="works_for",
        object=EntityRef(label="Y"),
        epistemic_type=EpistemicType.FACT,
        confidence=1.0,
        confidence_method="t",
        extracted_by="human:mallory",
        approved_by="human:mallory",
        evidence=[],
    )
    with pytest.raises(UnauthorizedCommit, match="validated candidate"):
        engine.store.commit("human:mallory", "direct write", [op])


def test_unverifiable_llm_fact_is_downgraded_and_capped(engine) -> None:  # type: ignore[no-untyped-def]
    r = _llm_ingest(engine)
    reports = {
        sc.candidate.key()[:3]: sc.report for sc in engine.store.candidates(source_id=r.source_id)
    }
    founded = reports[("Acme社", "founded_in", "2006")]
    assert founded.effective_type is EpistemicType.HYPOTHESIS
    assert {"EVIDENCE_QUOTE_NOT_FOUND", "FACT_DOWNGRADED"} <= set(founded.codes())
    bob = reports[("佐藤ボブ", "works_for", "Acme社")]
    assert bob.effective_type is EpistemicType.FACT
    assert bob.effective_confidence == LLM_CONFIDENCE_CAP


def test_malformed_llm_output_is_recorded_not_staged(engine) -> None:  # type: ignore[no-untyped-def]
    r = _llm_ingest(engine)
    assert r.invalid == 1
    from kge import audit

    assert audit.entries(engine.store.conn, "extraction_invalid")

    class Garbage:
        model = "garbage"

        def complete(self, prompt: str) -> str:
            return "Sure! Here are the facts: ..."

    src, _, _ = engine.store.register_source("g", "g", SourceKind.DOCUMENT, "g")
    res = LLMExtractor(Garbage(), engine.ontology).extract(src)
    assert res.candidates == [] and res.invalid


def test_replay_is_offline_and_strict(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = ReplayLLMClient(tmp_path)
    with pytest.raises(Exception, match="no recorded response"):
        client.complete("unknown prompt")
    assert ReplayLLMClient(ROOT / "fixtures/llm").model == "replay"
