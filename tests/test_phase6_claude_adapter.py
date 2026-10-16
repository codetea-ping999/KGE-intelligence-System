"""Phase 6: Claude adapter request shape, failure handling and record/replay round trip."""

import json
from types import SimpleNamespace

import pytest

from conftest import ROOT, bootstrap, doc
from kge.extract import LLMExtractor, ReplayLLMClient, prompt_key
from kge.llm_claude import (
    EXTRACTION_SCHEMA,
    ClaudeLLMClient,
    LLMRefused,
    LLMTruncated,
    RecordingLLMClient,
)
from kge.models import SourceKind

RESPONSE = json.loads(
    json.loads(next((ROOT / "fixtures/llm").glob("*.json")).read_text("utf-8"))["response"]
)


class FakeMessages:
    def __init__(self, stop_reason: str = "end_turn", text: str | None = None) -> None:
        self.calls: list[dict] = []  # type: ignore[type-arg]
        self.stop_reason = stop_reason
        self.text = text if text is not None else json.dumps(RESPONSE, ensure_ascii=False)

    def create(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            stop_details=None,
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=self.text)],
        )  # fmt: skip


def fake_client(**kw):  # type: ignore[no-untyped-def]
    messages = FakeMessages(**kw)
    return ClaudeLLMClient(
        client=SimpleNamespace(beta=SimpleNamespace(messages=messages))
    ), messages


def test_request_uses_structured_output_and_fallback() -> None:
    client, messages = fake_client()
    assert json.loads(client.complete("prompt"))["assertions"]
    [call] = messages.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": EXTRACTION_SCHEMA}
    assert call["output_config"]["effort"] == "medium"
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in call and "tool_choice" not in call


@pytest.mark.parametrize(("stop", "exc"), [("refusal", LLMRefused), ("max_tokens", LLMTruncated)])
def test_refusal_and_truncation_raise(stop, exc) -> None:  # type: ignore[no-untyped-def]
    client, _ = fake_client(stop_reason=stop)
    with pytest.raises(exc):
        client.complete("prompt")


def test_live_output_is_still_quarantined(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    client, _ = fake_client()
    r = engine.ingest("news", doc("news_2026.txt"), LLMExtractor(client, engine.ontology))
    assert r.accepted == [] and len(r.pending) == 3
    sc = engine.store.get_candidate(r.pending[0])
    assert sc.candidate.extracted_by.startswith("llm:claude-opus-5-5@")


def test_record_then_replay_gives_the_same_candidates(engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    live, _ = fake_client()
    recorder = RecordingLLMClient(live, tmp_path)
    extractor = LLMExtractor(recorder, engine.ontology)
    src, _, _ = engine.store.register_source(
        "news", "news", SourceKind.DOCUMENT,
        doc("news_2026.txt"),
    )  # fmt: skip
    recorded = extractor.extract(src)
    key = prompt_key(extractor.prompt(src))
    saved = json.loads((tmp_path / f"{key}.json").read_text("utf-8"))
    assert saved["model"] == "claude-opus-5-5"
    replayed = LLMExtractor(ReplayLLMClient(tmp_path), engine.ontology).extract(src)
    assert [c.key() for c in replayed.candidates] == [c.key() for c in recorded.candidates]


def test_failed_live_call_rolls_back_the_ingest(engine) -> None:  # type: ignore[no-untyped-def]
    from kge import audit
    from kge.llm_claude import LLMUnavailable

    class NoCredentials:
        def create(self, **kwargs):  # type: ignore[no-untyped-def]
            raise TypeError("Could not resolve authentication method.")

    broken = ClaudeLLMClient(client=SimpleNamespace(beta=SimpleNamespace(messages=NoCredentials())))
    extractor = LLMExtractor(broken, engine.ontology)
    with pytest.raises(LLMUnavailable):
        engine.ingest("news", doc("news_2026.txt"), extractor)
    assert engine.store.candidates() == []
    assert engine.store.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    assert [e["action"] for e in audit.entries(engine.store.conn)] == ["ingest_failed"]
    assert audit.verify(engine.store.conn).ok
    # a retry with a working client is processed, not skipped as "unchanged"
    working, _ = fake_client()
    r = engine.ingest("news", doc("news_2026.txt"), LLMExtractor(working, engine.ontology))
    assert not r.skipped and r.staged
