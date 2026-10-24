"""Ingestion, review and retraction workflows (extract → stage → validate → review → commit)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from kge.clock import Clock
from kge.extract import ExtractionFailed, Extractor
from kge.models import (
    Candidate,
    CandidateState,
    Severity,
    SourceKind,
    StagedCandidate,
    Status,
    ValidationReport,
    is_human_actor,
    is_llm_actor,
)
from kge.ontology import Ontology
from kge.reason import Reasoner
from kge.store import AddAssertion, AddEvidence, CommitResult, Op, Retract, Store
from kge.validate import Validator
from kge.versioning import ImpactReport, impact, retract_ops

AUTO = "policy:auto"
Decision = Literal["accept", "reject", "pending"]


class ReviewError(Exception):
    pass


def auto_policy(extracted_by: str, report: ValidationReport) -> tuple[Decision, str]:
    """Only clean, non-LLM candidates are accepted without a human."""
    if report.has_errors:
        return "reject", "validation errors: " + ", ".join(
            i.code for i in report.issues if i.severity is Severity.ERROR
        )
    if is_llm_actor(extracted_by):
        return "pending", "LLM output requires human review"
    sev = report.max_severity
    if sev is not None and sev >= Severity.WARNING:
        return "pending", "needs review: " + ", ".join(
            i.code for i in report.issues if i.severity >= Severity.WARNING
        )
    return "accept", "clean candidate from deterministic extractor"


def accept_op(sc: StagedCandidate, report: ValidationReport, approved_by: str) -> Op:
    c = sc.candidate
    if report.duplicate_of:
        return AddEvidence(
            assertion_id=report.duplicate_of,
            evidence=c.evidence,
            approved_by=approved_by,
            candidate_id=sc.id,
        )
    return AddAssertion(
        subject=c.subject,
        predicate=c.predicate,
        object=c.object,
        epistemic_type=report.effective_type,
        confidence=report.effective_confidence,
        confidence_method=c.confidence_method,
        valid_from=c.valid_from,
        valid_to=c.valid_to,
        extracted_by=c.extracted_by,
        approved_by=approved_by,
        candidate_id=sc.id,
        evidence=c.evidence,
    )


@dataclass
class IngestResult:
    source_id: str
    version: int
    skipped: bool = False
    staged: list[str] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    obsolete: list[str] = field(default_factory=list)
    unchanged: int = 0
    retracted: list[str] = field(default_factory=list)
    invalid: int = 0
    changesets: list[int] = field(default_factory=list)


class Engine:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the ontology-dependent parts after the ontology in effect changed."""
        self.validator = Validator(self.store, self.store.ontology)
        self.reasoner = Reasoner(self.store, self.store.ontology)

    @property
    def ontology(self) -> Ontology:
        return self.store.ontology

    @classmethod
    def open(
        cls,
        db: str | Path,
        ontology_path: str | Path,
        clock: Clock | None = None,
        allow_ontology_change: bool = False,
    ) -> Engine:
        ontology = Ontology.load(ontology_path)
        return cls(Store(db, ontology, clock, allow_ontology_change))

    # ------------------------------------------------------------ ingestion

    def _live(self, source_id: str, extracted_by: str) -> dict[tuple[str, ...], StagedCandidate]:
        """Candidates from earlier versions of a source that still stand."""
        live: dict[tuple[str, ...], StagedCandidate] = {}
        for sc in self.store.candidates(source_id=source_id):
            if sc.candidate.extracted_by != extracted_by:
                continue
            if sc.state is CandidateState.PENDING:
                live[sc.candidate.key()] = sc
            elif sc.state is CandidateState.ACCEPTED and sc.assertion_id:
                a = self.store.get_assertion(sc.assertion_id)
                if a is not None and a.status is Status.ACCEPTED:
                    live[sc.candidate.key()] = sc
        return live

    def ingest(
        self,
        uri: str,
        content: str,
        extractor: Extractor,
        title: str | None = None,
        kind: SourceKind = SourceKind.DOCUMENT,
        actor: str = "system",
    ) -> IngestResult:
        """All-or-nothing: if extraction or validation fails, the source version and every
        candidate staged for it are rolled back, so a retry is not mistaken for 'unchanged'."""
        try:
            with self.store.transaction():
                return self._ingest(uri, content, extractor, title, kind, actor)
        except Exception as exc:
            self.store.audit(
                actor, "ingest_failed", {"uri": uri, "error": f"{type(exc).__name__}: {exc}"}
            )
            raise

    def _ingest(
        self,
        uri: str,
        content: str,
        extractor: Extractor,
        title: str | None,
        kind: SourceKind,
        actor: str,
    ) -> IngestResult:
        src, changed, prev = self.store.register_source(uri, title or uri, kind, content, actor)
        result = IngestResult(source_id=src.id, version=src.version)
        if not changed:
            result.skipped = True
            self.store.audit(
                actor,
                "ingest_skipped",
                {"uri": uri, "source_id": src.id, "reason": "content unchanged"},
            )
            return result

        extraction = extractor.extract(src)
        if extraction.failed:
            # an unreadable answer is not an empty document: removing what earlier versions
            # stated would turn a failure into deletions
            raise ExtractionFailed(f"{extractor.id} could not extract {uri}: {extraction.invalid}")
        result.invalid = len(extraction.invalid)
        if extraction.invalid:
            self.store.audit(
                extractor.id,
                "extraction_invalid",
                {"source_id": src.id, "version": src.version, "items": extraction.invalid},
            )

        # Incremental update: only what changed relative to the previous version is staged.
        live = self._live(src.id, extractor.id) if prev else {}
        new_keys = {c.key() for c in extraction.candidates}
        gone = f"no longer stated in {src.id} v{src.version}"
        withdrawn: list[str] = []
        for key, sc in live.items():
            if key in new_keys:
                continue
            if sc.state is CandidateState.PENDING:
                self.store.decide(sc.id, CandidateState.OBSOLETE, actor, gone)
            else:
                assert sc.assertion_id is not None
                self.store.withdraw_candidate(sc.id, actor, gone)
                withdrawn.append(sc.assertion_id)
            result.obsolete.append(sc.id)
        # an assertion goes only when no source still states it; one retract_ops call so a
        # shared dependent inference is retracted once
        unsupported = [
            a for a in dict.fromkeys(withdrawn) if not self.store.supporting_candidates(a)
        ]
        retracts: list[Retract] = retract_ops(self.store, unsupported, gone)
        excluded = {r.assertion_id for r in retracts}

        ops: list[Op] = list(retracts)
        batch: list[Candidate] = []
        seen: set[tuple[str, ...]] = set()
        for cand in extraction.candidates:
            if cand.key() in live or cand.key() in seen:
                result.unchanged += 1  # already known, or stated twice in this version
                continue
            seen.add(cand.key())
            cid = self.store.stage(cand, f"{src.id}v{src.version}", src, extraction.raw_output)
            result.staged.append(cid)
            report = self.validator.validate(cand, batch, excluded)
            self.store.set_report(cid, report)
            decision, why = auto_policy(cand.extracted_by, report)
            if decision == "accept":
                batch.append(cand)  # later candidates are checked against what gets committed
            if decision == "reject":
                self.store.decide(cid, CandidateState.REJECTED, AUTO, why)
                result.rejected.append(cid)
            elif decision == "pending":
                result.pending.append(cid)
            else:
                staged = self.store.get_candidate(cid)
                assert staged is not None
                promoted = [h for h in report.supersedes if h not in excluded]
                ops += retract_ops(self.store, promoted, f"verified by {cid}", Status.SUPERSEDED)
                excluded.update(promoted)
                ops.append(accept_op(staged, report, AUTO))
                result.accepted.append(cid)

        if ops:
            cr = self.store.commit(f"ingest:{extractor.id}", f"ingest {uri} v{src.version}", ops)
            result.changesets.append(cr.changeset)
            result.retracted = cr.retracted
            result.changesets.extend(r.changeset for r in self.reasoner.run())
        return result

    # ------------------------------------------------------------ review

    def accept(
        self, cid: str, reviewer: str, reason: str, supersede: Sequence[str] = ()
    ) -> list[CommitResult]:
        if not is_human_actor(reviewer):
            raise ReviewError("manual review must be done by a human: use 'human:<name>'")
        if not reason.strip():
            raise ReviewError("a reason is required")
        sc = self.store.get_candidate(cid)
        if sc is None:
            raise ReviewError(f"unknown candidate {cid}")
        if sc.state is not CandidateState.PENDING:
            raise ReviewError(f"candidate {cid} is {sc.state.value}")
        retracts = retract_ops(
            self.store, list(supersede), f"superseded by {cid}: {reason}", Status.SUPERSEDED
        )
        excluded = {r.assertion_id for r in retracts}
        # Re-validate: the graph may have changed since the candidate was staged.
        report = self.validator.validate(sc.candidate, exclude=excluded)
        self.store.set_report(cid, report)
        if report.has_errors:
            raise ReviewError(f"{cid} has validation errors: {report.codes()}")
        blocking = [i for i in report.issues if i.severity is Severity.CONFLICT]
        if blocking:
            raise ReviewError(
                f"{cid} conflicts with {report.conflicts_with or 'other knowledge'};"
                " supersede the conflicting assertion(s) or reject the candidate"
            )
        promoted = [h for h in report.supersedes if h not in excluded]
        retracts += retract_ops(self.store, promoted, f"verified by {cid}", Status.SUPERSEDED)
        ops: list[Op] = [*retracts, accept_op(sc, report, reviewer)]
        cr = self.store.commit(reviewer, f"review accept {cid}: {reason}", ops)
        return [cr, *self.reasoner.run()]

    def reject(self, cid: str, reviewer: str, reason: str) -> None:
        if not reason.strip():
            raise ReviewError("a reason is required")
        self.store.decide(cid, CandidateState.REJECTED, reviewer, reason)

    def merge_alias(self, entity_id: str, alias_label: str, reviewer: str) -> None:
        if not is_human_actor(reviewer):
            raise ReviewError("entity merges must be done by a human")
        self.store.add_alias(entity_id, alias_label, reviewer)

    # ------------------------------------------------------------ change management

    def impact(self, assertion_id: str) -> ImpactReport:
        return impact(self.store, assertion_id)

    def retract(self, assertion_id: str, reason: str, actor: str) -> list[CommitResult]:
        if not reason.strip():
            raise ReviewError("a reason is required")
        ops = retract_ops(self.store, [assertion_id], reason)
        cr = self.store.commit(actor, f"retract {assertion_id}: {reason}", ops)
        return [cr, *self.reasoner.run()]
