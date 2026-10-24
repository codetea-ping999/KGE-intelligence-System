"""AC10: deterministic, offline runs. AC11: QA gate with golden regression."""

import json
import shutil
from pathlib import Path

from conftest import ROOT
from kge import demo, qa
from kge.cli import main
from kge.clock import DEFAULT_FIXED_START


def _run() -> list[str]:
    lines: list[str] = []
    demo.run(emit=lines.append)
    return lines


def test_demo_is_deterministic() -> None:
    assert _run() == _run()


def test_demo_matches_golden_and_passes_qa() -> None:
    e = demo.run(emit=lambda _: None)
    report = qa.run(e.store, DEFAULT_FIXED_START)
    report.checks.append(
        qa.golden_check(qa.export_snapshot(e.store), ROOT / "fixtures/golden/demo_snapshot.json")
    )
    assert report.ok, [c for c in report.checks if not c.ok]
    assert report.metrics["candidates"]["pending"] == 0


def test_qa_cli_exit_codes(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(ROOT)
    db = str(tmp_path / "none.db")
    assert main(["--db", db, "qa"]) == 0
    bad = tmp_path / "golden.json"
    data = json.loads((ROOT / "fixtures/golden/demo_snapshot.json").read_text("utf-8"))
    bad.write_text(json.dumps(data[1:], ensure_ascii=False), "utf-8")
    assert main(["--db", db, "qa", "--golden", str(bad)]) == 1


def test_qa_detects_fact_without_evidence() -> None:
    e = demo.run(emit=lambda _: None)
    c = e.store.conn
    c.execute("DROP TRIGGER ev_no_change")
    c.execute("DELETE FROM evidence WHERE assertion_id='A00001'")
    failed = {ch.name: ch.detail for ch in qa.checks(e.store) if not ch.ok}
    assert failed["facts_have_evidence"] == "A00001"
    assert "model_integrity" in failed


def test_cli_workflow(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("KGE_CLOCK", "fixed")
    db = str(tmp_path / "kge.db")
    docs = tmp_path / "docs"
    shutil.copytree(ROOT / "fixtures/docs", docs)
    assert main(["--db", db, "ingest", str(docs / "registry_v1.txt"), "--uri", "registry"]) == 0
    assert main(["--db", db, "ingest", str(docs / "news_2026.txt"), "--extractor", "llm"]) == 0
    capsys.readouterr()
    assert main(["--db", db, "candidates"]) == 0
    pending = [line.split()[0] for line in capsys.readouterr().out.splitlines()]
    assert len(pending) == 3
    assert (
        main(["--db", db, "accept", pending[1], "--reviewer", "policy:auto", "--reason", "x"]) == 2
    )
    assert (
        main(["--db", db, "accept", pending[0], "--reviewer", "human:alice", "--reason", "checked"])
        == 0
    )
    assert (
        main(
            [
                "--db",
                db,
                "reject",
                pending[1],
                "--reviewer",
                "human:alice",
                "--reason",
                "unsupported",
            ]
        )
        == 0
    )
    assert main(["--db", db, "ingest", str(docs / "registry_v2.txt"), "--uri", "registry"]) == 0
    for cmd in (
        ["query", "--hypotheses"],
        ["log"],
        ["diff", "1", "3"],
        ["impact", "A00001"],
        ["show", "A00003"],
        ["show", pending[0]],
        ["export"],
        ["export", "--format", "turtle"],
        ["audit", "log"],
    ):
        assert main(["--db", db, *cmd]) == 0, cmd
    assert main(["--db", db, "audit", "verify"]) == 0
    assert main(["--db", db, "qa"]) == 0
    snap = Path(db).with_name("snapshot.json")
    assert main(["--db", db, "export", "--out", str(snap)]) == 0
    assert json.loads(snap.read_text("utf-8"))  # written to the file, not stdout
