"""AC8: impact analysis follows justifications transitively; retraction cascades."""

from conftest import bootstrap
from kge.models import EpistemicType, Status


def _located_jp(engine):  # type: ignore[no-untyped-def]
    return next(
        a
        for a in engine.store.active(predicate="located_in")
        if engine.store.label(a.object_value) == "日本"
    )


def test_impact_lists_multi_level_dependents(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    rep = engine.impact(_located_jp(engine).id)
    by_depth = {}
    for item in rep.dependents:
        a = engine.store.get_assertion(item.assertion_id)
        by_depth.setdefault(item.depth, set()).add(a.predicate)
    assert by_depth == {1: {"works_in_country"}, 2: {"works_in_region"}}
    labels = {engine.store.label(e) for e in rep.entities}
    assert {"Acme社", "田中アリス", "日本", "アジア"} <= labels


def test_retraction_cascades_and_does_not_rederive(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    root = _located_jp(engine)
    deps = [i.assertion_id for i in engine.impact(root.id).dependents]
    results = engine.retract(root.id, "registry error", "human:alice")
    assert set(results[0].retracted) == {root.id, *deps}
    assert len(results) == 1  # reasoner found nothing new to derive
    for aid in [root.id, *deps]:
        assert engine.store.get_assertion(aid).status is Status.RETRACTED
    active_inf = [a for a in engine.store.active() if a.epistemic_type is EpistemicType.INFERENCE]
    assert all(engine.store.label(a.object_value) not in {"日本", "アジア"} for a in active_inf)


def test_inference_confidence_and_period(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    bob = engine.store.find_entity_by_label("佐藤ボブ")
    [region] = engine.store.active(subject=bob.id, predicate="works_in_region")
    assert region.confidence == round(round(0.95 * 0.95, 4) * 0.95, 4)
    assert (str(region.valid_from), str(region.valid_to)) == ("2020-01-01", "2022-12-31")
    assert region.justification.rule_id == "R2_work_region"
