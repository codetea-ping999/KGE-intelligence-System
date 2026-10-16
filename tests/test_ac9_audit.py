"""AC9: every state change is audited; tampering is detected."""

import sqlite3

import pytest

from conftest import bootstrap, doc, llm
from kge import audit


def test_all_changes_are_audited(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    r = engine.ingest("news", doc("news_2026.txt"), llm(engine))
    engine.accept(r.pending[0], "human:alice", "ok")
    engine.reject(r.pending[1], "human:alice", "no")
    log = audit.entries(engine.store.conn)
    actions = {e["action"] for e in log}
    assert {
        "source_registered",
        "candidate_staged",
        "candidate_validated",
        "candidate_decided",
        "changeset_committed",
        "entity_created",
        "extraction_invalid",
    } <= actions
    committed = {e["payload"]["changeset"] for e in log if e["action"] == "changeset_committed"}
    assert committed == {cs.id for cs in engine.store.changesets()}
    decided = {e["payload"]["candidate_id"] for e in log if e["action"] == "candidate_decided"}
    assert {sc.id for sc in engine.store.candidates() if sc.state.value != "pending"} <= decided
    human = [e for e in log if e["actor"] == "human:alice"]
    assert human and audit.verify(engine.store.conn).ok


def test_append_only_triggers(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    c = engine.store.conn
    for sql in (
        "UPDATE audit_log SET actor='x'",
        "DELETE FROM audit_log",
        "DELETE FROM assertion_versions",
        "UPDATE assertion_versions SET confidence=1",
        "UPDATE evidence SET quote='x'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql)


def test_tampering_is_detected(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    c = engine.store.conn
    assert audit.verify(c).ok
    c.execute("DROP TRIGGER audit_no_update")
    c.execute("UPDATE audit_log SET payload='{}' WHERE seq=3")
    v = audit.verify(c)
    assert not v.ok and v.first_bad_seq == 3


def test_deleted_entry_is_detected(engine) -> None:  # type: ignore[no-untyped-def]
    bootstrap(engine)
    c = engine.store.conn
    c.execute("DROP TRIGGER audit_no_delete")
    c.execute("DELETE FROM audit_log WHERE seq=5")
    assert not audit.verify(c).ok
