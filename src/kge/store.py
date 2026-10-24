"""SQLite-backed, append-only knowledge store.

Every change to committed knowledge goes through `Store.commit`, which enforces
the invariants of DESIGN.md (no unreviewed candidate and no LLM output without
human approval can become an assertion) and writes one audit entry per changeset.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kge import audit
from kge.clock import Clock, SystemClock
from kge.models import (
    Assertion,
    Candidate,
    CandidateState,
    Entity,
    EntityRef,
    EpistemicType,
    Evidence,
    Justification,
    LiteralValue,
    Source,
    SourceKind,
    StagedCandidate,
    Status,
    ValidationReport,
    is_human_actor,
    is_llm_actor,
    normalize_text,
)
from kge.ontology import Ontology

_AV_COLUMNS = [
    "id",
    "subject",
    "predicate",
    "object_value",
    "object_kind",
    "object_datatype",
    "epistemic_type",
    "status",
    "confidence",
    "confidence_method",
    "valid_from",
    "valid_to",
    "extracted_by",
    "approved_by",
    "candidate_id",
    "recorded_at",
    "tx_from",
    "cs_from",
]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS entities(
  id TEXT PRIMARY KEY, label TEXT NOT NULL, label_norm TEXT NOT NULL,
  types TEXT NOT NULL, aliases TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(
  id TEXT NOT NULL, version INTEGER NOT NULL, uri TEXT NOT NULL, title TEXT NOT NULL,
  kind TEXT NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL,
  retrieved_at TEXT NOT NULL, PRIMARY KEY(id, version));
CREATE TABLE IF NOT EXISTS changesets(
  id INTEGER PRIMARY KEY, parent_id INTEGER, author TEXT NOT NULL, message TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS changeset_ops(
  changeset_id INTEGER NOT NULL, seq INTEGER NOT NULL, op TEXT NOT NULL, target TEXT NOT NULL,
  payload TEXT NOT NULL, PRIMARY KEY(changeset_id, seq));
CREATE TABLE IF NOT EXISTS assertion_versions(
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL, subject TEXT NOT NULL, predicate TEXT NOT NULL, object_value TEXT NOT NULL,
  object_kind TEXT NOT NULL, object_datatype TEXT, epistemic_type TEXT NOT NULL,
  status TEXT NOT NULL, confidence REAL NOT NULL, confidence_method TEXT NOT NULL,
  valid_from TEXT, valid_to TEXT, extracted_by TEXT NOT NULL, approved_by TEXT NOT NULL,
  candidate_id TEXT, recorded_at TEXT NOT NULL, tx_from TEXT NOT NULL, tx_to TEXT,
  cs_from INTEGER NOT NULL, cs_to INTEGER);
CREATE INDEX IF NOT EXISTS av_current ON assertion_versions(id, cs_to);
CREATE INDEX IF NOT EXISTS av_spo ON assertion_versions(subject, predicate);
CREATE TABLE IF NOT EXISTS evidence(
  assertion_id TEXT NOT NULL, source_id TEXT NOT NULL, source_version INTEGER NOT NULL,
  quote TEXT NOT NULL, locator TEXT, cs INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS justifications(assertion_id TEXT PRIMARY KEY, rule_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS justification_premises(
  assertion_id TEXT NOT NULL, premise_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jp_premise ON justification_premises(premise_id);
CREATE TABLE IF NOT EXISTS candidates(
  id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, source_id TEXT, source_version INTEGER,
  payload TEXT NOT NULL, raw_output TEXT, state TEXT NOT NULL, report TEXT,
  decided_by TEXT, reason TEXT, decided_at TEXT, assertion_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log(
  seq INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS av_no_delete BEFORE DELETE ON assertion_versions
BEGIN SELECT RAISE(ABORT, 'assertion_versions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS av_close_only BEFORE UPDATE ON assertion_versions
WHEN OLD.cs_to IS NOT NULL OR NEW.cs_to IS NULL OR NEW.tx_to IS NULL
  OR {" OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in _AV_COLUMNS)}
BEGIN SELECT RAISE(ABORT, 'assertion versions may only be closed, never edited'); END;
CREATE TABLE IF NOT EXISTS ontology_versions(
  version INTEGER PRIMARY KEY, content_hash TEXT NOT NULL, content TEXT NOT NULL,
  after_changeset INTEGER NOT NULL, recorded_at TEXT NOT NULL, actor TEXT NOT NULL,
  reason TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS ov_no_update BEFORE UPDATE ON ontology_versions
BEGIN SELECT RAISE(ABORT, 'ontology_versions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ov_no_delete BEFORE DELETE ON ontology_versions
BEGIN SELECT RAISE(ABORT, 'ontology_versions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ev_no_change BEFORE UPDATE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
"""


class UnauthorizedCommit(Exception):
    """Raised when a change would bypass validation or review."""


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- changeset ops


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityIdRef(_Op):
    kind: Literal["id"] = "id"
    id: str


TermRef = Annotated[EntityRef | EntityIdRef | LiteralValue, Field(discriminator="kind")]
SubjectRef = Annotated[EntityRef | EntityIdRef, Field(discriminator="kind")]


class AddAssertion(_Op):
    op: Literal["add"] = "add"
    subject: SubjectRef
    predicate: str
    object: TermRef
    epistemic_type: EpistemicType
    confidence: float
    confidence_method: str
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    extracted_by: str
    approved_by: str
    candidate_id: str | None = None
    evidence: list[Evidence] = []
    justification: Justification | None = None


class AddEvidence(_Op):
    op: Literal["add_evidence"] = "add_evidence"
    assertion_id: str
    evidence: list[Evidence]
    approved_by: str
    candidate_id: str | None = None


class Retract(_Op):
    op: Literal["retract"] = "retract"
    assertion_id: str
    reason: str
    status: Status = Status.RETRACTED


Op = AddAssertion | AddEvidence | Retract


class CommitResult(BaseModel):
    changeset: int
    created: list[str] = []
    retracted: list[str] = []
    evidence_added: list[str] = []


class ChangeSetInfo(BaseModel):
    id: int
    parent_id: int | None
    author: str
    message: str
    created_at: dt.datetime
    ops: list[dict[str, Any]]


# ---------------------------------------------------------------- store


def _d(s: str | None) -> dt.date | None:
    return dt.date.fromisoformat(s) if s else None


def _iso(d: dt.date | dt.datetime | None) -> str | None:
    return d.isoformat() if d else None


class OntologyMismatch(Exception):
    """The ontology given to the store is not the one in effect for its knowledge."""


class OntologyVersion(BaseModel):
    version: int
    content_hash: str
    after_changeset: int  # in effect for changesets after this one
    recorded_at: dt.datetime
    actor: str
    reason: str


class Store:
    def __init__(
        self,
        path: str | Path,
        ontology: Ontology,
        clock: Clock | None = None,
        allow_ontology_change: bool = False,
    ) -> None:
        """The ontology in effect is the one recorded in the database. A different
        ``ontology`` is refused, unless ``allow_ontology_change`` is set: then the recorded
        one stays in effect and the new one is kept as ``pending_ontology`` until it is
        applied (see ``kge.ontology_change``)."""
        self.path = str(path)
        self.clock: Clock = clock or SystemClock()
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._in_tx = False
        self.ontology = ontology
        self.pending_ontology: Ontology | None = None
        versions = self.ontology_versions()
        if not versions:
            self.record_ontology(ontology, "system", "initial ontology")
        elif versions[-1].content_hash != ontology.content_hash():
            current = versions[-1]
            if not allow_ontology_change:
                raise OntologyMismatch(
                    f"the ontology differs from v{current.version}, which is in effect for"
                    " this database; review it with 'kge ontology plan' and apply it with"
                    " 'kge ontology apply'"
                )
            self.ontology = self.ontology_version(current.version)
            self.pending_ontology = ontology

    # ------------------------------------------------------------ ontology versions

    def ontology_versions(self) -> list[OntologyVersion]:
        rows = self.conn.execute(
            "SELECT version, content_hash, after_changeset, recorded_at, actor, reason"
            " FROM ontology_versions ORDER BY version"
        ).fetchall()
        return [
            OntologyVersion(
                version=r[0],
                content_hash=r[1],
                after_changeset=r[2],
                recorded_at=dt.datetime.fromisoformat(r[3]),
                actor=r[4],
                reason=r[5],
            )
            for r in rows
        ]

    def ontology_version(self, version: int) -> Ontology:
        row = self.conn.execute(
            "SELECT content FROM ontology_versions WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown ontology version {version}")
        return Ontology.model_validate_json(row[0])

    def ontology_version_at(self, changeset: int) -> int:
        """The ontology version that was in effect when ``changeset`` was written."""
        row = self.conn.execute(
            "SELECT MAX(version) FROM ontology_versions WHERE after_changeset < ? OR version = 1",
            (changeset,),
        ).fetchone()
        return int(row[0])

    def record_ontology(self, ontology: Ontology, actor: str, reason: str) -> int:
        """Record ``ontology`` as the one in effect from the next changeset on."""
        with self.transaction() as c:
            row = c.execute("SELECT MAX(version) FROM ontology_versions").fetchone()
            version = int(row[0] or 0) + 1
            now = self.clock.now()
            h = ontology.content_hash()
            c.execute(
                "INSERT INTO ontology_versions VALUES (?,?,?,?,?,?,?)",
                (
                    version,
                    h,
                    ontology.canonical_json(),
                    self.head(),
                    now.isoformat(),
                    actor,
                    reason,
                ),
            )
            audit.append(
                c,
                now,
                actor,
                "ontology_registered" if version == 1 else "ontology_changed",
                {
                    "version": version,
                    "content_hash": h,
                    "after_changeset": self.head(),
                    "reason": reason,
                },
            )
        self.ontology = ontology
        self.pending_ontology = None
        return version

    def close(self) -> None:
        self.conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self._in_tx:
            yield self.conn
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._in_tx = False

    def audit(self, actor: str, action: str, payload: dict[str, Any]) -> None:
        with self.transaction() as c:
            audit.append(c, self.clock.now(), actor, action, payload)

    def _next(self, name: str) -> int:
        row = self.conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
        n = (row[0] if row else 0) + 1
        self.conn.execute(
            "INSERT INTO counters(name, value) VALUES(?, ?)"
            " ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (name, n),
        )
        return n

    # ------------------------------------------------------------ sources

    def register_source(
        self, uri: str, title: str, kind: SourceKind, content: str, actor: str = "system"
    ) -> tuple[Source, bool, int | None]:
        """Returns (source, changed, previous_version). Unchanged content is a no-op."""
        h = sha256_text(content)
        with self.transaction() as c:
            row = c.execute(
                "SELECT id, version, content_hash FROM sources WHERE uri=?"
                " ORDER BY version DESC LIMIT 1",
                (uri,),
            ).fetchone()
            if row and row[2] == h:
                src = self.get_source(row[0], row[1])
                assert src is not None
                return src, False, row[1]
            sid = row[0] if row else f"S{self._next('source'):03d}"
            version = row[1] + 1 if row else 1
            now = self.clock.now()
            c.execute(
                "INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)",
                (sid, version, uri, title, kind.value, content, h, now.isoformat()),
            )
            audit.append(
                c,
                now,
                actor,
                "source_registered",
                {"source_id": sid, "version": version, "uri": uri, "content_hash": h},
            )
            src = self.get_source(sid, version)
            assert src is not None
            return src, True, (row[1] if row else None)

    def get_source(self, sid: str, version: int) -> Source | None:
        row = self.conn.execute(
            "SELECT id, version, uri, title, kind, content, content_hash, retrieved_at"
            " FROM sources WHERE id=? AND version=?",
            (sid, version),
        ).fetchone()
        if not row:
            return None
        return Source(
            id=row[0],
            version=row[1],
            uri=row[2],
            title=row[3],
            kind=SourceKind(row[4]),
            content=row[5],
            content_hash=row[6],
            retrieved_at=dt.datetime.fromisoformat(row[7]),
        )

    # ------------------------------------------------------------ entities

    def _entity(self, row: Sequence[Any]) -> Entity:
        return Entity(id=row[0], label=row[1], types=json.loads(row[2]), aliases=json.loads(row[3]))

    def get_entity(self, eid: str) -> Entity | None:
        row = self.conn.execute(
            "SELECT id, label, types, aliases FROM entities WHERE id=?", (eid,)
        ).fetchone()
        return self._entity(row) if row else None

    def find_entity_by_label(self, label: str) -> Entity | None:
        row = self.conn.execute(
            "SELECT id, label, types, aliases FROM entities WHERE label=? ORDER BY id LIMIT 1",
            (label,),
        ).fetchone()
        return self._entity(row) if row else None

    def find_similar_entities(self, label: str) -> list[Entity]:
        """Entities whose normalized label or alias equals the normalized `label`."""
        norm = normalize_text(label)
        out = []
        for row in self.conn.execute("SELECT id, label, types, aliases FROM entities"):
            e = self._entity(row)
            if e.label == label:
                continue
            if normalize_text(e.label) == norm or any(normalize_text(a) == norm for a in e.aliases):
                out.append(e)
        return out

    def entities(self) -> list[Entity]:
        return [
            self._entity(r)
            for r in self.conn.execute("SELECT id, label, types, aliases FROM entities ORDER BY id")
        ]

    def add_alias(self, eid: str, alias: str, actor: str) -> None:
        with self.transaction() as c:
            e = self.get_entity(eid)
            if e is None:
                raise KeyError(eid)
            if alias not in e.aliases:
                c.execute(
                    "UPDATE entities SET aliases=? WHERE id=?",
                    (json.dumps([*e.aliases, alias], ensure_ascii=False), eid),
                )
                audit.append(
                    c,
                    self.clock.now(),
                    actor,
                    "entity_alias_added",
                    {"entity_id": eid, "alias": alias},
                )

    def _resolve_entity(self, ref: EntityRef | EntityIdRef, implied_type: str, cs: int) -> str:
        if isinstance(ref, EntityIdRef):
            if self.get_entity(ref.id) is None:
                raise UnauthorizedCommit(f"unknown entity {ref.id}")
            return ref.id
        etype = ref.type or implied_type
        e = self.find_entity_by_label(ref.label)
        now = self.clock.now()
        if e is None:
            eid = f"E{self._next('entity'):04d}"
            self.conn.execute(
                "INSERT INTO entities VALUES (?,?,?,?,?)",
                (eid, ref.label, normalize_text(ref.label), json.dumps([etype]), "[]"),
            )
            audit.append(
                self.conn,
                now,
                "system",
                "entity_created",
                {"entity_id": eid, "label": ref.label, "type": etype, "changeset": cs},
            )
            return eid
        if not self.ontology.conforms(e.types, etype):
            for t in e.types:
                if self.ontology.disjoint(t, etype):
                    raise UnauthorizedCommit(f"type {etype} is disjoint with {t} on {e.id}")
            self.conn.execute(
                "UPDATE entities SET types=? WHERE id=?", (json.dumps([*e.types, etype]), e.id)
            )
            audit.append(
                self.conn,
                now,
                "system",
                "entity_type_added",
                {"entity_id": e.id, "type": etype, "changeset": cs},
            )
        return e.id

    def label(self, eid: str) -> str:
        e = self.get_entity(eid)
        return e.label if e else eid

    # ------------------------------------------------------------ candidates (staging)

    def stage(
        self,
        cand: Candidate,
        batch_id: str,
        source: Source | None,
        raw_output: str | None = None,
    ) -> str:
        with self.transaction() as c:
            cid = f"C{self._next('candidate'):04d}"
            now = self.clock.now()
            c.execute(
                "INSERT INTO candidates(id, batch_id, source_id, source_version, payload,"
                " raw_output, state, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    cid,
                    batch_id,
                    source.id if source else None,
                    source.version if source else None,
                    cand.model_dump_json(),
                    raw_output,
                    CandidateState.PENDING.value,
                    now.isoformat(),
                ),
            )
            audit.append(
                c,
                now,
                cand.extracted_by,
                "candidate_staged",
                {"candidate_id": cid, "batch_id": batch_id, "key": list(cand.key())},
            )
        return cid

    def set_report(self, cid: str, report: ValidationReport) -> None:
        with self.transaction() as c:
            c.execute("UPDATE candidates SET report=? WHERE id=?", (report.model_dump_json(), cid))
            audit.append(
                c,
                self.clock.now(),
                "validator",
                "candidate_validated",
                {
                    "candidate_id": cid,
                    "issues": report.codes(),
                    "effective_type": report.effective_type.value,
                },
            )

    def decide(
        self,
        cid: str,
        state: CandidateState,
        decided_by: str,
        reason: str,
        assertion_id: str | None = None,
    ) -> None:
        with self.transaction() as c:
            sc = self.get_candidate(cid)
            if sc is None:
                raise KeyError(cid)
            if sc.state is not CandidateState.PENDING:
                raise UnauthorizedCommit(f"candidate {cid} is already {sc.state.value}")
            now = self.clock.now()
            c.execute(
                "UPDATE candidates SET state=?, decided_by=?, reason=?, decided_at=?,"
                " assertion_id=? WHERE id=?",
                (state.value, decided_by, reason, now.isoformat(), assertion_id, cid),
            )
            audit.append(
                c,
                now,
                decided_by,
                "candidate_decided",
                {
                    "candidate_id": cid,
                    "state": state.value,
                    "reason": reason,
                    "assertion_id": assertion_id,
                },
            )

    def _staged(self, row: Sequence[Any]) -> StagedCandidate:
        return StagedCandidate(
            id=row[0],
            batch_id=row[1],
            source_id=row[2],
            source_version=row[3],
            candidate=Candidate.model_validate_json(row[4]),
            state=CandidateState(row[5]),
            report=ValidationReport.model_validate_json(row[6]) if row[6] else None,
            decided_by=row[7],
            reason=row[8],
            decided_at=dt.datetime.fromisoformat(row[9]) if row[9] else None,
            assertion_id=row[10],
            created_at=dt.datetime.fromisoformat(row[11]),
        )

    _CAND_COLS = (
        "id, batch_id, source_id, source_version, payload, state, report,"
        " decided_by, reason, decided_at, assertion_id, created_at"
    )

    def get_candidate(self, cid: str) -> StagedCandidate | None:
        row = self.conn.execute(
            f"SELECT {self._CAND_COLS} FROM candidates WHERE id=?", (cid,)
        ).fetchone()
        return self._staged(row) if row else None

    def withdraw_candidate(self, cid: str, by: str, reason: str) -> None:
        """An accepted candidate whose source no longer states it. The assertion it produced
        keeps its other support; see ``supporting_candidates``."""
        with self.transaction() as c:
            sc = self.get_candidate(cid)
            if sc is None:
                raise KeyError(cid)
            if sc.state is not CandidateState.ACCEPTED:
                raise UnauthorizedCommit(f"candidate {cid} is {sc.state.value}, not accepted")
            now = self.clock.now()
            c.execute(
                "UPDATE candidates SET state=?, reason=?, decided_at=? WHERE id=?",
                (CandidateState.OBSOLETE.value, reason, now.isoformat(), cid),
            )
            audit.append(
                c,
                now,
                by,
                "candidate_withdrawn",
                {"candidate_id": cid, "assertion_id": sc.assertion_id, "reason": reason},
            )

    def supporting_candidates(self, assertion_id: str) -> list[str]:
        """Accepted candidates (from any source) that still state this assertion."""
        rows = self.conn.execute(
            "SELECT id FROM candidates WHERE assertion_id=? AND state=? ORDER BY id",
            (assertion_id, CandidateState.ACCEPTED.value),
        )
        return [r[0] for r in rows]

    def candidates(
        self,
        state: CandidateState | None = None,
        source_id: str | None = None,
        source_version: int | None = None,
    ) -> list[StagedCandidate]:
        sql = f"SELECT {self._CAND_COLS} FROM candidates WHERE 1=1"
        args: list[Any] = []
        if state:
            sql += " AND state=?"
            args.append(state.value)
        if source_id:
            sql += " AND source_id=?"
            args.append(source_id)
        if source_version is not None:
            sql += " AND source_version=?"
            args.append(source_version)
        return [self._staged(r) for r in self.conn.execute(sql + " ORDER BY id", args)]

    # ------------------------------------------------------------ assertions

    _AV_SELECT = (
        "SELECT id, subject, predicate, object_value, object_kind, object_datatype,"
        " epistemic_type, status, confidence, confidence_method, valid_from, valid_to,"
        " extracted_by, approved_by, candidate_id, recorded_at, tx_to, cs_from"
        " FROM assertion_versions"
    )

    def _assertion(self, row: Sequence[Any], upto_cs: int | None = None) -> Assertion:
        aid = row[0]
        ev_sql = (
            "SELECT source_id, source_version, quote, locator FROM evidence WHERE assertion_id=?"
        )
        ev_args: list[Any] = [aid]
        if upto_cs is not None:
            ev_sql += " AND cs<=?"
            ev_args.append(upto_cs)
        evidence = [
            Evidence(source_id=r[0], source_version=r[1], quote=r[2], locator=r[3])
            for r in self.conn.execute(ev_sql + " ORDER BY rowid", ev_args)
        ]
        just = None
        jr = self.conn.execute(
            "SELECT rule_id FROM justifications WHERE assertion_id=?", (aid,)
        ).fetchone()
        if jr:
            premises = [
                r[0]
                for r in self.conn.execute(
                    "SELECT premise_id FROM justification_premises WHERE assertion_id=?"
                    " ORDER BY rowid",
                    (aid,),
                )
            ]
            just = Justification(rule_id=jr[0], premise_ids=premises)
        return Assertion(
            id=aid,
            subject=row[1],
            predicate=row[2],
            object_value=row[3],
            object_kind=row[4],
            object_datatype=row[5],
            epistemic_type=EpistemicType(row[6]),
            status=Status(row[7]),
            confidence=row[8],
            confidence_method=row[9],
            valid_from=_d(row[10]),
            valid_to=_d(row[11]),
            extracted_by=row[12],
            approved_by=row[13],
            candidate_id=row[14],
            recorded_at=dt.datetime.fromisoformat(row[15]),
            superseded_at=dt.datetime.fromisoformat(row[16]) if row[16] else None,
            changeset=row[17],
            evidence=evidence,
            justification=just,
        )

    def get_assertion(self, aid: str) -> Assertion | None:
        """Current version of an assertion (whatever its status)."""
        row = self.conn.execute(
            self._AV_SELECT + " WHERE id=? AND cs_to IS NULL", (aid,)
        ).fetchone()
        return self._assertion(row) if row else None

    def history(self, aid: str) -> list[Assertion]:
        return [
            self._assertion(r)
            for r in self.conn.execute(self._AV_SELECT + " WHERE id=? ORDER BY row_id", (aid,))
        ]

    def active(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        include_hypotheses: bool = False,
        exclude: set[str] | frozenset[str] = frozenset(),
    ) -> list[Assertion]:
        """Currently accepted knowledge. Hypotheses are excluded unless asked for."""
        sql = self._AV_SELECT + " WHERE cs_to IS NULL AND status='accepted'"
        args: list[Any] = []
        if subject:
            sql += " AND subject=?"
            args.append(subject)
        if predicate:
            sql += " AND predicate=?"
            args.append(predicate)
        if not include_hypotheses:
            sql += " AND epistemic_type != 'hypothesis'"
        rows = self.conn.execute(sql + " ORDER BY id", args).fetchall()
        return [self._assertion(r) for r in rows if r[0] not in exclude]

    def snapshot(
        self,
        changeset: int | None = None,
        at: dt.datetime | None = None,
        include_hypotheses: bool = False,
    ) -> list[Assertion]:
        """Accepted knowledge as the system knew it after `changeset` or at time `at`."""
        if changeset is None and at is None:
            return self.active(include_hypotheses=include_hypotheses)
        if changeset is not None:
            sql = self._AV_SELECT + " WHERE cs_from<=? AND (cs_to IS NULL OR cs_to>?)"
            args: list[Any] = [changeset, changeset]
            upto = changeset
        else:
            assert at is not None
            # stored times are UTC ISO strings, so the cut-off must be too for the text
            # comparison to be chronological (a naive time is read as UTC)
            when = (at if at.tzinfo else at.replace(tzinfo=dt.UTC)).astimezone(dt.UTC)
            iso = when.isoformat()
            sql = self._AV_SELECT + " WHERE tx_from<=? AND (tx_to IS NULL OR tx_to>?)"
            args = [iso, iso]
            row = self.conn.execute(
                "SELECT MAX(id) FROM changesets WHERE created_at<=?", (iso,)
            ).fetchone()
            upto = row[0] or 0
        sql += " AND status='accepted'"
        if not include_hypotheses:
            sql += " AND epistemic_type != 'hypothesis'"
        return [self._assertion(r, upto) for r in self.conn.execute(sql + " ORDER BY id", args)]

    def dependents(self, premise_id: str) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT assertion_id FROM justification_premises WHERE premise_id=?"
                " ORDER BY assertion_id",
                (premise_id,),
            )
        ]

    # ------------------------------------------------------------ changesets

    def changesets(self) -> list[ChangeSetInfo]:
        out = []
        for cid, parent, author, msg, at in self.conn.execute(
            "SELECT id, parent_id, author, message, created_at FROM changesets ORDER BY id"
        ).fetchall():
            ops = [
                {"op": op, "target": target, **json.loads(payload)}
                for op, target, payload in self.conn.execute(
                    "SELECT op, target, payload FROM changeset_ops WHERE changeset_id=?"
                    " ORDER BY seq",
                    (cid,),
                )
            ]
            out.append(
                ChangeSetInfo(
                    id=cid,
                    parent_id=parent,
                    author=author,
                    message=msg,
                    created_at=dt.datetime.fromisoformat(at),
                    ops=ops,
                )
            )
        return out

    def head(self) -> int:
        row = self.conn.execute("SELECT MAX(id) FROM changesets").fetchone()
        return int(row[0] or 0)

    def commit(self, author: str, message: str, ops: Sequence[Op]) -> CommitResult:
        if not ops:
            raise ValueError("refusing to create an empty changeset")
        with self.transaction() as c:
            parent = self.head() or None
            now = self.clock.now()
            cur = c.execute(
                "INSERT INTO changesets(parent_id, author, message, created_at) VALUES (?,?,?,?)",
                (parent, author, message, now.isoformat()),
            )
            cs = int(cur.lastrowid or 0)
            result = CommitResult(changeset=cs)
            for seq, op in enumerate(ops, start=1):
                if isinstance(op, AddAssertion):
                    target = self._apply_add(op, cs, now)
                    result.created.append(target)
                elif isinstance(op, AddEvidence):
                    target = self._apply_add_evidence(op, cs)
                    result.evidence_added.append(target)
                else:
                    target = self._apply_retract(op, cs, now)
                    result.retracted.append(target)
                c.execute(
                    "INSERT INTO changeset_ops VALUES (?,?,?,?,?)",
                    (cs, seq, op.op, target, op.model_dump_json(exclude={"op"})),
                )
            audit.append(
                c,
                now,
                author,
                "changeset_committed",
                {
                    "changeset": cs,
                    "parent": parent,
                    "message": message,
                    "created": result.created,
                    "retracted": result.retracted,
                    "evidence_added": result.evidence_added,
                },
            )
            return result

    def _check_candidate(
        self, cid: str | None, extracted_by: str, approved_by: str, op: _Op | None = None
    ) -> None:
        if cid is None:
            return
        sc = self.get_candidate(cid)
        if sc is None:
            raise UnauthorizedCommit(f"unknown candidate {cid}")
        if sc.state is not CandidateState.PENDING:
            raise UnauthorizedCommit(f"candidate {cid} is {sc.state.value}, not pending")
        if sc.report is None:
            raise UnauthorizedCommit(f"candidate {cid} has not been validated")
        if sc.report.has_errors:
            raise UnauthorizedCommit(f"candidate {cid} has validation errors")
        if sc.candidate.extracted_by != extracted_by:
            raise UnauthorizedCommit(f"candidate {cid}: extractor mismatch")
        if is_llm_actor(extracted_by) and not is_human_actor(approved_by):
            raise UnauthorizedCommit(f"candidate {cid}: LLM output requires human approval")
        # the operation must say exactly what was validated, not an edited copy of it
        c, rep = sc.candidate, sc.report
        if isinstance(op, AddAssertion):
            same = (
                op.subject == c.subject
                and op.predicate == c.predicate
                and op.object == c.object
                and (op.valid_from, op.valid_to) == (c.valid_from, c.valid_to)
                and op.evidence == c.evidence
                and op.confidence_method == c.confidence_method
                and op.epistemic_type is rep.effective_type
                and op.confidence == rep.effective_confidence
                and rep.duplicate_of is None
            )
            if not same:
                raise UnauthorizedCommit(
                    f"candidate {cid}: operation differs from what was validated"
                )
        elif isinstance(op, AddEvidence):
            if op.assertion_id != rep.duplicate_of or op.evidence != c.evidence:
                raise UnauthorizedCommit(
                    f"candidate {cid}: operation differs from what was validated"
                )

    def _apply_add(self, op: AddAssertion, cs: int, now: dt.datetime) -> str:
        if op.candidate_id is None:
            # Only the reasoner may add knowledge without a staged candidate.
            if not (
                op.epistemic_type is EpistemicType.INFERENCE
                and op.extracted_by.startswith("reasoner:")
                and op.justification is not None
            ):
                raise UnauthorizedCommit("assertions must come from a validated candidate")
            for pid in op.justification.premise_ids:
                p = self.get_assertion(pid)
                if (
                    p is None
                    or p.status is not Status.ACCEPTED
                    or p.epistemic_type is EpistemicType.HYPOTHESIS
                ):
                    raise UnauthorizedCommit(f"premise {pid} is not accepted knowledge")
        else:
            if op.epistemic_type is EpistemicType.INFERENCE:
                raise UnauthorizedCommit("inferences can only be produced by the reasoner")
            self._check_candidate(op.candidate_id, op.extracted_by, op.approved_by, op)
        pdef = self.ontology.predicates.get(op.predicate)
        if pdef is None:
            raise UnauthorizedCommit(f"unknown predicate {op.predicate}")
        subject = self._resolve_entity(op.subject, pdef.domain, cs)
        if isinstance(op.object, LiteralValue):
            obj, kind, datatype = op.object.value, "literal", op.object.datatype
        else:
            obj, kind, datatype = self._resolve_entity(op.object, pdef.range, cs), "entity", None
        aid = f"A{self._next('assertion'):05d}"
        a = Assertion(
            id=aid,
            subject=subject,
            predicate=op.predicate,
            object_value=obj,
            object_kind=kind,
            object_datatype=datatype,
            epistemic_type=op.epistemic_type,
            status=Status.ACCEPTED,
            confidence=op.confidence,
            confidence_method=op.confidence_method,
            valid_from=op.valid_from,
            valid_to=op.valid_to,
            recorded_at=now,
            extracted_by=op.extracted_by,
            approved_by=op.approved_by,
            candidate_id=op.candidate_id,
            changeset=cs,
            evidence=op.evidence,
            justification=op.justification,
        )  # constructing the model enforces the invariants
        self._insert_version(a, now, cs)
        for ev in a.evidence:
            self._insert_evidence(aid, ev, cs)
        if a.justification:
            self.conn.execute(
                "INSERT INTO justifications VALUES (?,?)", (aid, a.justification.rule_id)
            )
            for pid in a.justification.premise_ids:
                self.conn.execute("INSERT INTO justification_premises VALUES (?,?)", (aid, pid))
        if op.candidate_id:
            self._close_candidate(op.candidate_id, op.approved_by, aid, now)
        return aid

    def _close_candidate(self, cid: str, by: str, aid: str, now: dt.datetime) -> None:
        self.conn.execute(
            "UPDATE candidates SET state=?, decided_by=?, reason=COALESCE(reason, 'accepted'),"
            " decided_at=?, assertion_id=? WHERE id=?",
            (CandidateState.ACCEPTED.value, by, now.isoformat(), aid, cid),
        )
        audit.append(
            self.conn,
            now,
            by,
            "candidate_decided",
            {"candidate_id": cid, "state": "accepted", "assertion_id": aid},
        )

    def _insert_version(self, a: Assertion, now: dt.datetime, cs: int) -> None:
        self.conn.execute(
            f"INSERT INTO assertion_versions({', '.join(_AV_COLUMNS)})"
            f" VALUES ({', '.join('?' * len(_AV_COLUMNS))})",
            (
                a.id,
                a.subject,
                a.predicate,
                a.object_value,
                a.object_kind,
                a.object_datatype,
                a.epistemic_type.value,
                a.status.value,
                a.confidence,
                a.confidence_method,
                _iso(a.valid_from),
                _iso(a.valid_to),
                a.extracted_by,
                a.approved_by,
                a.candidate_id,
                a.recorded_at.isoformat(),
                now.isoformat(),
                cs,
            ),
        )

    def _insert_evidence(self, aid: str, ev: Evidence, cs: int) -> None:
        self.conn.execute(
            "INSERT INTO evidence VALUES (?,?,?,?,?,?)",
            (aid, ev.source_id, ev.source_version, ev.quote, ev.locator, cs),
        )

    def _apply_add_evidence(self, op: AddEvidence, cs: int) -> str:
        a = self.get_assertion(op.assertion_id)
        if a is None or a.status is not Status.ACCEPTED:
            raise UnauthorizedCommit(f"{op.assertion_id} is not accepted knowledge")
        if op.candidate_id:
            sc = self.get_candidate(op.candidate_id)
            assert sc is not None
            self._check_candidate(op.candidate_id, sc.candidate.extracted_by, op.approved_by, op)
        for ev in op.evidence:
            self._insert_evidence(a.id, ev, cs)
        if op.candidate_id:
            self._close_candidate(op.candidate_id, op.approved_by, a.id, self.clock.now())
        return a.id

    def _apply_retract(self, op: Retract, cs: int, now: dt.datetime) -> str:
        if op.status is Status.ACCEPTED:
            raise ValueError("retract status must be retracted or superseded")
        a = self.get_assertion(op.assertion_id)
        if a is None or a.status is not Status.ACCEPTED:
            raise UnauthorizedCommit(f"{op.assertion_id} is not accepted knowledge")
        self.conn.execute(
            "UPDATE assertion_versions SET tx_to=?, cs_to=? WHERE id=? AND cs_to IS NULL",
            (now.isoformat(), cs, a.id),
        )
        self._insert_version(a.model_copy(update={"status": op.status}), now, cs)
        return a.id
