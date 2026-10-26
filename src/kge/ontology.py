"""Ontology (schema) definition loaded from YAML."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

LITERAL_TYPES = {"xsd:string", "xsd:integer", "xsd:gYear", "xsd:date"}


class EntityTypeDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disjoint_with: list[str] = []


class PredicateDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str
    range: str
    functional: bool = False
    temporal: bool = False
    description: str = ""

    @property
    def literal_range(self) -> bool:
        return self.range in LITERAL_TYPES


class RuleDef(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str
    if_: list[str] = Field(alias="if", min_length=1)
    then: str
    confidence_factor: float = Field(default=1.0, gt=0.0, le=1.0)


Pattern = tuple[str, str, str]


def parse_pattern(text: str) -> Pattern:
    parts = text.split()
    if len(parts) != 3:
        raise ValueError(f"pattern must be 'subject predicate object': {text!r}")
    return parts[0], parts[1], parts[2]


class Ontology(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_types: dict[str, EntityTypeDef]
    predicates: dict[str, PredicateDef]
    rules: list[RuleDef] = []

    @model_validator(mode="after")
    def _consistent(self) -> Ontology:
        for name, p in self.predicates.items():
            if p.domain not in self.entity_types:
                raise ValueError(f"predicate {name}: unknown domain {p.domain}")
            if p.range not in self.entity_types and p.range not in LITERAL_TYPES:
                raise ValueError(f"predicate {name}: unknown range {p.range}")
        for name, t in self.entity_types.items():
            for other in t.disjoint_with:
                if other not in self.entity_types:
                    raise ValueError(f"entity type {name}: unknown disjoint type {other}")
        for r in self.rules:
            for pat in [*r.if_, r.then]:
                _, pred, _ = parse_pattern(pat)
                if pred not in self.predicates:
                    raise ValueError(f"rule {r.id}: unknown predicate {pred}")
        return self

    def disjoint(self, a: str, b: str) -> bool:
        ta, tb = self.entity_types.get(a), self.entity_types.get(b)
        return bool((ta and b in ta.disjoint_with) or (tb and a in tb.disjoint_with))

    @classmethod
    def load(cls, path: str | Path) -> Ontology:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)


_INT = re.compile(r"^-?\d+$")
_YEAR = re.compile(r"^\d{4}$")


def literal_ok(datatype: str, value: str) -> bool:
    if datatype == "xsd:string":
        return True
    if datatype == "xsd:integer":
        return bool(_INT.match(value))
    if datatype == "xsd:gYear":
        return bool(_YEAR.match(value))
    if datatype == "xsd:date":
        try:
            dt.date.fromisoformat(value)
        except ValueError:
            return False
        return True
    return False
