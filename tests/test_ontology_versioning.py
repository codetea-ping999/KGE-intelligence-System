"""Ontology versioning: changes are recorded, analysed for impact, approved by a human and
applied as an audited migration that re-validates existing knowledge."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import rdflib
import yaml
from rdflib.namespace import OWL

from conftest import ROOT, bootstrap, cand, ingest_specs
from kge import audit, ontology_change
from kge.clock import DEFAULT_FIXED_START, FixedClock
from kge.export import DEFAULT_BASE, to_turtle
from kge.models import CandidateState, Status
from kge.ontology import Ontology
from kge.pipeline import AUTO, Engine, ReviewError
from kge.qa import checks
from kge.store import OntologyMismatch

BASE = ROOT / "ontology/base.yaml"
V2 = ROOT / "fixtures/ontology/base_v2.yaml"


def _variant(edit) -> Ontology:  # type: ignore[no-untyped-def]
    data = yaml.safe_load(BASE.read_text(encoding="utf-8"))
    edit(data)
    return Ontology.model_validate(data)


def _actions(e: Engine) -> list[str]:
    return [x["action"] for x in audit.entries(e.store.conn)]


def test_first_open_records_version_1(engine) -> None:  # type: ignore[no-untyped-def]
    [v] = engine.store.ontology_versions()
    assert (v.version, v.after_changeset, v.actor) == (1, 0, "system")
    assert v.content_hash == Ontology.load(BASE).content_hash()
    assert _actions(engine) == ["ontology_registered"]
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        engine.store.conn.execute("UPDATE ontology_versions SET reason='x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        engine.store.conn.execute("DELETE FROM ontology_versions")


def test_canonical_hash_ignores_yaml_formatting(tmp_path: Path) -> None:
    reformatted = tmp_path / "o.yaml"
    reformatted.write_text(
        yaml.safe_dump(yaml.safe_load(BASE.read_text("utf-8")), allow_unicode=True), "utf-8"
    )
    assert Ontology.load(reformatted).content_hash() == Ontology.load(BASE).content_hash()


def test_changed_ontology_file_is_refused_until_applied(tmp_path: Path) -> None:
    db = tmp_path / "kge.db"
    e = Engine.open(db, BASE, FixedClock(DEFAULT_FIXED_START))
    bootstrap(e)
    e.store.close()
    with pytest.raises(OntologyMismatch, match="kge ontology plan"):
        Engine.open(db, V2, FixedClock(DEFAULT_FIXED_START))
    e = Engine.open(db, V2, FixedClock(DEFAULT_FIXED_START), allow_ontology_change=True)
    assert "works_in_region" in e.ontology.predicates  # v1 stays in effect
    assert e.store.pending_ontology is not None
    assert not next(c for c in checks(e.store) if c.name == "ontology_versioned").ok


def test_plan_is_read_only_and_finds_stale_inferences(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    head, entries = engine.store.head(), len(_actions(engine))
    p = ontology_change.plan(engine, Ontology.load(V2))
    assert [str(c) for c in p.changes] == [
        "+ predicate located_in_region",
        "- predicate works_in_region",
        "- rule R2_work_region",
        "+ rule R3_org_region",
    ]
    assert p.invalid == []
    stale = {engine.store.get_assertion(f.assertion_id).predicate for f in p.stale_inferences}  # type: ignore[union-attr]
    assert stale == {"works_in_region"} and len(p.stale_inferences) == 2
    assert {r.assertion_id for r in p.retractions} == {f.assertion_id for f in p.stale_inferences}
    assert engine.store.head() == head and len(_actions(engine)) == entries
    assert len(engine.store.ontology_versions()) == 1


def test_apply_needs_a_human_and_a_change(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    with pytest.raises(ReviewError, match="human"):
        ontology_change.apply(engine, Ontology.load(V2), AUTO, "x")
    with pytest.raises(ReviewError, match="unchanged"):
        ontology_change.apply(engine, Ontology.load(BASE), "human:o", "x")
    assert len(engine.store.ontology_versions()) == 1


def test_apply_migrates_knowledge(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    before = engine.store.head()
    stale = [
        f.assertion_id for f in ontology_change.plan(engine, Ontology.load(V2)).stale_inferences
    ]
    r = ontology_change.apply(engine, Ontology.load(V2), "human:o", "地域は組織単位で管理する")

    assert r.version == 2 and set(r.retracted) == set(stale)
    for aid in stale:
        a = engine.store.get_assertion(aid)
        assert a is not None and a.status is Status.RETRACTED
    derived = {
        (engine.store.label(a.subject), engine.store.label(a.object_value))
        for a in engine.store.active(predicate="located_in_region")
    }
    assert derived == {("Acme社", "アジア"), ("Beta社", "ヨーロッパ")}
    assert (
        engine.ontology is engine.store.ontology
        and "works_in_region" not in engine.ontology.predicates
    )
    # the versions are tied to changesets, so the past stays interpretable
    assert engine.store.ontology_version_at(before) == 1
    assert engine.store.ontology_version_at(engine.store.head()) == 2
    assert {a.predicate for a in engine.store.snapshot(changeset=before)} >= {"works_in_region"}
    assert "ontology_changed" in _actions(engine) and audit.verify(engine.store.conn).ok
    assert all(c.ok for c in checks(engine.store))
    # an existing database now opens with v2 only
    assert engine.store.ontology_versions()[-1].content_hash == Ontology.load(V2).content_hash()


def test_changed_rule_withdraws_and_rederives(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)

    def lower_factor(d):  # type: ignore[no-untyped-def]
        d["rules"][0]["confidence_factor"] = 0.5

    p = ontology_change.plan(engine, _variant(lower_factor))
    assert p.stale_inferences and all(
        "R1_work_country was changed" in f.reason for f in p.stale_inferences
    )
    r = ontology_change.apply(engine, _variant(lower_factor), "human:o", "R1 を弱める")
    confs = {a.confidence for a in engine.store.active(predicate="works_in_country")}
    assert confs == {0.475}  # 0.95 * 0.5
    assert r.derived  # re-derived under the changed rule, and R2 on top of them


def test_narrowed_range_blocks_unless_invalid_facts_are_retracted(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)

    def narrow(d):  # type: ignore[no-untyped-def]
        d["predicates"]["works_for"]["range"] = "Company"
        d["predicates"]["employs"]["domain"] = "Company"

    new = _variant(narrow)
    p = ontology_change.plan(engine, new)
    assert {engine.store.get_assertion(f.assertion_id).predicate for f in p.invalid} == {
        "works_for"
    }  # type: ignore[union-attr]
    assert all("not Company" in f.reason for f in p.invalid)
    with pytest.raises(ReviewError, match="retract-invalid"):
        ontology_change.apply(engine, new, "human:o", "企業に限定")
    r = ontology_change.apply(engine, new, "human:o", "企業に限定", retract_invalid=True)
    assert engine.store.active(predicate="works_for") == []
    assert engine.store.active(predicate="works_in_country") == []  # cascade
    assert len(r.retracted) == len(p.retractions)
    assert all(c.ok for c in checks(engine.store))


def test_new_cardinality_limit_is_reported_not_resolved(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    ingest_specs(engine, "x", "台帳", lambda s: [cand(s, "Acme社", "employs", "鈴木")])

    def limit(d):  # type: ignore[no-untyped-def]
        d["predicates"]["employs"]["max_cardinality"] = 1

    p = ontology_change.plan(engine, _variant(limit))
    assert len(p.conflicts) == 2 and p.retractions == []
    ontology_change.apply(engine, _variant(limit), "human:o", "1人まで")
    assert not next(c for c in checks(engine.store) if c.name == "no_active_conflicts").ok


def test_disjoint_entity_types_block_the_change(engine) -> None:  # type: ignore[no-untyped-def]
    ingest_specs(engine, "x", "台帳", lambda s: [cand(s, "鈴木", "works_for", "Delta社")])
    ingest_specs(
        engine, "y", "台帳2", lambda s: [cand(s, "佐藤", "works_for", "Delta社", o_type="Company")]
    )
    delta = engine.store.find_entity_by_label("Delta社")
    assert delta is not None and delta.types == ["Organization", "Company"]

    def split(d):  # type: ignore[no-untyped-def]
        d["entity_types"]["Company"] = {"disjoint_with": ["Organization"]}

    p = ontology_change.plan(engine, _variant(split))
    assert p.entity_conflicts and "Delta社" in p.entity_conflicts[0]
    with pytest.raises(ReviewError, match="disjoint"):
        ontology_change.apply(engine, _variant(split), "human:o", "x", retract_invalid=True)


def test_pending_candidates_are_revalidated(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    r = ingest_specs(
        engine,
        "x",
        "台帳",
        lambda s: [cand(s, "Acme社", "founded_in", "2006", datatype="xsd:gYear")],
    )
    [cid] = r.pending

    def drop(d):  # type: ignore[no-untyped-def]
        del d["predicates"]["founded_in"]

    p = ontology_change.plan(engine, _variant(drop))
    assert [f.assertion_id for f in p.candidates] == [cid]
    assert "SCHEMA_UNKNOWN_PREDICATE" in p.candidates[0].reason
    ontology_change.apply(
        engine, _variant(drop), "human:o", "設立年は扱わない", retract_invalid=True
    )
    sc = engine.store.get_candidate(cid)
    assert sc is not None and sc.state is CandidateState.PENDING
    assert sc.report is not None and "SCHEMA_UNKNOWN_PREDICATE" in sc.report.codes()
    with pytest.raises(ReviewError, match="validation errors"):
        engine.accept(cid, "human:r", "x")


def test_versions_exported_with_provenance(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    ontology_change.apply(engine, Ontology.load(V2), "human:o", "移行")
    g = rdflib.Graph().parse(data=to_turtle(engine.store), format="turtle")
    ont = rdflib.URIRef(DEFAULT_BASE + "ontology")
    v1, v2 = (rdflib.URIRef(DEFAULT_BASE + f"ontology/v{n}") for n in (1, 2))
    assert (ont, OWL.versionIRI, v2) in g and (ont, OWL.priorVersion, v1) in g
    assert (v2, rdflib.URIRef("http://www.w3.org/ns/prov#wasRevisionOf"), v1) in g


def test_cli_plan_apply_log_diff(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from kge.cli import main

    monkeypatch.setenv("KGE_CLOCK", "fixed")
    db = str(tmp_path / "kge.db")
    reg = str(ROOT / "fixtures/docs/registry_v1.txt")
    assert main(["--db", db, "ingest", reg, "--uri", "registry"]) == 0
    v2 = ["--db", db, "--ontology", str(V2)]
    assert main([*v2, "query"]) == 2  # refused until the change is applied
    capsys.readouterr()
    assert main([*v2, "ontology", "plan"]) == 0
    out = capsys.readouterr().out
    assert "- rule R2_work_region" in out and "inferences to withdraw" in out
    assert main([*v2, "ontology", "apply"]) == 2  # needs --actor/--reason
    assert main([*v2, "ontology", "apply", "--actor", "human:o", "--reason", "移行"]) == 0
    assert "applied ontology v2" in capsys.readouterr().out
    assert main([*v2, "query"]) == 0
    capsys.readouterr()
    assert main([*v2, "ontology", "log"]) == 0
    log = capsys.readouterr().out.splitlines()
    assert log[0].startswith("v1") and log[1].startswith("v2") and "human:o" in log[1]
    assert main([*v2, "ontology", "diff", "1", "2"]) == 0
    assert "+ rule R3_org_region" in capsys.readouterr().out
    assert main([*v2, "ontology", "plan"]) == 0
    assert "no changes: v2" in capsys.readouterr().out


def test_failed_apply_rolls_back(engine, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    head = engine.store.head()

    def boom() -> list[object]:
        raise RuntimeError("reasoner failed")

    monkeypatch.setattr(engine, "refresh", lambda: None)  # keep the patched reasoner
    monkeypatch.setattr(engine.reasoner, "run", boom)
    with pytest.raises(RuntimeError):
        ontology_change.apply(engine, Ontology.load(V2), "human:o", "移行")
    assert len(engine.store.ontology_versions()) == 1 and engine.store.head() == head
    assert "works_in_region" in engine.store.ontology.predicates
    assert all(c.ok for c in checks(engine.store))
