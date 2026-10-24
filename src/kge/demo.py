"""End-to-end demo scenario. Deterministic: fixed clock + recorded LLM responses."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from kge.clock import DEFAULT_FIXED_START, FixedClock
from kge.extract import LLMExtractor, ReplayLLMClient, RuleExtractor
from kge.models import Assertion, CandidateState
from kge.pipeline import AUTO, Engine, IngestResult, accept_op
from kge.qa import run as run_qa
from kge.qa import show_term
from kge.store import UnauthorizedCommit
from kge.versioning import diff

ROOT = Path(__file__).resolve().parents[2]


def _fmt(e: Engine, a: Assertion) -> str:
    when = ""
    if a.valid_from or a.valid_to:
        when = f" [{a.valid_from or ''}〜{a.valid_to or ''}]"
    return (
        f"{a.id} {e.store.label(a.subject)} --{a.predicate}--> {show_term(e.store, a)}{when}"
        f" ({a.epistemic_type.value}, conf={a.confidence})"
    )


def _summary(r: IngestResult) -> str:
    if r.skipped:
        return f"  {r.source_id} v{r.version}: 内容に変更なし。チェンジセットは作成しません"
    return (
        f"  {r.source_id} v{r.version}: 候補 {len(r.staged)} 件"
        f"（自動承認 {len(r.accepted)} / 要レビュー {len(r.pending)} / 自動却下 {len(r.rejected)}）"
        f"、変更なし {r.unchanged} 件、取り消し {len(r.retracted)} 件、不正出力 {r.invalid} 件"
        f"、チェンジセット {r.changesets}"
    )


def run(db: str = ":memory:", root: Path = ROOT, emit: Callable[[str], None] = print) -> Engine:
    e = Engine.open(db, root / "ontology/base.yaml", FixedClock(DEFAULT_FIXED_START))
    docs = root / "fixtures/docs"
    rule = RuleExtractor(e.ontology)
    llm = LLMExtractor(ReplayLLMClient(root / "fixtures/llm"), e.ontology)

    def read(name: str) -> str:
        return (docs / name).read_text(encoding="utf-8")

    emit("== 1. 台帳 v1 をルール抽出で取り込む")
    emit(_summary(e.ingest("fixtures/registry.txt", read("registry_v1.txt"), rule, "社員台帳")))
    for a in e.store.active():
        emit("  " + _fmt(e, a))

    emit("\n== 2. 同じ台帳を再取り込み（差分更新）")
    emit(_summary(e.ingest("fixtures/registry.txt", read("registry_v1.txt"), rule, "社員台帳")))

    emit("\n== 3. ニュース記事を LLM 抽出（録画応答のリプレイ）で取り込む")
    r = e.ingest("fixtures/news_2026.txt", read("news_2026.txt"), llm, "ニュース記事")
    emit(_summary(r))
    for sc in e.store.candidates(source_id=r.source_id):
        rep = sc.report
        assert rep is not None
        codes = ", ".join(rep.codes()) or "-"
        emit(
            f"  {sc.id} {' / '.join(sc.candidate.key()[:3])} → {sc.state.value},"
            f" {rep.effective_type.value}, 指摘: {codes}"
        )

    emit("\n== 4. LLM 候補を人の承認なしで確定しようとする")
    target = r.pending[0]
    staged = e.store.get_candidate(target)
    assert staged is not None and staged.report is not None
    try:
        e.store.commit(AUTO, "bypass attempt", [accept_op(staged, staged.report, AUTO)])
        emit("  !! 確定できてしまった（不具合）")
    except UnauthorizedCommit as exc:
        emit(f"  拒否されました: {exc}")

    emit("\n== 5. 人がレビューする")
    e.accept(target, "human:reviewer", "記事本文の記述と一致することを確認")
    emit(f"  {target} を承認")
    for cid in r.pending[1:]:
        e.reject(cid, "human:reviewer", "裏付けがなく既存の事実と矛盾する")
        emit(f"  {cid} を却下")
    checkpoint = e.store.head()
    for a in e.store.active(subject=e.store.find_entity_by_label("佐藤ボブ").id):  # type: ignore[union-attr]
        emit("  " + _fmt(e, a))

    emit("\n== 6. 影響分析: 「Acme社 located_in 日本」を変えると何が影響を受けるか")
    located = next(
        a for a in e.store.active(predicate="located_in") if e.store.label(a.subject) == "Acme社"
    )
    imp = e.impact(located.id)
    for item in imp.dependents:
        dep = e.store.get_assertion(item.assertion_id)
        assert dep is not None
        emit(f"  深さ{item.depth}: {_fmt(e, dep)}（{item.via} 経由）")
    emit("  関係する実体: " + ", ".join(e.store.label(x) for x in imp.entities))

    emit("\n== 7. 台帳 v2（Acme社の所在国が変更）を取り込む")
    emit(_summary(e.ingest("fixtures/registry.txt", read("registry_v2.txt"), rule, "社員台帳")))

    emit(f"\n== 8. 差分: チェンジセット {checkpoint} → {e.store.head()}")
    d = diff(e.store, checkpoint, e.store.head())
    for a in d.removed:
        emit("  - " + _fmt(e, a))
    for a in d.added:
        emit("  + " + _fmt(e, a))

    emit("\n== 9. 品質チェック")
    qa = run_qa(e.store, DEFAULT_FIXED_START)
    for c in qa.checks:
        emit(f"  [{'OK' if c.ok else 'NG'}] {c.name} {c.detail}".rstrip())
    m = qa.metrics
    emit(
        f"  確定 {m['active_assertions']} 件 {m['by_type']}、"
        f"候補 {m['candidates']}、LLM由来比率 {m['llm_derived_ratio']}、"
        f"監査ログ {m['audit_entries']} 件"
    )
    pending = e.store.candidates(state=CandidateState.PENDING)
    emit(f"  未レビュー候補: {len(pending)} 件")
    return e
