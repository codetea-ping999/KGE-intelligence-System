"""AC1: facts, inferences and hypotheses are distinguished and guarded."""

import datetime as dt

import pytest
from pydantic import ValidationError

from conftest import bootstrap, cand, ingest_specs
from kge.models import Assertion, EpistemicType, Evidence, Status

BASE = dict(
    id="A1",
    subject="E1",
    predicate="works_for",
    object_value="E2",
    object_kind="entity",
    status=Status.ACCEPTED,
    confidence=0.9,
    confidence_method="t",
    recorded_at=dt.datetime(2026, 1, 1),
    extracted_by="rule:t",
    approved_by="policy:auto",
    changeset=1,
)
EV = [Evidence(source_id="S1", source_version=1, quote="q")]


def test_fact_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        Assertion(**BASE, epistemic_type=EpistemicType.FACT)  # type: ignore[arg-type]
    Assertion(**BASE, epistemic_type=EpistemicType.FACT, evidence=EV)  # type: ignore[arg-type]


def test_inference_requires_justification() -> None:
    with pytest.raises(ValidationError, match="justification"):
        Assertion(**BASE, epistemic_type=EpistemicType.INFERENCE)  # type: ignore[arg-type]


def test_three_types_coexist_and_hypotheses_hidden_by_default(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    r = ingest_specs(
        engine,
        "memo",
        "メモ: 田中アリスはいずれ転職するかもしれない。",
        lambda s: [
            cand(
                s,
                "田中アリス",
                "works_in_region",
                "ヨーロッパ",
                etype=EpistemicType.HYPOTHESIS,
                valid_from="2030-01-01",
            )
        ],
    )
    assert r.pending == []  # clean hypothesis from a deterministic extractor
    types = {a.epistemic_type for a in engine.store.active(include_hypotheses=True)}
    assert types == {EpistemicType.FACT, EpistemicType.INFERENCE, EpistemicType.HYPOTHESIS}
    default = {a.epistemic_type for a in engine.store.active()}
    assert EpistemicType.HYPOTHESIS not in default


def test_hypotheses_never_feed_the_reasoner(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    ingest_specs(
        engine,
        "memo",
        "メモ",
        lambda s: [
            cand(s, "鈴木", "works_for", "Acme社", s_type="Person", etype=EpistemicType.HYPOTHESIS)
        ],
    )
    suzuki = engine.store.find_entity_by_label("鈴木")
    assert suzuki is not None
    assert engine.store.active(subject=suzuki.id) == []
    inferred = engine.store.active(subject=suzuki.id, include_hypotheses=True)
    assert [a.epistemic_type for a in inferred] == [EpistemicType.HYPOTHESIS]


def test_extractors_cannot_claim_inferences(engine) -> None:  # type: ignore[no-untyped-def]
    r = ingest_specs(
        engine,
        "x",
        "x",
        lambda s: [cand(s, "A", "works_for", "B", etype=EpistemicType.INFERENCE)],
    )
    assert r.rejected and engine.store.active(include_hypotheses=True) == []
