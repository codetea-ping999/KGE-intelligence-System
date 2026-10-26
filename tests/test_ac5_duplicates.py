"""AC5: exact duplicates merge evidence; near-duplicate entities are reported."""

from conftest import bootstrap, cand, ingest_specs


def test_exact_duplicate_adds_evidence_instead_of_new_assertion(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    n = len(engine.store.active(include_hypotheses=True))
    r = ingest_specs(
        engine,
        "other",
        "別の台帳: Acme社は日本に所在する。",
        lambda s: [cand(s, "Acme社", "located_in", "日本")],
    )
    sc = engine.store.get_candidate(r.staged[0])
    assert "DUPLICATE_EXACT" in sc.report.codes() and sc.state.value == "accepted"
    assert len(engine.store.active(include_hypotheses=True)) == n
    a = engine.store.get_assertion(sc.assertion_id)
    assert {e.source_id for e in a.evidence} == {"S001", r.source_id}


def test_width_and_case_variants_are_reported_not_merged(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    r = ingest_specs(
        engine,
        "x",
        "ＡＣＭＥ社は2005年に設立された。",
        lambda s: [cand(s, "ＡＣＭＥ社", "founded_in", "2005", datatype="xsd:gYear")],
    )
    sc = engine.store.get_candidate(r.staged[0])
    issue = next(i for i in sc.report.issues if i.code == "DUPLICATE_ENTITY_CANDIDATE")
    acme = engine.store.find_entity_by_label("Acme社")
    assert issue.related == [acme.id]
    assert sc.state.value == "pending"  # a human decides whether they are the same


def test_alias_match_is_reported(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    acme = engine.store.find_entity_by_label("Acme社")
    engine.merge_alias(acme.id, "アクメ", "human:alice")
    r = ingest_specs(
        engine, "x", "アクメは日本に所在する。", lambda s: [cand(s, "アクメ", "located_in", "日本")]
    )
    sc = engine.store.get_candidate(r.staged[0])
    assert "DUPLICATE_ENTITY_CANDIDATE" in sc.report.codes()
