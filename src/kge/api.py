"""Stable public API for embedding KGE in another Python system.

Everything another program needs is reachable from :class:`KnowledgeGraph`; the modules
behind it (``kge.store``, ``kge.pipeline`` …) may change between versions, this class and
the interchange format (``kge.interchange``) are kept compatible.

    from kge import KnowledgeGraph

    with KnowledgeGraph.open("kge.db", ontology="ontology/base.yaml") as kg:
        result = kg.submit(doc, system="crm", origin="llm")   # staged, never auto-accepted
        for c in kg.candidates():
            ...                                                # show to a human reviewer
        kg.accept(c.id, reviewer="human:sato", reason="checked against the contract")
        kg.export_knowledge().to_json()

The guarantee that unvalidated LLM output never becomes accepted knowledge holds for every
entry point: model output is staged as candidates, a human (``human:<name>``) must accept
it, and the store itself refuses any other commit of it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

from kge import audit, interchange, qa
from kge.clock import Clock
from kge.extract import Extractor, LLMExtractor, ReplayLLMClient, RuleExtractor
from kge.interchange import (
    AuditDoc,
    CandidatesDoc,
    DiffDoc,
    KnowledgeDoc,
    Origin,
    Submission,
    SubmissionExtractor,
)
from kge.models import Assertion, CandidateState, StagedCandidate
from kge.ontology import Ontology
from kge.pipeline import Engine, IngestResult
from kge.store import CommitResult, Store
from kge.versioning import ImpactReport

ExtractorName = Literal["rule", "llm-replay"]


class KnowledgeGraph:
    """One knowledge base (a SQLite file, or ``":memory:"``) under one ontology."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @classmethod
    def open(
        cls,
        db: str | Path = ":memory:",
        ontology: str | Path = "ontology/base.yaml",
        clock: Clock | None = None,
    ) -> KnowledgeGraph:
        """Open (or create) a knowledge base. Raises ``OntologyMismatch`` if the database
        was written under a different ontology; migrate it with ``kge ontology apply``."""
        if str(db) != ":memory:":
            Path(db).parent.mkdir(parents=True, exist_ok=True)
        return cls(Engine.open(db, ontology, clock))

    def close(self) -> None:
        self.engine.store.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def store(self) -> Store:
        """Low-level access. Not covered by the compatibility promise."""
        return self.engine.store

    @property
    def ontology(self) -> Ontology:
        return self.engine.ontology

    # ------------------------------------------------------------ writing

    def ingest_text(
        self,
        uri: str,
        text: str,
        extractor: ExtractorName | Extractor = "rule",
        title: str | None = None,
        llm_fixtures: str | Path = "fixtures/llm",
    ) -> IngestResult:
        """Extract candidates from a document with a built-in or custom extractor.
        Re-ingesting the same ``uri`` stages only what changed."""
        ex: Extractor
        if extractor == "rule":
            ex = RuleExtractor(self.ontology)
        elif extractor == "llm-replay":
            ex = LLMExtractor(ReplayLLMClient(llm_fixtures), self.ontology)
        else:
            ex = extractor
        return self.engine.ingest(uri, text, ex, title=title)

    def submit(
        self,
        submission: Submission | str | bytes | dict[str, Any],
        system: str,
        origin: Origin = "llm",
    ) -> IngestResult:
        """Stage statements produced by another system (an interchange ``submission``).

        ``origin`` says how they were produced and defaults to the cautious ``"llm"``:
        such candidates always wait for a human, facts whose quote is not found in the
        submitted text become hypotheses, and confidence is capped. ``"deterministic"`` is
        for rule-based or curated sources; clean candidates are then accepted
        automatically, but a fact still needs a quote found in the submitted text.
        """
        sub = (
            submission
            if isinstance(submission, Submission)
            else interchange.parse_submission(submission)
        )
        ex = SubmissionExtractor(sub, system, origin)
        return self.engine.ingest(
            sub.source.uri,
            sub.source.content,
            ex,
            title=sub.source.title,
            kind=interchange.source_kind(sub),
            actor=f"system:{system}",
        )

    def accept(
        self, candidate_id: str, reviewer: str, reason: str, supersede: Sequence[str] = ()
    ) -> list[CommitResult]:
        """Human review: ``reviewer`` must be ``human:<name>`` and ``reason`` non-empty."""
        return self.engine.accept(candidate_id, reviewer, reason, supersede)

    def reject(self, candidate_id: str, reviewer: str, reason: str) -> None:
        self.engine.reject(candidate_id, reviewer, reason)

    def retract(self, assertion_id: str, actor: str, reason: str) -> list[CommitResult]:
        """Retract an assertion and every inference that depends on it."""
        return self.engine.retract(assertion_id, reason, actor)

    # ------------------------------------------------------------ reading

    def candidates(self, state: str | None = "pending") -> list[StagedCandidate]:
        return self.store.candidates(state=CandidateState(state) if state else None)

    def candidate(self, candidate_id: str) -> StagedCandidate | None:
        return self.store.get_candidate(candidate_id)

    def query(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        include_hypotheses: bool = False,
        as_of_changeset: int | None = None,
        as_of: dt.datetime | None = None,
    ) -> list[Assertion]:
        """Accepted knowledge, now or as it was known at a changeset or time.
        ``subject`` is an entity label."""
        rows = self.store.snapshot(as_of_changeset, as_of, include_hypotheses=include_hypotheses)
        return [
            a
            for a in rows
            if (subject is None or self.store.label(a.subject) == subject)
            and (predicate is None or a.predicate == predicate)
        ]

    def label(self, entity_id: str) -> str:
        return self.store.label(entity_id)

    def history(self, assertion_id: str) -> list[Assertion]:
        return self.store.history(assertion_id)

    def impact(self, assertion_id: str) -> ImpactReport:
        return self.engine.impact(assertion_id)

    def head(self) -> int:
        """The latest changeset number."""
        return self.store.head()

    def verify_audit(self) -> audit.VerifyResult:
        return audit.verify(self.store.conn)

    def quality(self) -> qa.QAReport:
        """Invariant checks and metrics (the golden regression is a CLI concern)."""
        return qa.run(self.store, self.store.clock.now())

    # ------------------------------------------------------------ interchange

    def export_knowledge(
        self,
        as_of_changeset: int | None = None,
        as_of: dt.datetime | None = None,
        include_hypotheses: bool = True,
    ) -> KnowledgeDoc:
        return interchange.knowledge(self.store, as_of_changeset, as_of, include_hypotheses)

    def export_candidates(self, state: str | None = "pending") -> CandidatesDoc:
        return interchange.candidates(self.store, state)

    def export_diff(self, base: int, target: int) -> DiffDoc:
        return interchange.changes(self.store, base, target)

    def export_audit(self) -> AuditDoc:
        return interchange.audit_log(self.store)


__all__ = ["ExtractorName", "KnowledgeGraph"]
