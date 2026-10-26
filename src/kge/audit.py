"""Append-only, hash-chained audit log stored in the same SQLite database."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

GENESIS = "0" * 64


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _entry_hash(prev_hash: str, seq: int, at: str, actor: str, action: str, payload: str) -> str:
    body = canonical_json([prev_hash, seq, at, actor, action, payload])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def append(
    conn: sqlite3.Connection, at: dt.datetime, actor: str, action: str, payload: dict[str, Any]
) -> int:
    row = conn.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    seq, prev = (row[0] + 1, row[1]) if row else (1, GENESIS)
    at_s = at.isoformat()
    body = canonical_json(payload)
    h = _entry_hash(prev, seq, at_s, actor, action, body)
    conn.execute(
        "INSERT INTO audit_log(seq, at, actor, action, payload, prev_hash, hash)"
        " VALUES (?,?,?,?,?,?,?)",
        (seq, at_s, actor, action, body, prev, h),
    )
    return seq


@dataclass
class VerifyResult:
    ok: bool
    entries: int
    first_bad_seq: int | None = None
    message: str = "ok"


def verify(conn: sqlite3.Connection) -> VerifyResult:
    prev = GENESIS
    expected_seq = 1
    n = 0
    for seq, at, actor, action, payload, prev_hash, h in conn.execute(
        "SELECT seq, at, actor, action, payload, prev_hash, hash FROM audit_log ORDER BY seq"
    ):
        n += 1
        if seq != expected_seq:
            return VerifyResult(False, n, seq, f"gap: expected seq {expected_seq}, found {seq}")
        if prev_hash != prev:
            return VerifyResult(False, n, seq, "prev_hash does not match previous entry")
        if _entry_hash(prev, seq, at, actor, action, payload) != h:
            return VerifyResult(False, n, seq, "entry content does not match its hash")
        prev = h
        expected_seq += 1
    return VerifyResult(True, n)


def entries(conn: sqlite3.Connection, action: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT seq, at, actor, action, payload FROM audit_log"
    args: tuple[Any, ...] = ()
    if action:
        sql += " WHERE action = ?"
        args = (action,)
    out = []
    for seq, at, actor, act, payload in conn.execute(sql + " ORDER BY seq", args):
        out.append(
            {"seq": seq, "at": at, "actor": actor, "action": act, "payload": json.loads(payload)}
        )
    return out
