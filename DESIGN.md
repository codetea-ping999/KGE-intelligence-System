# KGE PoC 設計書（最小構成 v0.1）

作成日: 2026-10-15 / 状態: フェーズ0〜5 と、フェーズ6のうち RDF/PROV-O 出力・Claude API 接続を実装済み（README.md 参照）

## 0. 方針の要約

- **Python 3.12 + SQLite（標準ライブラリ）だけで動く**ローカル完結の構成にする。外部DBやサーバは使わない。
- 知識は「主語・述語・目的語」のトリプルを**アサーション（記述）として実体化**し、その上に認識論的種別（事実・推論・仮説）、出典、時刻、有効期間、信頼度、状態を持たせる。
- **LLMの出力は必ず `candidate`（候補）として隔離領域に入る。** スキーマ検証・重複/矛盾検査・独立した出典の確認・承認（人手またはルール）を通過したものだけが `accepted` になる。LLM単独の出力が `fact` かつ `accepted` になる経路はコード上に存在させない。
- すべての変更は**チェンジセット（コミット）単位で追記のみ**。過去状態の再現、差分、影響分析、監査はこのログから導く。

## 1. アーキテクチャ

```
           ┌──────────────┐
 入力文書 → │  Extractor   │  ルール抽出 / LLM抽出(アダプタ) / 手入力
           └──────┬───────┘
                  │ CandidateAssertion（常に status=candidate）
           ┌──────▼───────┐
           │   Staging    │  隔離領域。確定知識の照会には出てこない
           └──────┬───────┘
                  │
           ┌──────▼───────────────────────────────┐
           │  Validation Gate                      │
           │  1 スキーマ検証（オントロジー）         │
           │  2 重複検出（完全一致・実体名寄せ）     │
           │  3 矛盾検出（関数的述語・型排他・期間）  │
           │  4 出典検査（LLM由来は独立出典が必須）   │
           └──────┬───────────────────────────────┘
                  │ ValidationReport
           ┌──────▼───────┐
           │ Review/Policy│  自動承認ポリシー or 人手承認(CLI)
           └──────┬───────┘
                  │ accept / reject / defer
           ┌──────▼───────┐      ┌──────────────┐
           │ Commit (Store)│ ───→ │ Audit Log    │ ハッシュ連鎖・追記のみ
           └──────┬───────┘      └──────────────┘
                  │ ChangeSet
      ┌───────────┼─────────────┐
┌─────▼─────┐ ┌───▼────────┐ ┌──▼──────────┐
│ Reasoner  │ │ Diff/Impact│ │ QA Reporter │
│ 推論ルール │ │ 差分・影響  │ │ 品質指標    │
└───────────┘ └────────────┘ └─────────────┘
```

| モジュール | 役割 | 主な依存 |
|---|---|---|
| `kge.models` | データモデル（Pydantic v2） | pydantic |
| `kge.ontology` | オントロジーYAMLの読込とスキーマ検証 | pyyaml |
| `kge.store` | SQLiteへの永続化、時点照会（as-of） | sqlite3 |
| `kge.extract` | 抽出器インタフェース、ルール抽出、LLMアダプタ（既定はリプレイ用モック） | — |
| `kge.validate` | 重複・矛盾・出典検査、ValidationReport生成 | — |
| `kge.review` | 承認ポリシーと人手承認キュー | — |
| `kge.reason` | 前向き推論ルール、根拠（justification）記録 | — |
| `kge.versioning` | チェンジセット、差分、影響分析 | — |
| `kge.audit` | ハッシュ連鎖監査ログと改ざん検証 | hashlib |
| `kge.qa` | 品質指標と回帰チェック | — |
| `kge.cli` | `kge ingest / candidates / accept / reject / query / diff / impact / retract / audit / qa / demo` | argparse（依存削減のため typer から変更） |

**採用しなかった選択肢**: RDFストア（rdflib/Oxigraph）やNeo4jは表現力が高いが、PoC段階では依存と運用が増える。SQLiteの表設計をRDF*/PROV-Oに素直に対応させておき、フェーズ6でエクスポートを用意して移行可能性を確保する。

## 2. データモデル

### 2.1 主要概念

| 概念 | 説明 |
|---|---|
| Entity | 実体。`id`, `type`, `label`, `aliases` |
| Source | 出典。`id`, `uri`, `title`, `content_hash`(sha256), `retrieved_at`, `kind`（document / dataset / human / llm） |
| Assertion | 知識の最小単位。下表参照 |
| Evidence | アサーションと出典の対応。`source_id`, `locator`（ページ・文字範囲）, `quote` |
| Justification | 推論・仮説の根拠。`assertion_id`, `rule_id`, `premise_ids[]` |
| ChangeSet | 1回の確定操作。`id`, `parent_id`, `author`, `created_at`, `message`, `ops[]` |
| ReviewDecision | 承認判断。`assertion_id`, `decision`, `reviewer`, `reason`, `at` |
| AuditEntry | 監査ログ。`seq`, `at`, `actor`, `action`, `payload`, `prev_hash`, `hash` |

### 2.2 Assertion の属性

| 属性 | 型 | 説明 |
|---|---|---|
| `id` | str | 内容から導出しない一意ID（ULID） |
| `subject` / `predicate` / `object` | str | 目的語は実体IDかリテラル（`object_kind` で区別） |
| `epistemic_type` | enum | `fact`（出典で直接裏付け）/ `inference`（ルールで導出）/ `hypothesis`（未検証の仮説、LLMの推測を含む） |
| `status` | enum | `candidate` → `accepted` / `rejected`、確定後は `retracted` / `superseded` |
| `confidence` | float 0–1 | 信頼度 |
| `confidence_method` | str | 算出方法（`source_prior`, `rule_min`, `llm_self_report`, `human` など） |
| `valid_from` / `valid_to` | date/None | 有効期間（現実世界で成り立つ期間）。None は無期限 |
| `recorded_at` | datetime | 記録時刻（システムが知った時刻） |
| `superseded_at` | datetime/None | 確定知識から外れた時刻。`recorded_at` と合わせて取引時間軸を作る |
| `extracted_by` | str | 抽出器ID（`rule:v1`, `llm:<model>@<prompt_hash>`, `human:<name>`） |
| `evidence[]` / `justification` | — | 事実は1件以上のEvidence、推論はJustificationが必須 |
| `changeset_id` | str | 確定したチェンジセット |

有効期間と記録時刻の2軸（バイテンポラル）を持つので、「2026-10-01時点でシステムが何を知っていたか」と「2020年に何が成り立っていたか」を別々に照会できる。

### 2.3 種別ごとの不変条件

- `fact`: 種別が `llm` 以外の出典を持つEvidenceが1件以上。
- `inference`: Justificationがあり、前提はすべて `accepted`。信頼度は前提の最小値×ルール係数。
- `hypothesis`: `accepted` になっても照会時は既定で除外し、明示指定で含める。
- LLM抽出のアサーションは `fact` を申告できても、独立出典の照合が取れるまでは `hypothesis` に降格して保存する。

### 2.4 SQLite テーブル

`entities`, `sources`, `assertion_versions`, `evidence`, `justifications`, `justification_premises`, `changesets`, `changeset_ops`, `candidates`（検証レポートと承認判断を含む）, `audit_log`。`assertion_versions` は版を閉じる以外の更新をトリガで禁止し、状態変化は新しい版の行と `superseded_at` で表す。

### 2.5 オントロジー（例）

```yaml
entity_types:
  Person: {}
  Organization: {}
  Drug: {}
  Disease: {disjoint_with: [Drug]}
predicates:
  works_for:   {domain: Person, range: Organization, functional: true, temporal: true}
  treats:      {domain: Drug, range: Disease}
  founded_in:  {domain: Organization, range: xsd:gYear, functional: true}
rules:
  - id: R1_employer_country
    if:   ["?p works_for ?o", "?o located_in ?c"]
    then: "?p works_in_country ?c"
    confidence_factor: 0.9
```

## 3. 検証ロジック

| 検査 | 判定 |
|---|---|
| スキーマ | 述語が定義済み、主語・目的語の型がdomain/rangeに合致、リテラルの型、有効期間の前後関係 |
| 完全重複 | `(subject, predicate, object)` が同じで有効期間が重なる確定知識がある → 新規作成せずEvidenceを追加 |
| 実体の重複 | 正規化ラベル（NFKC・小文字化・空白除去）と別名の一致 → 統合候補として報告（自動統合はしない） |
| 関数的述語の矛盾 | `functional: true` の述語で、同じ主語・異なる目的語・期間が重なる |
| 型の排他 | ある実体が `disjoint_with` の型を同時に持つ |
| 出典 | 事実にEvidenceがない、出典ハッシュが不一致、LLM出典のみ |

矛盾は「拒否」ではなく**報告して人手判断に回す**。どちらを残すかは信頼度と出典で提示する。

## 4. 差分更新と影響分析

- 取り込みは対象の出典単位で行い、前回の同一出典の抽出結果と比べて `add / retract / modify` の差分だけをチェンジセットにする（出典の `content_hash` が同じなら何もしない）。
- `kge diff <cs1> <cs2>` は2時点間のアサーション集合の差を出す。
- `kge impact <assertion_id>` は Justification の依存グラフを前向きにたどり、取り消すと根拠を失う推論、関係する実体、再検証が必要な仮説を列挙する。取り消し時は影響を受けた推論を自動で `retracted` にし、同じチェンジセットに含める。

## 5. 監査ログと品質保証

- 監査ログは各エントリが直前エントリのハッシュを含むハッシュ連鎖で、`kge audit verify` で改ざんや欠落を検出する。抽出・検証・承認・確定・取り消しのすべてを記録する。
- 品質指標（`kge qa`）: 制約違反数、未解決の矛盾数、候補の滞留数と滞留日数、Evidenceのない事実の数（常に0であるべき）、種別ごとの件数、LLM由来の比率。
- 回帰テスト: 小さな正解データセット（golden）に対し、取り込み→検証→確定の結果がスナップショットと一致するかを毎回確認する。

## 6. 実行環境

- `uv` で依存を固定（`pyproject.toml` と `uv.lock`）。`uv` がない環境向けに `requirements.lock` も出力する。
- `Makefile`: `make setup`, `make test`, `make demo`, `make qa`, `make check`（lint・型・テスト一式）。
- 任意で `Dockerfile`（python:3.12-slim）と GitHub Actions のCI定義。
- LLMは既定で**録画済み応答のリプレイ**（`fixtures/llm/*.json`）を使い、ネットワークなしで結果が毎回同じになる。実LLM（Claude API）は環境変数で切り替えるアダプタとして後付けする。

ディレクトリ構成:

```
kge-poc/
  pyproject.toml  Makefile  README.md
  ontology/base.yaml
  src/kge/{models,ontology,store,extract,validate,review,reason,versioning,audit,qa,cli}.py
  fixtures/{docs,llm,golden}/
  tests/
```

## 7. 受け入れ基準

| # | 要件 | 受け入れ基準（すべて自動テストで確認） |
|---|---|---|
| AC1 | 事実・推論・仮説の区別 | 3種別を保存・照会できる。既定の照会は仮説を含まない。Evidenceのない `fact` と Justificationのない `inference` は保存時に拒否される |
| AC2 | LLM出力の隔離 | LLM抽出器の出力はすべて `candidate` で保存され、確定知識の照会に出てこない。承認なしに `accepted` にする公開APIがない。LLM出典のみの事実は `hypothesis` に降格される |
| AC3 | 出典・時刻・期間・信頼度 | 確定アサーションはすべて出典（または根拠）、`recorded_at`、有効期間、信頼度と算出方法を持つ。as-of照会で過去の任意時点の知識集合を再現できる |
| AC4 | スキーマ検証 | 未定義述語、domain/range違反、リテラル型違反、期間の逆転を検出し、ValidationReportにコード付きで出す |
| AC5 | 重複検出 | 完全重複は新規行を作らずEvidenceを追加する。表記ゆれの実体（全角/半角・大小文字・別名）を統合候補として報告する |
| AC6 | 矛盾検出 | 関数的述語の期間重複矛盾と型排他違反を検出する。期間が重ならない場合は矛盾としない |
| AC7 | 差分更新 | 同じ文書の再取り込みは空のチェンジセット。1文を変更した文書の再取り込みは該当アサーションの差分だけを生む |
| AC8 | 影響分析 | 前提を取り消すと、それに依存する推論（多段を含む）が列挙され、自動で取り消される |
| AC9 | 監査ログ | すべての状態変化に監査エントリがある。ログの1行を書き換えると `audit verify` が失敗する |
| AC10 | 再現性 | クリーン環境で `make setup && make check && make demo` が成功し、demoの出力が2回とも一致する。ネットワーク不要 |
| AC11 | 継続的品質保証 | `make qa` が品質指標を出力し、golden回帰が一致しない場合や「Evidenceのない事実」が1件以上ある場合に非ゼロで終了する |

## 8. 段階的な開発計画

| フェーズ | 内容 | 完了条件 |
|---|---|---|
| 0 基盤 | リポジトリ骨格、pyproject、Makefile、CI、lint/型/テストの土台 | `make check` が空のテストで成功 |
| 1 データモデルと保存 | Pydanticモデル、SQLiteスキーマ、追記型ストア、as-of照会、監査ログ | AC1, AC3, AC9 |
| 2 検証ゲート | オントロジー読込、スキーマ検証、重複・矛盾・出典検査、ValidationReport | AC4, AC5, AC6 |
| 3 取り込みパイプライン | 抽出器インタフェース、ルール抽出、LLMリプレイアダプタ、Staging、承認CLI、確定 | AC2 |
| 4 推論と版管理 | 前向き推論ルール、Justification、チェンジセット、差分、影響分析、連鎖取り消し | AC7, AC8 |
| 5 品質保証 | 品質指標、golden回帰、`make qa`、デモシナリオ | AC10, AC11 |
| 6（任意） | 実LLMアダプタ（Claude API）、RDF/PROV-Oエクスポート、簡易Web閲覧 | 別途合意 |

フェーズ0〜5が最小構成のPoCで、各フェーズの終わりにテストが緑であることを確認してから次へ進む。

## 9. 確認したい点

1. 置き場所: 既定はこのプロジェクトの共有フォルダ（`/mnt/project-files/kge-poc/`）。GitHubリポジトリを使う場合はリポジトリ名を指定してください。
2. デモ用ドメイン: 既定は架空の企業・人物データ（雇用・所在地・設立年）。研究対象の分野があれば合わせます。
3. 実LLM接続はフェーズ6に回し、PoC本体はリプレイで再現性を優先します。
