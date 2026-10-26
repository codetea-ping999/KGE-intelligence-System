"""Command line interface: `kge <command>`. Run `kge -h` for the list of commands."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

from kge import audit, demo, qa
from kge.clock import clock_from_env
from kge.extract import ExtractionFailed, Extractor, LLMExtractor, ReplayLLMClient, RuleExtractor
from kge.models import Assertion, CandidateState
from kge.pipeline import Engine, ReviewError
from kge.store import UnauthorizedCommit
from kge.versioning import diff


def _engine(args: argparse.Namespace) -> Engine:
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    return Engine.open(args.db, args.ontology, clock_from_env())


def _fmt(e: Engine, a: Assertion) -> str:
    return demo._fmt(e, a) + f" [{a.status.value}]"


def _print_json(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_ingest(args: argparse.Namespace) -> int:
    e = _engine(args)
    extractor: Extractor
    if args.extractor == "rule":
        extractor = RuleExtractor(e.ontology)
    else:
        extractor = LLMExtractor(ReplayLLMClient(args.llm_fixtures), e.ontology)
    path = Path(args.path)
    r = e.ingest(args.uri or path.as_posix(), path.read_text(encoding="utf-8"), extractor)
    print(demo._summary(r))
    return 0


def cmd_candidates(args: argparse.Namespace) -> int:
    e = _engine(args)
    state = CandidateState(args.state) if args.state != "all" else None
    for sc in e.store.candidates(state=state):
        rep = sc.report
        codes = ", ".join(rep.codes()) if rep else "unvalidated"
        eff = rep.effective_type.value if rep else "?"
        print(f"{sc.id} [{sc.state.value}] {' / '.join(sc.candidate.key()[:3])} ({eff}) {codes}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    e = _engine(args)
    if args.id.startswith("C"):
        sc = e.store.get_candidate(args.id)
        if sc is None:
            print(f"unknown candidate {args.id}", file=sys.stderr)
            return 1
        _print_json(sc.model_dump(mode="json"))
    else:
        hist = e.store.history(args.id)
        if not hist:
            print(f"unknown assertion {args.id}", file=sys.stderr)
            return 1
        _print_json([h.model_dump(mode="json") for h in hist])
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    e = _engine(args)
    results = e.accept(args.id, args.reviewer, args.reason, args.supersede or [])
    print(f"accepted {args.id}: changesets {[r.changeset for r in results]}")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    _engine(args).reject(args.id, args.reviewer, args.reason)
    print(f"rejected {args.id}")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    e = _engine(args)
    at = dt.datetime.fromisoformat(args.as_of) if args.as_of else None
    rows = e.store.snapshot(args.as_of_changeset, at, include_hypotheses=args.hypotheses)
    for a in rows:
        if args.subject and e.store.label(a.subject) != args.subject:
            continue
        if args.predicate and a.predicate != args.predicate:
            continue
        print(_fmt(e, a))
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    for cs in _engine(args).store.changesets():
        ops = ", ".join(f"{o['op']}:{o['target']}" for o in cs.ops)
        print(f"#{cs.id} {cs.created_at.isoformat()} {cs.author}: {cs.message}\n    {ops}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    e = _engine(args)
    d = diff(e.store, args.base, args.target)
    for a in d.removed:
        print("- " + _fmt(e, a))
    for a in d.added:
        print("+ " + _fmt(e, a))
    return 0


def cmd_impact(args: argparse.Namespace) -> int:
    e = _engine(args)
    rep = e.impact(args.id)
    for item in rep.dependents:
        a = e.store.get_assertion(item.assertion_id)
        assert a is not None
        print(f"depth {item.depth} via {item.via}: {_fmt(e, a)}")
    print("entities: " + ", ".join(f"{x}:{e.store.label(x)}" for x in rep.entities))
    return 0


def cmd_retract(args: argparse.Namespace) -> int:
    results = _engine(args).retract(args.id, args.reason, args.actor)
    print(f"retracted {results[0].retracted} in changeset {results[0].changeset}")
    return 0


def cmd_alias(args: argparse.Namespace) -> int:
    _engine(args).merge_alias(args.entity, args.alias, args.reviewer)
    print(f"added alias {args.alias!r} to {args.entity}")
    return 0


def cmd_reason(args: argparse.Namespace) -> int:
    results = _engine(args).reasoner.run()
    print(f"{sum(len(r.created) for r in results)} inferences in {len(results)} changesets")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    e = _engine(args)
    if args.action == "verify":
        v = audit.verify(e.store.conn)
        print(f"{'OK' if v.ok else 'TAMPERED'}: {v.entries} entries, {v.message}")
        return 0 if v.ok else 1
    for entry in audit.entries(e.store.conn):
        print(json.dumps(entry, ensure_ascii=False))
    return 0


def cmd_qa(args: argparse.Namespace) -> int:
    reports = []
    if Path(args.db).exists():
        e = _engine(args)
        reports.append(("database " + args.db, qa.run(e.store, e.store.clock.now())))
    # Golden regression: rerun the reference scenario from scratch and compare.
    with tempfile.TemporaryDirectory() as tmp:
        ref = demo.run(str(Path(tmp) / "ref.db"), emit=lambda _: None)
        snap = qa.export_snapshot(ref.store)
        golden = Path(args.golden)
        if args.update_golden:
            golden.parent.mkdir(parents=True, exist_ok=True)
            golden.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", "utf-8")
            print(f"golden updated: {golden}")
        ref_report = qa.run(ref.store, ref.store.clock.now())
        ref_report.checks.append(qa.golden_check(snap, golden))
        reports.append(("reference scenario", ref_report))
        ref.store.close()
    ok = True
    for name, rep in reports:
        print(f"## {name}")
        for c in rep.checks:
            print(f"  [{'OK' if c.ok else 'NG'}] {c.name} {c.detail}".rstrip())
        print("  metrics: " + json.dumps(rep.metrics, ensure_ascii=False))
        ok = ok and rep.ok
    return 0 if ok else 1


def cmd_demo(args: argparse.Namespace) -> int:
    if args.db != ":memory:":
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
        if Path(args.db).exists():
            Path(args.db).unlink()
    demo.run(args.db)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    _print_json(qa.export_snapshot(_engine(args).store))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kge", description="Knowledge Graph Engineering PoC")
    p.add_argument("--db", default=os.environ.get("KGE_DB", "build/kge.db"))
    p.add_argument("--ontology", default=os.environ.get("KGE_ONTOLOGY", "ontology/base.yaml"))
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("ingest", help="extract candidates from a document and validate them")
    s.add_argument("path")
    s.add_argument("--extractor", choices=["rule", "llm"], default="rule")
    s.add_argument("--llm-fixtures", default="fixtures/llm")
    s.add_argument("--uri")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("candidates", help="list staged candidates")
    s.add_argument("--state", default="pending", choices=[*CandidateState, "all"])
    s.set_defaults(func=cmd_candidates)

    s = sub.add_parser("show", help="show a candidate (C...) or an assertion's history (A...)")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)

    for name, func in (("accept", cmd_accept), ("reject", cmd_reject)):
        s = sub.add_parser(name, help=f"{name} a pending candidate (human review)")
        s.add_argument("id")
        s.add_argument("--reviewer", required=True, help="human:<name>")
        s.add_argument("--reason", required=True)
        if name == "accept":
            s.add_argument("--supersede", nargs="*", help="assertion ids this candidate replaces")
        s.set_defaults(func=func)

    s = sub.add_parser("query", help="list accepted knowledge")
    s.add_argument("--subject")
    s.add_argument("--predicate")
    s.add_argument("--hypotheses", action="store_true", help="include hypotheses")
    s.add_argument("--as-of-changeset", type=int)
    s.add_argument("--as-of", help="ISO 8601 transaction time")
    s.set_defaults(func=cmd_query)

    sub.add_parser("log", help="list changesets").set_defaults(func=cmd_log)

    s = sub.add_parser("diff", help="difference between two changesets")
    s.add_argument("base", type=int)
    s.add_argument("target", type=int)
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("impact", help="what depends on an assertion")
    s.add_argument("id")
    s.set_defaults(func=cmd_impact)

    s = sub.add_parser("retract", help="retract an assertion and its dependent inferences")
    s.add_argument("id")
    s.add_argument("--actor", required=True)
    s.add_argument("--reason", required=True)
    s.set_defaults(func=cmd_retract)

    s = sub.add_parser("alias", help="record that a label is an alias of an entity")
    s.add_argument("entity")
    s.add_argument("alias")
    s.add_argument("--reviewer", required=True)
    s.set_defaults(func=cmd_alias)

    sub.add_parser("reason", help="run the reasoner to fixpoint").set_defaults(func=cmd_reason)

    s = sub.add_parser("audit", help="verify or print the audit log")
    s.add_argument("action", choices=["verify", "log"])
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("qa", help="quality checks, metrics and golden regression")
    s.add_argument("--golden", default="fixtures/golden/demo_snapshot.json")
    s.add_argument("--update-golden", action="store_true")
    s.set_defaults(func=cmd_qa)

    s = sub.add_parser("demo", help="run the reference scenario")
    s.add_argument("--db", default="build/demo.db")
    s.set_defaults(func=cmd_demo)

    sub.add_parser("export", help="print accepted knowledge as JSON").set_defaults(func=cmd_export)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ReviewError, UnauthorizedCommit, KeyError, ExtractionFailed) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
