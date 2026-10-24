"""AC3: source, recording time, validity period and confidence are tracked; as-of queries."""

from conftest import bootstrap, doc, rule
from kge.models import EpistemicType, Status


def test_every_assertion_carries_provenance(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    for a in engine.store.active(include_hypotheses=True):
        assert a.recorded_at is not None and a.confidence_method
        assert 0.0 <= a.confidence <= 1.0
        if a.epistemic_type is EpistemicType.FACT:
            ev = a.evidence[0]
            src = engine.store.get_source(ev.source_id, ev.source_version)
            assert src is not None and ev.quote in src.content and ev.locator
        else:
            assert a.justification and a.justification.premise_ids
    tanaka = engine.store.find_entity_by_label("田中アリス")
    works = engine.store.active(subject=tanaka.id, predicate="works_for")[0]
    assert str(works.valid_from) == "2019-04-01" and works.valid_to is None


def test_as_of_changeset_reproduces_past_state(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    cs = engine.store.head()
    before = [(a.id, a.status) for a in engine.store.active()]
    engine.ingest("registry", doc("registry_v2.txt"), rule(engine))
    assert [(a.id, a.status) for a in engine.store.snapshot(changeset=cs)] == before
    assert [a.id for a in engine.store.active()] != [x for x, _ in before]


def test_as_of_time_and_history(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    located = engine.store.active(predicate="located_in")[0]
    t_before = engine.store.clock.now()
    engine.ingest("registry", doc("registry_v2.txt"), rule(engine))
    past_ids = {a.id for a in engine.store.snapshot(at=t_before)}
    assert located.id in past_ids
    assert located.id not in {a.id for a in engine.store.active()}
    hist = engine.store.history(located.id)
    assert [h.status for h in hist] == [Status.ACCEPTED, Status.RETRACTED]
    assert hist[0].superseded_at is not None and hist[1].recorded_at == hist[0].recorded_at
