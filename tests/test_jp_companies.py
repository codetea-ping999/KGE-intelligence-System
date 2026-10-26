"""Importers for real open data (法人番号 CSV, Wikidata), run on synthetic files of the same
format (tests/data, all fictional)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ROOT
from kge.clock import DEFAULT_FIXED_START, FixedClock
from kge.datasets import HEADER, DatasetExtractor, parse
from kge.extract import ExtractionFailed
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
    with pytest.raises(houjin.MalformedCSV, match="expected 30 columns"):
        houjin.convert("1,9000000000009,too,few,columns\n")
    with pytest.raises(ExtractionFailed, match="expected"):
        e.ingest(
            "bad",
            HEADER + "\nonly\tthree\tfields\n",
            DatasetExtractor(e.ontology, e.store, "import:test"),
            kind=SourceKind.DATASET,
        )
    assert e.store.candidates() == []
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


def test_unreadable_row_aborts_the_reimport(e: Engine, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    from kge.cli import main

    _import(e, "houjin", "houjin_synthetic_v1.csv", "houjin")
    lines = _read("houjin_synthetic_v1.csv").splitlines(keepends=True)
    broken = next(i for i, ln in enumerate(lines) if B in ln)
    lines[broken] = lines[broken].rstrip("\n").rsplit(",", 1)[0] + "\n"  # one cell short
    bad = tmp_path / "bad.csv"
    bad.write_text("".join(lines), "utf-8")
    db = tmp_path / "jp.db"
    base = ["--db", str(db), "--ontology", str(ONTO)]
    assert (
        main([*base, "import", "houjin", str(DATA / "houjin_synthetic_v1.csv"), "--uri", "h"]) == 0
    )
    before = len(Engine.open(db, ONTO).store.active())
    assert main([*base, "import", "houjin", str(bad), "--uri", "h"]) == 2
    assert "nothing imported" in capsys.readouterr().err
    assert len(Engine.open(db, ONTO).store.active()) == before


def test_same_name_different_corporate_number_stay_apart(e: Engine) -> None:
    def row(num: str, pred: str, value: str, otype: str = "xsd:string") -> str:
        return (
            f"corporate_number:{num}\t同名商事株式会社\tCompany\t{pred}\t\t{value}\t{otype}\t\t\t"
        )

    text = "\n".join(
        [
            HEADER,
            row(A, "corporate_number", A),
            row(A, "inception_year", "1950", "xsd:gYear"),
            row(B, "corporate_number", B),
            row(B, "inception_year", "1990", "xsd:gYear"),
        ]
    )
    r = e.ingest("x", text + "\n", DatasetExtractor(e.ontology, e.store, "import:test"))
    assert r.pending == [] and r.rejected == []
    numbers = {
        e.store.label(a.subject): a.object_value
        for a in e.store.active(predicate="corporate_number")
    }
    assert len(numbers) == 2 and set(numbers.values()) == {A, B}
    # a later import of B under the plain name still finds B's entity
    later = DatasetExtractor(e.ontology, e.store, "import:test")._resolver(
        DatasetExtractor(e.ontology, e.store, "import:test")._index()
    )
    assert numbers[later(f"corporate_number:{B}", "同名商事株式会社")] == B


def test_referenced_parent_keeps_its_corporate_number() -> None:
    import json

    def lit(v: str) -> dict[str, str]:
        return {"type": "literal", "value": v}

    binding = {
        "item": {"type": "uri", "value": "http://www.wikidata.org/entity/QTEST9"},
        "itemLabel": lit("架空子会社"),
        "corp": lit(A),
        "prop": lit("P749"),
        "stmt": {"type": "uri", "value": "http://www.wikidata.org/entity/statement/QTEST9-1"},
        "value": {"type": "uri", "value": "http://www.wikidata.org/entity/QTEST8"},
        "valueLabel": lit("架空親会社"),
        "valueCorp": lit(B),  # the parent has no subject rows of its own in this result
    }
    rows, _ = parse(wikidata.convert(json.dumps({"results": {"bindings": [binding]}})))
    numbered = {(r.s_key, r.o_value) for _, r in rows if r.predicate == "corporate_number"}
    assert (f"corporate_number:{B}", B) in numbered


def _with(cells_for_b: dict[int, str]) -> str:
    """The v1 CSV with some cells of corporation B's first row replaced."""
    lines = _read("houjin_synthetic_v1.csv").splitlines(keepends=True)
    i = next(i for i, ln in enumerate(lines) if B in ln)
    cells = lines[i].rstrip("\n").split(",")
    for col, value in cells_for_b.items():
        cells[col] = value
    lines[i] = ",".join(cells) + "\n"
    return "".join(lines)


def test_invalid_date_aborts_the_conversion(e: Engine) -> None:
    _import(e, "houjin", "houjin_synthetic_v1.csv", "houjin")
    before = len(e.store.active())
    bad = _with({houjin.COLUMNS.index("changeDate"): "2015-13-05"})
    with pytest.raises(houjin.MalformedCSV, match="invalid date in changeDate"):
        houjin.convert(bad)
    assert len(e.store.active()) == before  # nothing was withdrawn


def test_bad_date_in_a_canonical_dataset_fails_the_extraction(e: Engine) -> None:
    content = houjin.convert(_read("houjin_synthetic_v1.csv"))
    e.ingest("h", content, DatasetExtractor(e.ontology, e.store, "import:houjin-bangou"),
             kind=SourceKind.DATASET)  # fmt: skip
    before = sorted(a.id for a in e.store.active())
    rows = content.splitlines(keepends=True)
    i = next(i for i, ln in enumerate(rows) if ln.count("\t") == 9 and ln.split("\t")[7])
    cells = rows[i].split("\t")
    cells[7] = "2015-13-05"  # valid_from
    rows[i] = "\t".join(cells)
    with pytest.raises(ExtractionFailed):
        e.ingest("h", "".join(rows), DatasetExtractor(e.ontology, e.store, "import:houjin-bangou"),
                 kind=SourceKind.DATASET)  # fmt: skip
    assert sorted(a.id for a in e.store.active()) == before


def test_sequence_number_breaks_ties_numerically() -> None:
    seq, date = houjin.COLUMNS.index("sequenceNumber"), houjin.COLUMNS.index("changeDate")
    lines = [ln for ln in _read("houjin_synthetic_v1.csv").splitlines() if A in ln][:2]
    assert len(lines) == 2
    first, second = (ln.split(",") for ln in lines)
    first[seq], second[seq] = "9", "10"
    first[date] = second[date] = "2020-01-01"
    rows = parse(houjin.convert(",".join(first) + "\n" + ",".join(second) + "\n"))[0]
    name = houjin.COLUMNS.index("name")
    assert first[name] != second[name]
    labels = {r.s_label for _, r in rows if r.s_key == f"corporate_number:{A}"}
    assert labels == {second[name]}  # the higher sequence number is the latest snapshot
