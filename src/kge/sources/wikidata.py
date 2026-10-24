"""Wikidata (CC0) → canonical dataset.

``query(corporate_numbers)`` builds a SPARQL query for the Wikidata Query Service; its JSON
result (the W3C "SPARQL 1.1 Query Results JSON Format") is what ``convert`` reads. One result
row is one Wikidata statement, with its start/end qualifiers and its statement IRI, which
becomes the evidence locator. Deprecated statements are excluded by the query.

Companies are matched by Japanese corporate number (P3225), so they link to records imported
from the National Tax Agency whatever label each source uses.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from kge.datasets import Row, render

ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "kge-intelligence-system/0.1 (research PoC; https://github.com/codetea-ping999/KGE-intelligence-System)"
LICENSE = "Wikidata, CC0 1.0"

#: Wikidata property → (predicate, object type or datatype)
PROPERTIES: dict[str, tuple[str, str]] = {
    "P1448": ("legal_name", "xsd:string"),  # official name (Japanese only)
    "P159": ("headquarters_in", "Municipality"),  # headquarters location
    "P749": ("parent_organization", "Organization"),  # parent organization
    "P169": ("ceo", "Person"),  # chief executive officer
    "P571": ("inception_year", "xsd:gYear"),  # inception
}

_QUERY = """\
SELECT ?item ?itemLabel ?corp ?prop ?stmt ?value ?valueLabel ?valueCorp
       ?valuePref ?valuePrefLabel ?start ?end WHERE {{
  VALUES ?corp {{ {corps} }}
  ?item wdt:P3225 ?corp .
  VALUES (?prop ?p ?ps) {{ {props} }}
  ?item ?p ?stmt .
  ?stmt ?ps ?value ; wikibase:rank ?rank .
  FILTER(?rank != wikibase:DeprecatedRank)
  OPTIONAL {{ ?stmt pq:P580 ?start }}
  OPTIONAL {{ ?stmt pq:P582 ?end }}
  OPTIONAL {{ ?value wdt:P3225 ?valueCorp }}
  OPTIONAL {{ ?value wdt:P131* ?valuePref . ?valuePref wdt:P31 wd:Q50337 }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "ja,en" . }}
}}
ORDER BY ?corp ?prop ?stmt
"""


def query(corporate_numbers: list[str]) -> str:
    corps = " ".join(f'"{n}"' for n in sorted(set(corporate_numbers)) if n.isdigit())
    if not corps:
        raise ValueError("give at least one 13-digit corporate number")
    props = " ".join(f'("{p}" p:{p} ps:{p})' for p in sorted(PROPERTIES))
    return _QUERY.format(corps=corps, props=props)


def fetch(corporate_numbers: list[str], timeout: float = 60.0) -> str:
    """Run the query against the public endpoint (needs network access to wikidata.org)."""
    url = (
        ENDPOINT
        + "?"
        + urllib.parse.urlencode({"query": query(corporate_numbers), "format": "json"})
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https URL)
        return str(resp.read().decode("utf-8"))


def _v(b: dict[str, Any], name: str) -> str:
    cell = b.get(name)
    return str(cell["value"]) if cell else ""


def _qid(iri: str) -> str:
    return iri.rsplit("/", 1)[-1]


def _date(value: str) -> str:
    """Wikidata time literal → ISO date; only day-precision-looking values are kept."""
    if not value or value.startswith("-"):
        return ""
    day = value[:10]
    return "" if day.endswith("-00") or day[5:7] == "00" else day


def convert(results_json: str) -> str:
    data = json.loads(results_json)
    rows: list[Row] = []
    skipped: list[str] = []
    for b in data["results"]["bindings"]:
        prop, stmt = _v(b, "prop"), _v(b, "stmt")
        if prop not in PROPERTIES:
            continue
        predicate, otype = PROPERTIES[prop]
        corp = _v(b, "corp")
        subject = dict(
            s_key=f"corporate_number:{corp}", s_label=_v(b, "itemLabel"), s_type="Company"
        )
        rows.append(
            Row(
                **subject,
                predicate="corporate_number",
                o_key="",
                o_value=corp,
                o_type="xsd:string",
                locator=f"wikidata:{_qid(_v(b, 'item'))}",
            )
        )
        value, label = _v(b, "value"), _v(b, "valueLabel")
        when = dict(valid_from=_date(_v(b, "start")), valid_to=_date(_v(b, "end")))
        loc = f"wikidata:{_qid(stmt)}" if stmt else f"wikidata:{_qid(_v(b, 'item'))}"
        if predicate == "legal_name":
            if b.get("value", {}).get("xml:lang") != "ja":
                continue
            rows.append(
                Row(
                    **subject,
                    predicate=predicate,
                    o_key="",
                    o_value=value,
                    o_type=otype,
                    **when,
                    locator=loc,
                )
            )
        elif predicate == "inception_year":
            year = value[:4]
            if not year.isdigit():
                skipped.append(f"{loc}: unusable inception {value!r}")
                continue
            rows.append(
                Row(
                    **subject,
                    predicate=predicate,
                    o_key="",
                    o_value=year,
                    o_type=otype,
                    valid_from="",
                    valid_to="",
                    locator=loc,
                )
            )
        elif predicate == "headquarters_in":
            pref = _v(b, "valuePrefLabel")
            if not pref:
                skipped.append(f"{loc}: headquarters {label} is not in a Japanese prefecture")
                continue
            if label == pref:
                skipped.append(f"{loc}: headquarters given only as the prefecture {pref}")
                continue
            place = f"{pref}{label}"
            rows.append(
                Row(
                    **subject,
                    predicate=predicate,
                    o_key="",
                    o_value=place,
                    o_type=otype,
                    **when,
                    locator=loc,
                )
            )
            rows.append(
                Row(
                    s_key="",
                    s_label=place,
                    s_type="Municipality",
                    predicate="located_in_prefecture",
                    o_key="",
                    o_value=pref,
                    o_type="Prefecture",
                    locator=loc,
                )
            )
        else:  # parent_organization, ceo
            vcorp = _v(b, "valueCorp")
            okey = f"corporate_number:{vcorp}" if vcorp else ""
            if vcorp:  # so a later registry import links to this entity, whatever its label
                rows.append(
                    Row(
                        s_key=okey,
                        s_label=label,
                        s_type="Company",
                        predicate="corporate_number",
                        o_key="",
                        o_value=vcorp,
                        o_type="xsd:string",
                        locator=f"wikidata:{_qid(value)}",
                    )
                )
            rows.append(
                Row(
                    **subject,
                    predicate=predicate,
                    o_key=okey,
                    o_value=label,
                    o_type="Company" if vcorp else otype,
                    **when,
                    locator=loc,
                )
            )
    return render(rows, [f"source: {ENDPOINT} ({LICENSE})", *skipped])
