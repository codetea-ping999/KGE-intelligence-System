"""Command line interface: `kge <command>`. Run `kge -h` for the list of commands."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

from kge import audit, demo, ontology_change, qa
from kge.clock import clock_from_env
from kge.export import DEFAULT_BASE, to_turtle
from kge.extract import (
    ExtractionFailed,
    Extractor,
    LLMClient,
    LLMExtractor,
    LLMReplayMissing,
    ReplayLLMClient,
    RuleExtractor,
)
from kge.llm_claude import LLMError
from kge.models import Assertion, CandidateState, SourceKind
from kge.ontology import Ontology
from kge.pipeline import Engine, ReviewError
from kge.store import OntologyMismatch, UnauthorizedCommit
from kge.versioning import diff


def _engine(args: argparse.Namespace, allow_ontology_change: bool = False) -> Engine:
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    return Engine.open(args.db, args.ontology, clock_from_env(), allow_ontology_change)


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
        client: LLMClient
        if args.llm == "claude":
            from kge.llm_claude import ClaudeLLMClient, RecordingLLMClient  # needs extra

            client = ClaudeLLMClient(model=args.model, effort=args.effort)
            if args.record:
                client = RecordingLLMClient(client, args.llm_fixtures)
        else:
            client = ReplayLLMClient(args.llm_fixtures)
        extractor = LLMExtractor(client, e.ontology)
    path = Path(args.path)
    r = e.ingest(args.uri or path.as_posix(), path.read_text(encoding="utf-8"), extractor)
    print(demo._summary(r))
    return 0


IMPORTERS = {
    # name: (extractor id, source prior, title)
    "wikidata": ("import:wikidata", 0.9, "Wikidata (CC0)"),
    "houjin": ("import:houjin-bangou", 0.98, "国税庁法人番号公表サイト"),
}


def cmd_import(args: argparse.Namespace) -> int:
    """Convert a dataset file to the canonical form and ingest it like any other source."""
    from kge.datasets import DatasetExtractor
    from kge.sources import houjin, wikidata

    e = _engine(args)
    path = Path(args.path)
    raw = path.read_text(encoding="utf-8-sig")
    try:
        content = wikidata.convert(raw) if args.dataset == "wikidata" else houjin.convert(raw)
    except houjin.MalformedCSV as exc:
        print(f"error: nothing imported, {path} has unreadable rows: {exc}", file=sys.stderr)
        return 2
    ident, prior, title = IMPORTERS[args.dataset]
    if args.canonical_out:
        Path(args.canonical_out).write_text(content, encoding="utf-8")
    extractor = DatasetExtractor(e.ontology, e.store, ident, prior)
    uri = args.uri or f"{args.dataset}:{path.name}"
    r = e.ingest(uri, content, extractor, title=title, kind=SourceKind.DATASET)
    print(demo._summary(r))
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """Download Wikidata statements for companies given by corporate number."""
    from kge.sources import wikidata

    numbers = list(args.corporate_number)
    if args.from_db:
        e = _engine(args)
        numbers += [a.object_value for a in e.store.active(predicate="corporate_number")]
    Path(args.out).write_text(wikidata.fetch(numbers), encoding="utf-8")
    print(f"wrote {args.out}")
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
        e = _engine(args, allow_ontology_change=True)  # a mismatch is reported, not fatal
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
    store = _engine(args).store
    if args.format == "json":
        text = json.dumps(qa.export_snapshot(store), ensure_ascii=False, indent=2, default=str)
        text += "\n"
    else:
        text = to_turtle(store, base=args.base, include_history=not args.active_only)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_ontology(args: argparse.Namespace) -> int:
    if args.action == "show":
        return _show_ontology(Ontology.load(args.ontology))
    e = _engine(args, allow_ontology_change=True)
    if args.action == "log":
        for v in e.store.ontology_versions():
            print(
                f"v{v.version}  after changeset {v.after_changeset}  {v.recorded_at.isoformat()}"
                f"  {v.actor}  {v.content_hash[:12]}  {v.reason}"
            )
        if e.store.pending_ontology is not None:
            print(f"pending: {args.ontology} differs from the ontology in effect")
        return 0
    if args.action == "diff":
        if len(args.versions) != 2:
            print("error: ontology diff needs two versions, e.g. 'kge ontology diff 1 2'")
            return 2
        a, b = (e.store.ontology_version(v) for v in args.versions)
        for c in ontology_change.diff_ontologies(a, b):
            print(c)
        return 0
    new = Ontology.load(args.ontology)
    if args.action == "plan":
        p = ontology_change.plan(e, new)
        _print_plan(e, p)
        return 0
    if not args.actor or not args.reason:
        print("error: ontology apply needs --actor and --reason")
        return 2
    r = ontology_change.apply(e, new, args.actor, args.reason, args.retract_invalid)
    _print_plan(e, r.plan)
    print(
        f"applied ontology v{r.version}: retracted {len(r.retracted)}, derived {len(r.derived)},"
        f" changesets {r.changesets}"
    )
    return 0


def _print_plan(e: Engine, p: ontology_change.OntologyPlan) -> None:
    if p.empty:
        print(f"no changes: v{e.store.ontology_versions()[-1].version} is up to date")
        return
    print(f"changes against v{p.from_version}:")
    for c in p.changes:
        print(f"  {c}")
    sections = [
        ("stated knowledge that no longer conforms", p.invalid),
        ("inferences to withdraw", p.stale_inferences),
        ("pending candidates whose validation changes", p.candidates),
    ]
    for title, findings in sections:
        if findings:
            print(f"{title}:")
            for f in findings:
                shown = f.assertion_id
                a = e.store.get_assertion(f.assertion_id)
                if a is not None:
                    shown = _fmt(e, a)
                print(f"  {shown} — {f.reason}")
    if p.retractions:
        print(f"retractions incl. cascade: {', '.join(r.assertion_id for r in p.retractions)}")
    if p.conflicts:
        print(f"cardinality conflicts to resolve by review: {', '.join(p.conflicts)}")
    if p.entity_conflicts:
        print(f"entities with disjoint types: {', '.join(p.entity_conflicts)}")
    for b in p.blocking(retract_invalid=False):
        print(f"note: {b}")


def _show_ontology(onto: Ontology) -> int:
    """Print the class hierarchy, predicates with their characteristics, and all rules."""

    def tree(parent: str | None, depth: int) -> None:
        for name, t in sorted(onto.entity_types.items()):
            if t.subclass_of == parent:
                extra = f"  ⊥ {', '.join(t.disjoint_with)}" if t.disjoint_with else ""
                print(f"  {'  ' * depth}{name}{extra}")
                tree(name, depth + 1)

    print("classes:")
    tree(None, 0)
    print("predicates:")
    for name, pd in sorted(onto.predicates.items()):
        traits = [
            label
            for label, on in [
                ("functional", pd.functional),
                (f"max {pd.max_cardinality}", pd.max_cardinality and not pd.functional),
                ("temporal", pd.temporal),
                ("symmetric", pd.symmetric),
                ("transitive", pd.transitive),
                (f"inverse of {pd.inverse_of}", pd.inverse_of),
                (f"sub-property of {pd.subproperty_of}", pd.subproperty_of),
            ]
            if on
        ]
        print(
            f"  {name}: {pd.domain} → {pd.range}" + (f"  [{', '.join(traits)}]" if traits else "")
        )
    print("rules:")
    for r in onto.all_rules():
        origin = f"axiom:{r.axiom}" if r.axiom else "hand-written"
        print(f"  {r.id} ({origin}): {' , '.join(r.if_)} ⇒ {r.then}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kge", description="Knowledge Graph Engineering PoC")
    p.add_argument("--db", default=os.environ.get("KGE_DB", "build/kge.db"))
    p.add_argument("--ontology", default=os.environ.get("KGE_ONTOLOGY", "ontology/base.yaml"))
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("import", help="ingest a structured dataset (Wikidata JSON, 法人番号 CSV)")
    s.add_argument("dataset", choices=sorted(IMPORTERS))
    s.add_argument("path")
    s.add_argument("--uri", help="source identity for incremental re-imports")
    s.add_argument("--canonical-out", help="also write the canonical statements to this file")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("fetch", help="download Wikidata statements (needs network access)")
    s.add_argument("dataset", choices=["wikidata"])
    s.add_argument("--corporate-number", action="append", default=[])
    s.add_argument("--from-db", action="store_true", help="every corporate number in the db")
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("ingest", help="extract candidates from a document and validate them")
    s.add_argument("path")
    s.add_argument("--extractor", choices=["rule", "llm"], default="rule")
    s.add_argument(
        "--llm",
        choices=["replay", "claude"],
        default="replay",
        help="replay recorded responses (default) or call the Claude API",
    )
    s.add_argument("--model", default="claude-opus-5-5")
    s.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    s.add_argument(
        "--record",
        action="store_true",
        help="with --llm claude: save responses to --llm-fixtures for replay",
    )
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

    s = sub.add_parser(
        "ontology",
        help="show the ontology, or plan / apply / list / diff its versions",
        description="show: hierarchy, predicates and compiled rules of the ontology file."
        " plan: what applying the file would change. apply: make it the ontology in effect."
        " log: list versions. diff A B: compare two recorded versions.",
    )
    s.add_argument(
        "action", nargs="?", default="show", choices=["show", "plan", "apply", "log", "diff"]
    )
    s.add_argument("versions", nargs="*", type=int, help="diff: two version numbers")
    s.add_argument("--actor", help="apply: the human approving the change (human:<name>)")
    s.add_argument("--reason", help="apply: why the ontology changes")
    s.add_argument(
        "--retract-invalid",
        action="store_true",
        help="apply: retract stated knowledge that no longer conforms",
    )
    s.set_defaults(func=cmd_ontology)

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

    s = sub.add_parser("export", help="export knowledge as JSON or RDF Turtle (PROV-O)")
    s.add_argument("--format", choices=["json", "turtle"], default="json")
    s.add_argument("--base", default=DEFAULT_BASE, help="base IRI for turtle output")
    s.add_argument(
        "--active-only", action="store_true", help="turtle: omit retracted/superseded assertions"
    )
    s.add_argument("--out", help="write to a file instead of stdout")
    s.set_defaults(func=cmd_export)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (
        ReviewError,
        UnauthorizedCommit,
        OntologyMismatch,
        KeyError,
        ExtractionFailed,
        LLMReplayMissing,
        LLMError,
        OSError,  # unreadable files, unreachable endpoints
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
