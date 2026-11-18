"""Embedding: public API, interchange format and machine-readable CLI.

The LLM isolation guarantee (AC2) must hold for every entry point, including submissions
from other systems."""

import json
from pathlib import Path

import pytest

from conftest import DOCS, ROOT, doc
from kge import (
    CandidateState,
    EpistemicType,
    InterchangeError,
    KnowledgeGraph,
    ReviewError,
    UnauthorizedCommit,
)
from kge.cli import main
from kge.clock import DEFAULT_FIXED_START, FixedClock
from kge.interchange import json_schema, parse
from kge.models import is_llm_actor
from kge.pipeline import AUTO, accept_op

ONTOLOGY = ROOT / "ontology/base.yaml"
TEXT = "田中アリスはAcme社に勤務している。\nAcme社は日本に所在する。\n"


@pytest.fixture
def kg() -> KnowledgeGraph:
    return KnowledgeGraph.open(":memory:", ONTOLOGY, FixedClock(DEFAULT_FIXED_START))


def submission(statements: list[dict[str, object]], uri: str = "crm:42") -> dict[str, object]:
    return {
        "schema": "kge.interchange",
        "version": 1,
        "kind": "submission",
        "source": {"uri": uri, "title": "CRM note 42", "content": TEXT},
        "statements": statements,
    }


WORKS = {
    "subject": "田中アリス",
    "subject_type": "Person",
    "predicate": "works_for",
    "object": "Acme社",
    "object_type": "Organization",
    "confidence": 0.95,
    "quote": "田中アリスはAcme社に勤務している。",
}


# ------------------------------------------------------------------ public API


def test_facade_covers_ingest_query_and_history(kg: KnowledgeGraph) -> None:
    r = kg.ingest_text("registry", doc("registry_v1.txt"))
    assert r.accepted and not r.pending
    rows = kg.query(subject="田中アリス", predicate="works_for")
    assert [kg.label(a.object_value) for a in rows] == ["Acme社"]
    assert kg.history(rows[0].id)[0].evidence[0].source_id == r.source_id
    assert kg.verify_audit().ok and kg.quality().ok
    head = kg.head()
    kg.retract(rows[0].id, "human:alice", "left the company")
    assert kg.query(subject="田中アリス", predicate="works_for") == []
    assert kg.query(subject="田中アリス", predicate="works_for", as_of_changeset=head)


def test_facade_open_close_and_context_manager(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "kge.db"
    with KnowledgeGraph.open(db, ONTOLOGY) as kg:
        kg.ingest_text("registry", doc("registry_v1.txt"))
        n = len(kg.query())
    with KnowledgeGraph.open(db, ONTOLOGY) as kg:
        assert len(kg.query()) == n


# ------------------------------------------------------------------ submissions keep AC2


def test_llm_submission_waits_for_a_human(kg: KnowledgeGraph) -> None:
    r = kg.submit(submission([WORKS]), system="crm")  # origin defaults to "llm"
    assert r.accepted == [] and r.changesets == [] and len(r.pending) == 1
    sc = kg.candidate(r.pending[0])
    assert sc is not None and sc.candidate.extracted_by == "llm:external/crm"
    assert sc.report is not None and sc.report.effective_confidence == 0.7  # capped
    # neither the policy nor the store will commit it without a human
    with pytest.raises(UnauthorizedCommit):
        kg.store.commit(AUTO, "bypass", [accept_op(sc, sc.report, AUTO)])
    with pytest.raises(ReviewError):
        kg.accept(sc.id, "system:crm", "trust me")
    kg.accept(sc.id, "human:sato", "matches the CRM note")
    (a,) = kg.query(predicate="works_for")
    assert is_llm_actor(a.extracted_by) and a.approved_by == "human:sato"


def test_llm_submission_without_verifiable_quote_is_a_hypothesis(kg: KnowledgeGraph) -> None:
    made_up = {**WORKS, "quote": "田中アリスはAcme社の社長である。"}
    r = kg.submit(submission([made_up]), system="crm")
    sc = kg.candidate(r.pending[0])
    assert sc is not None and sc.report is not None
    assert sc.report.effective_type is EpistemicType.HYPOTHESIS
    assert "FACT_DOWNGRADED" in sc.report.codes()


def test_deterministic_submission_is_validated_like_any_extractor(kg: KnowledgeGraph) -> None:
    no_quote = {**WORKS, "subject": "佐藤ボブ", "quote": ""}
    bad_type = {**WORKS, "predicate": "unknown_predicate"}
    r = kg.submit(submission([WORKS, no_quote, bad_type]), system="erp", origin="deterministic")
    assert len(r.accepted) == 1 and len(r.rejected) == 2
    (a,) = kg.query(predicate="works_for")
    assert a.extracted_by == "external:erp" and a.approved_by == AUTO


def test_submitter_cannot_choose_its_own_identity(kg: KnowledgeGraph) -> None:
    for name in ["human:alice", "llm:x", "rule:ja-registry-v1", "a b", ""]:
        with pytest.raises(InterchangeError):
            kg.submit(submission([WORKS]), system=name)
    with pytest.raises(InterchangeError):
        kg.submit(submission([WORKS]), system="crm", origin="human")  # type: ignore[arg-type]
    smuggled = {**WORKS, "extracted_by": "rule:ja-registry-v1"}
    with pytest.raises(InterchangeError, match="extra"):
        kg.submit(submission([smuggled]), system="crm")
    inference = {**WORKS, "epistemic_type": "inference"}
    with pytest.raises(InterchangeError):
        kg.submit(submission([inference]), system="crm")
    assert kg.candidates(state=None) == []


def test_resubmission_is_incremental(kg: KnowledgeGraph) -> None:
    kg.submit(submission([WORKS]), system="erp", origin="deterministic")
    again = kg.submit(submission([WORKS]), system="erp", origin="deterministic")
    assert again.skipped
    located = {
        "subject": "Acme社",
        "predicate": "located_in",
        "object": "日本",
        "object_type": "Country",
        "confidence": 0.9,
        "quote": "Acme社は日本に所在する。",
    }
    changed = submission([located])
    changed["source"] = {"uri": "crm:42", "content": TEXT + "# rev 2\n"}
    r = kg.submit(changed, system="erp", origin="deterministic")
    assert len(r.accepted) == 1 and len(r.obsolete) == 1 and r.retracted
    assert kg.query(predicate="works_for") == []


# ------------------------------------------------------------------ interchange format


def test_versioned_envelope_is_checked(kg: KnowledgeGraph) -> None:
    for bad, msg in [
        ("not json", "not JSON"),
        ({"schema": "other", "version": 1}, "not a kge.interchange"),
        ({**submission([WORKS]), "version": 2}, "unsupported"),
    ]:
        with pytest.raises(InterchangeError, match=msg):
            kg.submit(bad, system="crm")  # type: ignore[arg-type]
    with pytest.raises(InterchangeError, match="expected a submission"):
        kg.submit(kg.export_knowledge().to_dict(), system="crm")


def test_exports_round_trip_through_the_parser(kg: KnowledgeGraph) -> None:
    kg.ingest_text("registry", doc("registry_v1.txt"))
    kg.submit(submission([WORKS]), system="crm")
    docs = [
        kg.export_knowledge(),
        kg.export_candidates(),
        kg.export_diff(0, kg.head()),
        kg.export_audit(),
    ]
    for d in docs:
        assert parse(d.to_json()) == d
    know = docs[0].to_dict()
    assert know["schema"] == "kge.interchange" and know["version"] == 1
    a = know["assertions"][0]
    assert {"subject_label", "evidence", "valid_from", "recorded_at", "confidence"} <= a.keys()
    assert docs[3].verified and docs[3].entries
    assert {c.state for c in docs[1].candidates} == {CandidateState.PENDING}


def test_committed_json_schema_is_current() -> None:
    committed = json.loads((ROOT / "schemas/kge-interchange-v1.json").read_text("utf-8"))
    assert committed == json_schema(), "run: uv run kge schema > schemas/kge-interchange-v1.json"


# ------------------------------------------------------------------ CLI


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_json_output_and_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KGE_CLOCK", "fixed")
    base = ["--db", str(tmp_path / "k.db"), "--ontology", str(ONTOLOGY)]
    code, out, _ = _run(capsys, *base, "--json", "ingest", str(DOCS / "registry_v1.txt"))
    assert code == 0 and json.loads(out)["accepted"]

    sub = tmp_path / "sub.json"
    sub.write_text(json.dumps(submission([WORKS]), ensure_ascii=False), "utf-8")
    code, out, _ = _run(capsys, *base, "submit", str(sub), "--system", "crm", "--json")
    (cid,) = json.loads(out)["pending"]

    code, out, _ = _run(capsys, *base, "candidates", "--json")
    assert [c["id"] for c in json.loads(out)["candidates"]] == [cid]

    code, out, err = _run(
        capsys, *base, "--json", "accept", cid, "--reviewer", "bot", "--reason", "x"
    )
    assert code == 2 and out == "" and json.loads(err)["error"]["type"] == "ReviewError"
    code, out, _ = _run(
        capsys, *base, "--json", "accept", cid, "--reviewer", "human:sato", "--reason", "ok"
    )
    assert code == 0 and json.loads(out)["accepted"] == cid

    for argv in (["query"], ["diff", "0", "3"], ["audit", "log"], ["log"], ["export"]):
        code, out, _ = _run(capsys, *base, "--json", *argv)
        assert code == 0
        json.loads(out)  # every --json output is one JSON document
    code, out, _ = _run(capsys, *base, "--json", "query")
    assert parse(out).kind == "knowledge"  # type: ignore[attr-defined]

    code, out, err = _run(capsys, *base, "--json", "show", "C9999")
    assert code == 1 and json.loads(err)["error"]["type"] == "NotFound"
    code, _, err = _run(
        capsys, *base, "--json", "submit", str(tmp_path / "missing.json"), "--system", "x"
    )
    assert code == 2 and "error" in json.loads(err)
    bad = tmp_path / "bad.json"
    bad.write_text("{}", "utf-8")
    code, _, err = _run(capsys, *base, "submit", str(bad), "--system", "x")
    assert code == 2 and err.startswith("error:")
    code, out, _ = _run(capsys, *base, "--json", "audit", "verify")
    assert code == 0 and json.loads(out)["ok"] is True
