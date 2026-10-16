"""Core data model. See DESIGN.md section 2."""

from __future__ import annotations

import datetime as dt
import unicodedata
from enum import IntEnum, StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EpistemicType(StrEnum):
    FACT = "fact"
    INFERENCE = "inference"
    HYPOTHESIS = "hypothesis"


class Status(StrEnum):
    """Lifecycle of committed knowledge. Candidates live in staging, not here."""

    ACCEPTED = "accepted"
    RETRACTED = "retracted"
    SUPERSEDED = "superseded"


class CandidateState(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    OBSOLETE = "obsolete"


class SourceKind(StrEnum):
    DOCUMENT = "document"
    DATASET = "dataset"
    HUMAN = "human"
    LLM = "llm"


class Severity(IntEnum):
    INFO = 0
    WARNING = 1
    CONFLICT = 2
    ERROR = 3


def normalize_text(s: str) -> str:
    """NFKC, case-folded, whitespace removed. Used for matching, never for storage."""
    return "".join(unicodedata.normalize("NFKC", s).casefold().split())


def is_llm_actor(actor: str) -> bool:
    return actor.startswith("llm:")


def is_human_actor(actor: str) -> bool:
    return actor.startswith("human:")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityRef(_Model):
    kind: Literal["entity"] = "entity"
    label: str = Field(min_length=1)
    type: str | None = None


class LiteralValue(_Model):
    kind: Literal["literal"] = "literal"
    value: str
    datatype: str


ObjectValue = Annotated[EntityRef | LiteralValue, Field(discriminator="kind")]


class Evidence(_Model):
    source_id: str
    source_version: int
    quote: str
    locator: str | None = None


class Justification(_Model):
    rule_id: str
    premise_ids: list[str] = Field(min_length=1)


class Entity(_Model):
    id: str
    label: str
    types: list[str]
    aliases: list[str] = []


class Source(_Model):
    id: str
    version: int
    uri: str
    title: str
    kind: SourceKind
    content: str
    content_hash: str
    retrieved_at: dt.datetime


class Candidate(_Model):
    """What an extractor proposes. Never knowledge until reviewed and committed."""

    subject: EntityRef
    predicate: str
    object: ObjectValue
    epistemic_type: EpistemicType = EpistemicType.FACT
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_method: str
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    evidence: list[Evidence] = []
    extracted_by: str

    def key(self) -> tuple[str, str, str, str, str]:
        obj = self.object.label if isinstance(self.object, EntityRef) else self.object.value
        return (
            self.subject.label,
            self.predicate,
            obj,
            self.valid_from.isoformat() if self.valid_from else "",
            self.valid_to.isoformat() if self.valid_to else "",
        )


class Assertion(_Model):
    """A committed, versioned statement (one version row in the store)."""

    id: str
    subject: str  # entity id
    predicate: str
    object_value: str  # entity id or literal lexical form
    object_kind: Literal["entity", "literal"]
    object_datatype: str | None = None
    epistemic_type: EpistemicType
    status: Status
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_method: str
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    recorded_at: dt.datetime
    superseded_at: dt.datetime | None = None
    extracted_by: str
    approved_by: str
    candidate_id: str | None = None
    changeset: int
    evidence: list[Evidence] = []
    justification: Justification | None = None

    @model_validator(mode="after")
    def _invariants(self) -> Assertion:
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("valid_from must not be after valid_to")
        if self.epistemic_type is EpistemicType.FACT and not self.evidence:
            raise ValueError("a fact requires at least one evidence")
        if self.epistemic_type is EpistemicType.INFERENCE and self.justification is None:
            raise ValueError("an inference requires a justification")
        if is_llm_actor(self.extracted_by) and not is_human_actor(self.approved_by):
            raise ValueError("LLM-extracted knowledge must be approved by a human")
        return self


class Issue(_Model):
    code: str
    severity: Severity
    message: str
    related: list[str] = []


class ValidationReport(_Model):
    issues: list[Issue] = []
    effective_type: EpistemicType
    effective_confidence: float
    duplicate_of: str | None = None
    conflicts_with: list[str] = []

    @property
    def max_severity(self) -> Severity | None:
        return max((i.severity for i in self.issues), default=None)

    @property
    def has_errors(self) -> bool:
        return any(i.severity is Severity.ERROR for i in self.issues)

    def codes(self) -> list[str]:
        return [i.code for i in self.issues]


class StagedCandidate(_Model):
    id: str
    batch_id: str
    source_id: str | None
    source_version: int | None
    candidate: Candidate
    state: CandidateState
    report: ValidationReport | None = None
    decided_by: str | None = None
    reason: str | None = None
    decided_at: dt.datetime | None = None
    assertion_id: str | None = None
    created_at: dt.datetime


def intervals_overlap(
    a_from: dt.date | None, a_to: dt.date | None, b_from: dt.date | None, b_to: dt.date | None
) -> bool:
    lo_a = a_from or dt.date.min
    hi_a = a_to or dt.date.max
    lo_b = b_from or dt.date.min
    hi_b = b_to or dt.date.max
    return lo_a <= hi_b and lo_b <= hi_a


def interval_intersection(
    intervals: list[tuple[dt.date | None, dt.date | None]],
) -> tuple[dt.date | None, dt.date | None] | None:
    """Intersection of closed intervals (None = unbounded); None if empty."""
    lo: dt.date | None = None
    hi: dt.date | None = None
    for f, t in intervals:
        if f is not None and (lo is None or f > lo):
            lo = f
        if t is not None and (hi is None or t < hi):
            hi = t
    if lo is not None and hi is not None and lo > hi:
        return None
    return lo, hi
