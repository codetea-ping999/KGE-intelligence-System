from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from kge.clock import DEFAULT_FIXED_START, FixedClock
from kge.extract import ExtractionResult, LLMExtractor, ReplayLLMClient, RuleExtractor
from kge.models import Candidate, EntityRef, EpistemicType, Evidence, LiteralValue, Source
from kge.pipeline import Engine

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "fixtures/docs"


@pytest.fixture
def engine() -> Engine:
    return Engine.open(":memory:", ROOT / "ontology/base.yaml", FixedClock(DEFAULT_FIXED_START))


def doc(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def rule(e: Engine) -> RuleExtractor:
    return RuleExtractor(e.ontology)


def llm(e: Engine) -> LLMExtractor:
    return LLMExtractor(ReplayLLMClient(ROOT / "fixtures/llm"), e.ontology)


class StaticExtractor:
    """Test extractor: builds candidates from (subject, predicate, object) specs."""

    def __init__(self, ident: str, build: Callable[[Source], list[Candidate]]) -> None:
        self.id = ident
        self._build = build

    def extract(self, source: Source) -> ExtractionResult:
        return ExtractionResult(self._build(source))


def cand(
    source: Source,
    s: str,
    p: str,
    o: str,
    *,
    s_type: str | None = None,
    o_type: str | None = None,
    datatype: str | None = None,
    quote: str | None = None,
    etype: EpistemicType = EpistemicType.FACT,
    by: str = "rule:test",
    confidence: float = 0.9,
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> Candidate:
    import datetime as dt

    obj: EntityRef | LiteralValue = (
        LiteralValue(value=o, datatype=datatype) if datatype else EntityRef(label=o, type=o_type)
    )
    q = quote if quote is not None else source.content.splitlines()[0]
    return Candidate(
        subject=EntityRef(label=s, type=s_type),
        predicate=p,
        object=obj,
        epistemic_type=etype,
        confidence=confidence,
        confidence_method="test",
        valid_from=dt.date.fromisoformat(valid_from) if valid_from else None,
        valid_to=dt.date.fromisoformat(valid_to) if valid_to else None,
        evidence=[Evidence(source_id=source.id, source_version=source.version, quote=q)]
        if q
        else [],
        extracted_by=by,
    )


def ingest_specs(
    e: Engine,
    uri: str,
    content: str,
    build: Callable[[Source], list[Candidate]],
    ident: str = "rule:test",
):  # type: ignore[no-untyped-def]
    return e.ingest(uri, content, StaticExtractor(ident, build))


def bootstrap(e: Engine) -> None:
    e.ingest("registry", doc("registry_v1.txt"), rule(e))
