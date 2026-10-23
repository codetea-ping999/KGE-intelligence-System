"""Importers for real open data (法人番号 CSV, Wikidata), run on synthetic files of the same
format (tests/data, all fictional)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ROOT
from kge.clock import DEFAULT_FIXED_START, FixedClock
from kge.datasets import HEADER, DatasetExtractor, parse
from kge.models import CandidateState, SourceKind
from kge.pipeline import Engine
from kge.qa import checks
from kge.sources import houjin, wikidata

DATA = ROOT / "tests/data"
ONTO = ROOT / "ontology/jp_companies.yaml"
A, B = "9000000000001", "9000000000002"


def _read(name: str) -> str:
    return (DATA / name).read_text(encoding="utf-8")


def _import(e: Engine, kind: str, name: str, uri: str):  # type: ignore[no-untyped-def]
    if kind == "houjin":
        content, ident, prior = houjin.convert(_read(name)), "import:houjin-bangou", 0.98
    else:
        content, ident, prior = wikidata.convert(_read(name)), "import:wikidata", 0.9
    return e.ingest(
        uri, content, DatasetExtractor(e.ontology, e.store, ident, prior), kind=SourceKind.DATASET
    )


@pytest.fixture
def e() -> Engine:
    return Engine.open(":memory:", ONTO, FixedClock(DEFAULT_FIXED_START))


def _facts(e: Engine, predicate: str) -> set[tuple[str, str, str, str]]:
    out = set()
    for a in e.store.active(predicate=predicate, include_hypotheses=True):
        obj = e.store.label(a.object_value) if a.object_kind == "entity" else a.object_value
        out.add((e.store.label(a.subject), obj, str(a.valid_from or ""), str(a.valid_to or "")))
    return out


def test_houjin_history_becomes_validity_periods(e: Engine) -> None:
    text = houjin.convert(_read("houjin_synthetic_v1.csv"))
    rows, invalid = parse(text)
    assert text.startswith(HEADER) and invalid == []
    assert "# row 5: excluded from search (hihyoji)" in text  # 検索対象除外 is honoured
    assert all(A in r.s_key or B in r.s_key or not r.s_key for _, r in rows)
    r = _import(e, "houjin", "houjin_synthetic_v1.csv", "houjin")
    assert r.rejected == [] and r.pending == []
    hd = "架空ホールディングス株式会社"
    assert _facts(e, "legal_name") >= {
        (hd, "架空精機株式会社", "2015-10-05", "2018-03-31"),
        (hd, hd, "2018-04-01", ""),
    }
    # the unchanged address across the name change is one period, not two
    assert _facts(e, "registered_office_in") >= {
        (hd, "愛知県豊田市", "2015-10-05", "2020-09-30"),
        (hd, "東京都千代田区", "2020-10-01", ""),
    }
    assert (hd, "東京都", "2020-10-01", "") in _facts(e, "registered_prefecture")
    for a in e.store.active():
        if a.justification is not None:
            continue  # inferences cite premises, not sources
        assert a.evidence[0].locator and a.evidence[0].locator.startswith("houjin:")
    assert all(c.ok for c in checks(e.store))


def test_wikidata_links_by_corporate_number_and_stays_gated(e: Engine) -> None:
    _import(e, "houjin", "houjin_synthetic_v1.csv", "houjin")
    entities = len(e.store.entities())
    r = _import(e, "wikidata", "wikidata_synthetic.json", "wikidata")
    hd = "架空ホールディングス株式会社"
    # "架空HD" (the Wikidata label) resolved to the entity holding the same corporate number
    assert e.store.find_entity_by_label("架空HD") is None
    assert len(e.store.entities()) == entities + 2  # only the two CEOs are new
    [corp] = e.store.active(
        predicate="corporate_number", subject=e.store.find_entity_by_label(hd).id
    )  # type: ignore[union-attr]
    assert {ev.locator for ev in corp.evidence} == {f"houjin:{A}", "wikidata:QTEST1"}
    assert ("架空部品株式会社", hd, "2018-04-01", "") in _facts(e, "parent_organization")
    assert (hd, "架空部品株式会社", "2018-04-01", "") in _facts(e, "subsidiary")  # inverse
    assert ("架空部品株式会社", hd, "2018-04-01", "") in _facts(e, "in_group_of")  # sub-property
    assert {x[1] for x in _facts(e, "ceo")} == {"山田架空", "鈴木試験"}
    assert (hd, "1950", "", "") in _facts(e, "inception_year")
    # Wikidata gives the official name without a start date, so it overlaps the old name:
    # a conflict for review, not a silent overwrite
    [pending] = [e.store.get_candidate(c) for c in r.pending]
    assert pending is not None and pending.candidate.predicate == "legal_name"
    assert "CONFLICT_FUNCTIONAL" in pending.report.codes()  # type: ignore[union-attr]
    text = wikidata.convert(_read("wikidata_synthetic.json"))
    assert "not in a Japanese prefecture" in text and "Kaku Holdings" not in text
    assert all(c.ok for c in checks(e.store))


def test_new_houjin_snapshot_is_an_incremental_update(e: Engine) -> None:
    _import(e, "houjin", "houjin_synthetic_v1.csv", "houjin")
    _import(e, "wikidata", "wikidata_synthetic.json", "wikidata")
    r = _import(e, "houjin", "houjin_synthetic_v2.csv", "houjin")
    parts = "架空部品株式会社"
    assert len(r.retracted) >= 2  # the open-ended office and the inference built on it
    assert _facts(e, "registered_office_in") >= {
        (parts, "愛知県刈谷市", "2015-10-05", "2023-12-31"),
        (parts, "愛知県名古屋市中村区", "2024-01-01", ""),
    }
    assert (parts, "愛知県", "2024-01-01", "") in _facts(e, "registered_prefecture")
    assert r.unchanged > 0  # untouched statements are not re-staged
    # facts from the other source are not touched by this source's update
    assert ("架空部品株式会社", "架空ホールディングス株式会社", "2018-04-01", "") in _facts(
        e, "parent_organization"
    )
    assert all(c.ok for c in checks(e.store))


def test_malformed_input_is_reported_not_imported(e: Engine) -> None:
    text = houjin.convert("1,9000000000009,too,few,columns\n")
    assert "expected 30 columns" in text
    r = e.ingest(
        "bad",
        HEADER + "\nonly\tthree\tfields\n",
        DatasetExtractor(e.ontology, e.store, "import:test"),
        kind=SourceKind.DATASET,
    )
    assert r.invalid == 1 and r.staged == []
    with pytest.raises(ValueError, match="not a kge dataset"):
        parse("hello")


def test_wikidata_query_mentions_only_requested_numbers() -> None:
    q = wikidata.query([B, A, A, "not-a-number"])
    assert f'"{A}" "{B}"' in q and "not-a-number" not in q
    for prop in wikidata.PROPERTIES:
        assert f"p:{prop} ps:{prop}" in q
    with pytest.raises(ValueError):
        wikidata.query([])


def test_cli_import(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from kge.cli import main

    monkeypatch.setenv("KGE_CLOCK", "fixed")
    base = ["--db", str(tmp_path / "jp.db"), "--ontology", str(ONTO)]
    canonical = tmp_path / "h.tsv"
    assert main([*base, "import", "houjin", str(DATA / "houjin_synthetic_v1.csv"),
                 "--canonical-out", str(canonical)]) == 0  # fmt: skip
    assert canonical.read_text("utf-8").startswith(HEADER)
    assert main([*base, "import", "wikidata", str(DATA / "wikidata_synthetic.json")]) == 0
    out = capsys.readouterr().out
    assert "要レビュー 1" in out
    assert main([*base, "candidates", "--state", "pending"]) == 0
    assert "legal_name" in capsys.readouterr().out
    assert main([*base, "import", "houjin", str(tmp_path / "missing.csv")]) == 2
    pending = Engine.open(tmp_path / "jp.db", ONTO).store.candidates(state=CandidateState.PENDING)
    assert len(pending) == 1
