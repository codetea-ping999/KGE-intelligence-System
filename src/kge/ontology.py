"""Ontology (schema) definition loaded from YAML."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

LITERAL_TYPES = {"xsd:string", "xsd:integer", "xsd:gYear", "xsd:date"}


class EntityTypeDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subclass_of: str | None = None
    disjoint_with: list[str] = []
    description: str = ""


class PredicateDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str
    range: str
    functional: bool = False
    max_cardinality: int | None = Field(default=None, ge=1)
    temporal: bool = False
    subproperty_of: str | None = None
    inverse_of: str | None = None
    symmetric: bool = False
    transitive: bool = False
    description: str = ""

    @property
    def literal_range(self) -> bool:
        return self.range in LITERAL_TYPES

    @property
    def cardinality_limit(self) -> int | None:
        """Maximum number of distinct values per subject at any point in time."""
        return 1 if self.functional else self.max_cardinality


class RuleDef(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str
    if_: list[str] = Field(alias="if", min_length=1)
    then: str
    confidence_factor: float = Field(default=1.0, gt=0.0, le=1.0)
    #: Set on rules compiled from property axioms rather than written by hand.
    axiom: str | None = None


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
        types, preds = self.entity_types, self.predicates
        for name, t in types.items():
            if t.subclass_of is not None and t.subclass_of not in types:
                raise ValueError(f"entity type {name}: unknown superclass {t.subclass_of}")
            for other in t.disjoint_with:
                if other not in types:
                    raise ValueError(f"entity type {name}: unknown disjoint type {other}")
        for name in types:
            _chain(name, lambda n: types[n].subclass_of, "subclass_of")
        for name in types:
            anc = self.ancestors(name)
            for i, a in enumerate(anc):
                for b in anc[i + 1 :]:
                    if self._declared_disjoint(a, b):
                        raise ValueError(
                            f"entity type {name} is unsatisfiable: {a} and {b} are disjoint"
                        )
        for name, p in preds.items():
            if p.domain not in types:
                raise ValueError(f"predicate {name}: unknown domain {p.domain}")
            if p.range not in types and p.range not in LITERAL_TYPES:
                raise ValueError(f"predicate {name}: unknown range {p.range}")
            if p.functional and p.max_cardinality not in (None, 1):
                raise ValueError(f"predicate {name}: functional implies max_cardinality 1")
            for ref in (p.subproperty_of, p.inverse_of):
                if ref is not None and ref not in preds:
                    raise ValueError(f"predicate {name}: unknown predicate {ref}")
            if (p.inverse_of or p.symmetric or p.transitive) and p.literal_range:
                raise ValueError(
                    f"predicate {name}: inverse/symmetric/transitive need an entity range"
                )
            if p.subproperty_of is not None:
                sup = preds[p.subproperty_of]
                if not self.is_subtype(p.domain, sup.domain) or not (
                    p.range == sup.range or self.is_subtype(p.range, sup.range)
                ):
                    raise ValueError(
                        f"predicate {name}: domain/range must be within those of {p.subproperty_of}"
                    )
            if p.inverse_of is not None:
                inv = preds[p.inverse_of]
                # the axiom derives `?y name ?x` from every `?x inverse_of ?y`, so whatever
                # the inverse may relate has to fit this predicate's domain and range
                if inv.literal_range or not (
                    self.is_subtype(inv.range, p.domain) and self.is_subtype(inv.domain, p.range)
                ):
                    raise ValueError(
                        f"predicate {name}: domain/range must cover the range/domain of "
                        f"{p.inverse_of}"
                    )
                if inv.inverse_of not in (None, name):
                    raise ValueError(
                        f"predicate {name}: {p.inverse_of} declares a different inverse"
                    )
            if p.symmetric and not (
                self.is_subtype(p.range, p.domain) and self.is_subtype(p.domain, p.range)
            ):
                raise ValueError(f"predicate {name}: symmetric needs domain == range")
            if p.transitive and not self.is_subtype(p.range, p.domain):
                raise ValueError(f"predicate {name}: transitive needs range within domain")
        for name in preds:
            _chain(name, lambda n: preds[n].subproperty_of, "subproperty_of")
        ids = {r.id for r in self.rules}
        for r in self.rules:
            if r.axiom is not None:
                raise ValueError(f"rule {r.id}: 'axiom' is reserved for compiled rules")
            for pat in [*r.if_, r.then]:
                _, pred, _ = parse_pattern(pat)
                if pred not in preds:
                    raise ValueError(f"rule {r.id}: unknown predicate {pred}")
        for r in self.axiom_rules():
            if r.id in ids:
                raise ValueError(f"rule id {r.id} is reserved for a property axiom")
        return self

    # -------------------------------------------------------------- class hierarchy

    def ancestors(self, name: str) -> list[str]:
        """The type itself followed by its superclasses, nearest first."""
        out = []
        cur: str | None = name
        while cur is not None and cur in self.entity_types and cur not in out:
            out.append(cur)
            cur = self.entity_types[cur].subclass_of
        return out

    def is_subtype(self, sub: str, sup: str) -> bool:
        return sup in self.ancestors(sub)

    def subtypes(self, name: str) -> list[str]:
        return sorted(t for t in self.entity_types if self.is_subtype(t, name))

    def _declared_disjoint(self, a: str, b: str) -> bool:
        ta, tb = self.entity_types.get(a), self.entity_types.get(b)
        return bool((ta and b in ta.disjoint_with) or (tb and a in tb.disjoint_with))

    def disjoint(self, a: str, b: str) -> bool:
        """Disjointness is inherited: subclasses of disjoint classes are disjoint."""
        return any(
            self._declared_disjoint(x, y) for x in self.ancestors(a) for y in self.ancestors(b)
        )

    def conforms(self, types: list[str], expected: str) -> bool:
        """Whether an entity with ``types`` is an instance of ``expected``."""
        return any(self.is_subtype(t, expected) for t in types)

    # -------------------------------------------------------------- property axioms

    def axiom_rules(self) -> list[RuleDef]:
        """Property characteristics compiled into ordinary rules, so that every entailment
        goes through the reasoner and gets a justification like any other inference."""
        out: list[RuleDef] = []
        for name, p in sorted(self.predicates.items()):
            if p.subproperty_of is not None:
                out.append(
                    RuleDef(
                        id=f"AX_subproperty_{name}",
                        if_=[f"?x {name} ?y"],
                        then=f"?x {p.subproperty_of} ?y",
                        axiom="subproperty_of",
                    )
                )
            if p.inverse_of is not None:
                out.append(
                    RuleDef(
                        id=f"AX_inverse_{p.inverse_of}_{name}",
                        if_=[f"?x {p.inverse_of} ?y"],
                        then=f"?y {name} ?x",
                        axiom="inverse_of",
                    )
                )
            if p.symmetric:
                out.append(
                    RuleDef(
                        id=f"AX_symmetric_{name}",
                        if_=[f"?x {name} ?y"],
                        then=f"?y {name} ?x",
                        axiom="symmetric",
                    )
                )
            if p.transitive:
                out.append(
                    RuleDef(
                        id=f"AX_transitive_{name}",
                        if_=[f"?x {name} ?y", f"?y {name} ?z"],
                        then=f"?x {name} ?z",
                        axiom="transitive",
                    )
                )
        return out

    def all_rules(self) -> list[RuleDef]:
        return [*self.rules, *self.axiom_rules()]

    def rule(self, rule_id: str) -> RuleDef | None:
        return next((r for r in self.all_rules() if r.id == rule_id), None)

    def extractable_predicates(self) -> list[str]:
        """Predicates an extractor should state directly. An inverse property is left out:
        its canonical direction is extracted and the reasoner derives the other one. Of a
        pair declared inverse of each other, the alphabetically first one is canonical."""

        def derived(name: str, p: PredicateDef) -> bool:
            inv = p.inverse_of
            if inv is None:
                return False
            return self.predicates[inv].inverse_of != name or inv < name

        return sorted(n for n, p in self.predicates.items() if not derived(n, p))

    def canonical_json(self) -> str:
        """Stable serialization: the same definitions always give the same text and hash."""
        return json.dumps(
            self.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def load(cls, path: str | Path) -> Ontology:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)


def _chain(start: str, parent: Callable[[str], str | None], what: str) -> None:
    seen = [start]
    cur = parent(start)
    while cur is not None:
        if cur in seen:
            raise ValueError(f"{what} cycle: {' -> '.join([*seen, cur])}")
        seen.append(cur)
        cur = parent(cur)


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
