"""Validation gate: schema, duplicates, contradictions and provenance (DESIGN.md section 3)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Protocol

from kge.models import (
    Assertion,
    Candidate,
    EntityRef,
    EpistemicType,
    Issue,
    LiteralValue,
    Severity,
    SourceKind,
    ValidationReport,
    intervals_overlap,
    is_llm_actor,
    max_concurrent,
    normalize_text,
)
from kge.ontology import Ontology, literal_ok
from kge.store import Store, sha256_text

#: Self-reported LLM confidence is never trusted above this value.
LLM_CONFIDENCE_CAP = 0.7


class Validator:
    def __init__(self, store: Store, ontology: Ontology) -> None:
        self.store = store
        self.ontology = ontology

    def validate(
        self,
        cand: Candidate,
        batch: Sequence[Candidate] = (),
        exclude: set[str] | frozenset[str] = frozenset(),
    ) -> ValidationReport:
        """`batch` holds earlier candidates of the same ingestion; `exclude` holds assertion
        ids that the same changeset is about to retract."""
        issues: list[Issue] = []
        effective_type = cand.epistemic_type
        confidence = cand.confidence
        llm = is_llm_actor(cand.extracted_by)

        def add(code: str, sev: Severity, msg: str, related: list[str] | None = None) -> None:
            issues.append(Issue(code=code, severity=sev, message=msg, related=related or []))

        if cand.epistemic_type is EpistemicType.INFERENCE:
            add(
                "INFERENCE_NOT_ALLOWED",
                Severity.ERROR,
                "inferences are produced by the reasoner, not by extractors",
            )

        pdef = self.ontology.predicates.get(cand.predicate)
        if pdef is None:
            add("SCHEMA_UNKNOWN_PREDICATE", Severity.ERROR, f"unknown predicate {cand.predicate}")
            return ValidationReport(
                issues=issues, effective_type=effective_type, effective_confidence=confidence
            )

        if cand.valid_from and cand.valid_to and cand.valid_from > cand.valid_to:
            add("SCHEMA_INTERVAL", Severity.ERROR, "valid_from is after valid_to")
        if (cand.valid_from or cand.valid_to) and not pdef.temporal:
            add(
                "SCHEMA_NOT_TEMPORAL",
                Severity.WARNING,
                f"{cand.predicate} is not declared temporal",
            )

        # -- terms: types, disjointness, near-duplicate entities
        subj_id = self._check_entity(cand.subject, pdef.domain, "subject", add, batch)
        obj_key: str | None = None
        if pdef.literal_range:
            if not isinstance(cand.object, LiteralValue):
                add("SCHEMA_RANGE", Severity.ERROR, f"{cand.predicate} expects {pdef.range}")
            elif cand.object.datatype != pdef.range:
                add(
                    "SCHEMA_LITERAL_TYPE",
                    Severity.ERROR,
                    f"expected {pdef.range}, got {cand.object.datatype}",
                )
            elif not literal_ok(pdef.range, cand.object.value):
                add(
                    "SCHEMA_LITERAL_TYPE",
                    Severity.ERROR,
                    f"{cand.object.value!r} is not a valid {pdef.range}",
                )
            else:
                obj_key = cand.object.value
        elif not isinstance(cand.object, EntityRef):
            add("SCHEMA_RANGE", Severity.ERROR, f"{cand.predicate} expects a {pdef.range} entity")
        else:
            obj_key = self._check_entity(cand.object, pdef.range, "object", add, batch)

        # -- provenance
        verified_independent = False
        for ev in cand.evidence:
            src = self.store.get_source(ev.source_id, ev.source_version)
            if src is None:
                add(
                    "EVIDENCE_SOURCE_MISSING",
                    Severity.ERROR,
                    f"unknown source {ev.source_id} v{ev.source_version}",
                )
                continue
            if sha256_text(src.content) != src.content_hash:
                add("SOURCE_HASH_MISMATCH", Severity.ERROR, f"source {src.id} content changed")
                continue
            if not ev.quote or normalize_text(ev.quote) not in normalize_text(src.content):
                add(
                    "EVIDENCE_QUOTE_NOT_FOUND",
                    Severity.WARNING,
                    f"quote not found in {src.id} v{src.version}: {ev.quote!r}",
                )
                continue
            if src.kind is not SourceKind.LLM:
                verified_independent = True
        if cand.epistemic_type is EpistemicType.FACT and not verified_independent:
            if llm:
                effective_type = EpistemicType.HYPOTHESIS
                add(
                    "FACT_DOWNGRADED",
                    Severity.WARNING,
                    "LLM claimed a fact without verifiable independent evidence;"
                    " stored as hypothesis",
                )
            else:
                add(
                    "EVIDENCE_MISSING",
                    Severity.ERROR,
                    "a fact needs evidence verified against a non-LLM source",
                )
        if llm and confidence > LLM_CONFIDENCE_CAP:
            confidence = LLM_CONFIDENCE_CAP
            add(
                "CONFIDENCE_CAPPED",
                Severity.INFO,
                f"LLM self-reported confidence capped at {LLM_CONFIDENCE_CAP}",
            )

        # -- duplicates and contradictions against committed knowledge
        duplicate_of: str | None = None
        conflicts: list[str] = []
        supersedes: list[str] = []
        limit = pdef.cardinality_limit
        # overlapping values for the cardinality check: (value label, from, to, assertion id)
        spans: list[tuple[str, dt.date | None, dt.date | None, str | None]] = []
        if subj_id is not None:
            for a in self.store.active(
                subject=subj_id, predicate=cand.predicate, include_hypotheses=True, exclude=exclude
            ):
                overlap = intervals_overlap(
                    a.valid_from, a.valid_to, cand.valid_from, cand.valid_to
                )
                if obj_key is not None and a.object_value == obj_key:
                    same_period = a.valid_from == cand.valid_from and a.valid_to == cand.valid_to
                    if (
                        same_period
                        and a.epistemic_type is not EpistemicType.FACT
                        and effective_type is EpistemicType.FACT
                    ):
                        # merging evidence would leave a verified statement as a hypothesis,
                        # or as an inference that falls with its premises
                        supersedes.append(a.id)
                        add(
                            "PROMOTES_TO_FACT",
                            Severity.INFO,
                            f"verified statement replaces {a.epistemic_type.value} {a.id}",
                            [a.id],
                        )
                    elif same_period:
                        duplicate_of = a.id
                        add(
                            "DUPLICATE_EXACT",
                            Severity.INFO,
                            f"same statement already exists as {a.id}; evidence will be merged",
                            [a.id],
                        )
                elif overlap and limit is not None:
                    spans.append((self._value(a), a.valid_from, a.valid_to, a.id))
                if pdef.functional and overlap and a.object_value != obj_key:
                    conflicts.append(a.id)
                    add(
                        "CONFLICT_FUNCTIONAL",
                        Severity.CONFLICT,
                        f"{cand.predicate} is functional; {a.id} has a different value"
                        f" ({self._show(a)}) in an overlapping period",
                        [a.id],
                    )
        if limit is not None and not pdef.functional and duplicate_of is None:
            # functional predicates are covered by the CONFLICT_FUNCTIONAL checks
            value = cand.key()[2]
            spans += [
                (other.key()[2], other.valid_from, other.valid_to, None)
                for other in batch
                if other.subject.label == cand.subject.label
                and other.predicate == cand.predicate
                and other.key()[2] != value
                and intervals_overlap(
                    other.valid_from, other.valid_to, cand.valid_from, cand.valid_to
                )
            ]
            held = max_concurrent(
                [(v, f, t) for v, f, t, _ in spans if v != value]
                + [(value, cand.valid_from, cand.valid_to)],
                (cand.valid_from, cand.valid_to),
            )
            if held > limit:
                related = sorted({x for *_, x in spans if x is not None})
                conflicts.extend(related)
                add(
                    "CONFLICT_CARDINALITY",
                    Severity.CONFLICT,
                    f"{cand.predicate} allows at most {limit} values per subject;"
                    f" with this one {held} would hold at the same time",
                    related,
                )
        if pdef.functional:
            for other in batch:
                if (
                    other.subject.label == cand.subject.label
                    and other.predicate == cand.predicate
                    and other.key()[2] != cand.key()[2]
                    and intervals_overlap(
                        other.valid_from, other.valid_to, cand.valid_from, cand.valid_to
                    )
                ):
                    add(
                        "CONFLICT_FUNCTIONAL_BATCH",
                        Severity.CONFLICT,
                        f"another candidate in this batch gives {cand.predicate} ="
                        f" {other.key()[2]}",
                    )

        return ValidationReport(
            issues=issues,
            effective_type=effective_type,
            effective_confidence=confidence,
            duplicate_of=duplicate_of,
            conflicts_with=conflicts,
            supersedes=supersedes,
        )

    def _value(self, a: Assertion) -> str:
        """The object as a candidate would name it (entity label or literal value)."""
        return self.store.label(a.object_value) if a.object_kind == "entity" else a.object_value

    def _show(self, a: Assertion) -> str:
        obj = self.store.label(a.object_value) if a.object_kind == "entity" else a.object_value
        return f"{obj}, {a.valid_from or '-∞'}..{a.valid_to or '∞'}"

    def _batch_types(self, label: str, batch: Sequence[Candidate]) -> set[str]:
        """Types earlier candidates of this ingestion give an entity not yet in the store."""
        out: set[str] = set()
        for o in batch:
            pd = self.ontology.predicates.get(o.predicate)
            if pd is None:
                continue
            if o.subject.label == label:
                out.add(o.subject.type or pd.domain)
            if isinstance(o.object, EntityRef) and o.object.label == label:
                out.add(o.object.type or pd.range)
        return out

    def _check_entity(
        self,
        ref: EntityRef,
        implied: str,
        role: str,
        add: _Add,
        batch: Sequence[Candidate] = (),
    ) -> str | None:
        """Returns the id of the existing entity the reference resolves to, if any."""
        for t in sorted(self._batch_types(ref.label, batch)):
            if t != implied and self.ontology.disjoint(t, implied):
                add(
                    "TYPE_DISJOINT",
                    Severity.ERROR,
                    f"{ref.label!r} is {t} earlier in this ingestion, which is disjoint with"
                    f" {implied}",
                )
        if ref.type is not None:
            if ref.type not in self.ontology.entity_types:
                add("SCHEMA_UNKNOWN_TYPE", Severity.ERROR, f"unknown entity type {ref.type}")
            elif not self.ontology.is_subtype(ref.type, implied):
                kind = "SCHEMA_DOMAIN" if role == "subject" else "SCHEMA_RANGE"
                add(kind, Severity.ERROR, f"{role} {ref.label!r} is {ref.type}, expected {implied}")
        existing = self.store.find_entity_by_label(ref.label)
        if existing is None:
            similar = self.store.find_similar_entities(ref.label)
            if similar:
                add(
                    "DUPLICATE_ENTITY_CANDIDATE",
                    Severity.WARNING,
                    f"{ref.label!r} looks like existing "
                    + ", ".join(f"{e.id}:{e.label}" for e in similar)
                    + "; merge manually if they are the same",
                    [e.id for e in similar],
                )
            return None
        wanted = ref.type if ref.type in self.ontology.entity_types else implied
        for t in existing.types:
            if t != wanted and self.ontology.disjoint(t, wanted):
                add(
                    "TYPE_DISJOINT",
                    Severity.ERROR,
                    f"{existing.id}:{existing.label} is {t}, which is disjoint with {wanted}",
                    [existing.id],
                )
        return existing.id


class _Add(Protocol):
    def __call__(
        self, code: str, sev: Severity, msg: str, related: list[str] | None = None
    ) -> None: ...
