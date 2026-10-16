"""AC4: schema validation with coded issues."""

import pytest

from conftest import bootstrap, cand, ingest_specs


def _codes(engine, build):  # type: ignore[no-untyped-def]
    r = ingest_specs(engine, "x", "台帳", lambda s: [build(s)])
    sc = engine.store.get_candidate(r.staged[0])
    return set(sc.report.codes()), sc.state.value


@pytest.mark.parametrize(
    ("build", "code"),
    [
        (lambda s: cand(s, "田中アリス", "likes", "Acme社"), "SCHEMA_UNKNOWN_PREDICATE"),
        (lambda s: cand(s, "X", "works_for", "Acme社", s_type="Organization"), "SCHEMA_DOMAIN"),
        (lambda s: cand(s, "X", "works_for", "2020", datatype="xsd:gYear"), "SCHEMA_RANGE"),
        (lambda s: cand(s, "Acme社", "founded_in", "Acme社"), "SCHEMA_RANGE"),
        (
            lambda s: cand(s, "Acme社", "founded_in", "20x5", datatype="xsd:gYear"),
            "SCHEMA_LITERAL_TYPE",
        ),
        (
            lambda s: cand(s, "Acme社", "founded_in", "2005", datatype="xsd:integer"),
            "SCHEMA_LITERAL_TYPE",
        ),
        (
            lambda s: cand(
                s, "X", "works_for", "Y", valid_from="2024-01-01", valid_to="2023-01-01"
            ),
            "SCHEMA_INTERVAL",
        ),
        (lambda s: cand(s, "X", "works_for", "Y", o_type="Spaceship"), "SCHEMA_UNKNOWN_TYPE"),
        (lambda s: cand(s, "X", "works_for", "Y", quote=""), "EVIDENCE_MISSING"),
        (lambda s: cand(s, "X", "works_for", "Y", quote="書かれていない文"), "EVIDENCE_MISSING"),
    ],
)
def test_schema_errors_are_rejected(engine, build, code) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    codes, state = _codes(engine, build)
    assert code in codes
    assert state == "rejected"


def test_non_temporal_predicate_with_period_needs_review(engine) -> None:  # type: ignore[no-untyped-def]
    codes, state = _codes(
        engine, lambda s: cand(s, "Acme社", "located_in", "日本", valid_from="2020-01-01")
    )
    assert "SCHEMA_NOT_TEMPORAL" in codes and state == "pending"


def test_valid_candidate_has_no_issues(engine) -> None:  # type: ignore[no-untyped-def]
    codes, state = _codes(engine, lambda s: cand(s, "X", "works_for", "Y"))
    assert codes == set() and state == "accepted"
