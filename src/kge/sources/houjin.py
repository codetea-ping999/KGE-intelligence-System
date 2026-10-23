"""National Tax Agency corporate number data (国税庁 法人番号公表サイト) → canonical dataset.

Reads the downloadable CSV (Unicode edition, no header row). Column positions follow the
published resource definition; they match the element names of the Web-API, listed in
``COLUMNS``. Each row is a snapshot of one corporation's name and registered head office as
of its change date (変更年月日). When a file holds several rows for the same corporation
(history data), consecutive snapshots become consecutive validity periods; a closure date
(登記記録の閉鎖等年月日) ends the last one.

Terms of use: 国税庁法人番号公表サイト「利用規約」(政府標準利用規約準拠). Attribute the source;
commercial use is permitted. Rows flagged 検索対象除外 (hihyoji = 1) are skipped.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections import defaultdict

from kge.datasets import Row, render

LICENSE = "出典: 国税庁法人番号公表サイト（国税庁）"

COLUMNS = [
    "sequenceNumber",
    "corporateNumber",
    "process",
    "correct",
    "updateDate",
    "changeDate",
    "name",
    "nameImageId",
    "kind",
    "prefectureName",
    "cityName",
    "streetNumber",
    "addressImageId",
    "prefectureCode",
    "cityCode",
    "postCode",
    "addressOutside",
    "addressOutsideImageId",
    "closeDate",
    "closeCause",
    "successorCorporateNumber",
    "changeCause",
    "assignmentDate",
    "latest",
    "enName",
    "enPrefectureName",
    "enCityName",
    "enAddressOutside",
    "furigana",
    "hihyoji",
]

#: 法人種別 codes for companies; every other kind is imported as an Organization
COMPANY_KINDS = {"301", "302", "303", "304", "305"}  # 株式会社, 有限会社, 合名, 合資, 合同


def _day_before(iso: str) -> str:
    return (dt.date.fromisoformat(iso) - dt.timedelta(days=1)).isoformat()


def convert(csv_text: str) -> str:
    records: dict[str, list[dict[str, str]]] = defaultdict(list)
    skipped: list[str] = []
    for n, cells in enumerate(csv.reader(io.StringIO(csv_text)), start=1):
        if not cells:
            continue
        if len(cells) != len(COLUMNS):
            skipped.append(f"row {n}: expected {len(COLUMNS)} columns, got {len(cells)}")
            continue
        rec = dict(zip(COLUMNS, cells, strict=True))
        if rec["hihyoji"] == "1":
            skipped.append(f"row {n}: excluded from search (hihyoji)")
            continue
        if not (rec["corporateNumber"].isdigit() and len(rec["corporateNumber"]) == 13):
            skipped.append(f"row {n}: invalid corporate number")
            continue
        records[rec["corporateNumber"]].append(rec)

    rows: list[Row] = []
    for number, recs in sorted(records.items()):
        recs.sort(key=lambda r: (r["changeDate"] or r["assignmentDate"], r["sequenceNumber"]))
        latest = recs[-1]
        stype = "Company" if latest["kind"] in COMPANY_KINDS else "Organization"
        subject = dict(s_key=f"corporate_number:{number}", s_label=latest["name"], s_type=stype)
        loc = f"houjin:{number}"
        rows.append(
            Row(
                **subject,
                predicate="corporate_number",
                o_key="",
                o_value=number,
                o_type="xsd:string",
                locator=loc,
            )
        )
        close = latest["closeDate"]
        for field, predicate in (("name", "legal_name"), ("office", "registered_office_in")):
            spans: list[tuple[str, str]] = []  # (value, valid_from), consecutive values merged
            for r in recs:
                if field == "name":
                    value = r["name"]
                elif r["addressOutside"] or not (r["prefectureName"] and r["cityName"]):
                    value = ""  # foreign or unknown head office
                else:
                    value = f"{r['prefectureName']}{r['cityName']}"
                start = r["changeDate"] or r["assignmentDate"]
                if not spans or spans[-1][0] != value:
                    spans.append((value, start))
            for i, (value, start) in enumerate(spans):
                end = _day_before(spans[i + 1][1]) if i + 1 < len(spans) else close
                if not value:
                    continue
                otype = "xsd:string" if predicate == "legal_name" else "Municipality"
                rows.append(
                    Row(
                        **subject,
                        predicate=predicate,
                        o_key="",
                        o_value=value,
                        o_type=otype,
                        valid_from=start,
                        valid_to=end,
                        locator=loc,
                    )
                )
        for r in recs:
            if r["prefectureName"] and r["cityName"] and not r["addressOutside"]:
                rows.append(
                    Row(
                        s_key="",
                        s_label=f"{r['prefectureName']}{r['cityName']}",
                        s_type="Municipality",
                        predicate="located_in_prefecture",
                        o_key="",
                        o_value=r["prefectureName"],
                        o_type="Prefecture",
                        locator=loc,
                    )
                )
    return render(rows, [LICENSE, *skipped])
