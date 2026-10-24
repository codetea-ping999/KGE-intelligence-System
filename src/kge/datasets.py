"""A canonical, line-oriented form for statements imported from structured datasets.

Importers (``kge.sources.*``) convert an external dataset into this text, and the text is
what gets registered as the source. Every candidate then quotes its own line, so the
evidence check (quote found in the hashed source) works for datasets exactly as it does for
documents, and incremental re-imports diff line by line.

Format: a ``#kge-dataset v1`` header, ``#`` comment lines (provenance, skipped rows), then one
tab-separated statement per line::

    s_key  s_label  s_type  predicate  o_key  o_value  o_type  valid_from  valid_to  locator

``o_type`` is an entity type, or an ``xsd:`` datatype for a literal. ``*_key`` optionally
names an identifier (``corporate_number:<13 digits>``) used to link to an entity already in
the store whatever its label is. Empty fields are empty strings.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from kge.extract import ExtractionResult
from kge.models import Candidate, EntityRef, EpistemicType, Evidence, LiteralValue, Source
from kge.ontology import Ontology
from kge.store import Store

HEADER = "#kge-dataset v1"
COLUMNS = [
    "s_key",
    "s_label",
    "s_type",
    "predicate",
    "o_key",
    "o_value",
    "o_type",
    "valid_from",
    "valid_to",
    "locator",
]
#: identifier kinds a key may use, and the literal predicate that carries them in the store
KEY_PREDICATES = {"corporate_number": "corporate_number"}


@dataclass(frozen=True)
class Row:
    s_key: str
    s_label: str
    s_type: str
    predicate: str
    o_key: str
    o_value: str
    o_type: str
    valid_from: str = ""
    valid_to: str = ""
    locator: str = ""

    def line(self) -> str:
        return "\t".join(_clean(getattr(self, c)) for c in COLUMNS)


def _clean(value: str) -> str:
    return " ".join(value.replace("\t", " ").split())


def render(rows: Iterable[Row], comments: Iterable[str] = ()) -> str:
    """Deterministic text: comments first, then statements in sorted order, duplicates
    dropped, so the same dataset always produces the same source hash."""
    lines = [HEADER, *(f"# {_clean(c)}" for c in comments)]
    lines += sorted({r.line() for r in rows})
    return "\n".join(lines) + "\n"


def parse(text: str) -> tuple[list[tuple[int, Row]], list[dict[str, Any]]]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != HEADER:
        raise ValueError(f"not a kge dataset: first line must be {HEADER!r}")
    rows: list[tuple[int, Row]] = []
    invalid: list[dict[str, Any]] = []
    for n, line in enumerate(lines[1:], start=2):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != len(COLUMNS):
            invalid.append(
                {"line": n, "error": f"expected {len(COLUMNS)} fields, got {len(parts)}"}
            )
            continue
        rows.append((n, Row(*parts)))
    return rows, invalid


def _date(value: str) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


class DatasetExtractor:
    """Turns a canonical dataset into candidates. Deterministic; nothing here writes."""

    def __init__(self, ontology: Ontology, store: Store, ident: str, prior: float = 0.9) -> None:
        self.ontology = ontology
        self.store = store
        self.id = ident
        self.source_prior = prior

    def _index(self) -> dict[str, dict[str, str]]:
        """identifier kind -> value -> label of the entity holding it, read once per run."""
        out: dict[str, dict[str, str]] = {}
        for kind, pred in KEY_PREDICATES.items():
            out[kind] = {
                a.object_value: self.store.label(a.subject)
                for a in self.store.active(predicate=pred)
            }
        return out

    @staticmethod
    def _resolver(index: dict[str, dict[str, str]]) -> Callable[[str, str], str]:
        """Maps (key, label) to the label to use. An identifier already in the store links to
        its entity whatever the label; a new identifier whose label is taken by an entity with
        a different identifier gets a label naming the identifier, so two companies that share
        a name never merge."""
        taken = {
            label: (kind, value) for kind, vals in index.items() for value, label in vals.items()
        }
        assigned: dict[tuple[str, str], str] = {}

        def resolve(key: str, label: str) -> str:
            if not key:
                return label
            kind, _, value = key.partition(":")
            if kind not in index:
                return label
            if value in index[kind]:
                return index[kind][value]
            if (kind, value) not in assigned:
                owner = taken.get(label)
                chosen = label if owner in (None, (kind, value)) else f"{label}［{kind}:{value}］"
                taken[chosen] = (kind, value)
                assigned[(kind, value)] = chosen
            return assigned[(kind, value)]

        return resolve

    def extract(self, source: Source) -> ExtractionResult:
        rows, invalid = parse(source.content)
        resolve = self._resolver(self._index())
        out: list[Candidate] = []
        for n, r in rows:
            try:
                obj: EntityRef | LiteralValue
                if r.o_type.startswith("xsd:"):
                    obj = LiteralValue(value=r.o_value, datatype=r.o_type)
                else:
                    obj = EntityRef(label=resolve(r.o_key, r.o_value), type=r.o_type or None)
                out.append(
                    Candidate(
                        subject=EntityRef(label=resolve(r.s_key, r.s_label), type=r.s_type or None),
                        predicate=r.predicate,
                        object=obj,
                        epistemic_type=EpistemicType.FACT,
                        confidence=self.source_prior,
                        confidence_method="source_prior",
                        valid_from=_date(r.valid_from),
                        valid_to=_date(r.valid_to),
                        evidence=[
                            Evidence(
                                source_id=source.id,
                                source_version=source.version,
                                quote=r.line(),
                                locator=r.locator or f"line:{n}",
                            )
                        ],
                        extracted_by=self.id,
                    )
                )
            except ValueError as e:
                invalid.append({"line": n, "error": str(e).splitlines()[0]})
        return ExtractionResult(out, invalid)
