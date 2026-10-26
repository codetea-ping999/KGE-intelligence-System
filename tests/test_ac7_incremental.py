"""AC7: re-ingestion produces only the difference."""

from conftest import bootstrap, doc, rule


def test_unchanged_document_creates_no_changeset(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    head = engine.store.head()
    n_cand = len(engine.store.candidates())
    r = engine.ingest("registry", doc("registry_v1.txt"), rule(engine))
    assert r.skipped and r.changesets == []
    assert engine.store.head() == head and len(engine.store.candidates()) == n_cand


def test_one_changed_line_yields_one_change(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    r = engine.ingest("registry", doc("registry_v2.txt"), rule(engine))
    assert r.version == 2 and len(r.staged) == 1 and r.unchanged == 6
    ingest_cs = next(cs for cs in engine.store.changesets() if cs.id == r.changesets[0])
    adds = [o["target"] for o in ingest_cs.ops if o["op"] == "add"]
    retracted = [o["target"] for o in ingest_cs.ops if o["op"] == "retract"]
    assert [engine.store.get_assertion(a).predicate for a in adds] == ["located_in"]
    # the old located_in fact plus the inferences that depended on it
    assert engine.store.history(retracted[0])[0].predicate == "located_in"
    assert all(
        engine.store.get_assertion(a).predicate.startswith("works_in") for a in retracted[1:]
    )


def test_third_version_compares_against_all_live_candidates(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    engine.ingest("registry", doc("registry_v2.txt"), rule(engine))
    v3 = doc("registry_v2.txt").replace("日本はアジアに属する。\n", "")
    r = engine.ingest("registry", v3, rule(engine))
    assert r.staged == [] and r.unchanged == 6 and len(r.retracted) == 1
