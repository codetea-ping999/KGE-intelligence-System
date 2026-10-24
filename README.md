# KGE-intelligence-System

Knowledge Graph Engineering（KGE）の研究用 PoC です。知識の抽出・統合・検証・バージョン管理・継続的品質保証をひとつにまとめた最小構成です。
設計の背景は [DESIGN.md](DESIGN.md) を参照してください。

## すぐ試す

```bash
make setup      # uv で依存を固定バージョンのまま導入（初回のみネットワークが必要）
make check      # ruff + mypy(strict) + pytest
make demo       # 参照シナリオを実行して build/demo.db を作る
make qa         # 品質ゲート（不変条件・指標・golden 回帰）。違反があれば非ゼロで終了
make repro      # デモを2回実行し、出力がバイト単位で一致することを確認
```

`uv` がない場合は `pip install -r requirements.lock && pip install -e '.[claude]'` でも動きます（Python 3.12 以上）。
Docker なら `docker build -t kge-poc . && docker run --rm kge-poc` でデモが流れます。

## 仕組み（要点）

```
文書 → Extractor → Staging(候補) → Validation Gate → Review/Policy → Commit → Reasoner
                                                                    ↘ Audit Log（ハッシュ連鎖）
```

- **LLM の出力は確定知識に直接入りません。** LLM 抽出器の出力は必ず候補（`candidate`）として隔離され、
  検証を通ったうえで `human:<名前>` のレビュアーが承認したものだけが確定します。
  この制約はレビュー処理だけでなく保存層（`Store.commit`）とデータモデルでも強制しているため、迂回できません。
- LLM が「事実」と主張しても、引用が元文書で確認できなければ **仮説に格下げ** されます。自己申告の信頼度は 0.7 で頭打ちです。
- 推論はルール（`ontology/base.yaml`）からのみ生まれ、根拠（ルールと前提）を記録します。
  前提が取り消されると、依存する推論は多段にわたって同じチェンジセットで取り消されます。
- 知識の行は追記のみで、取り消しや置き換えは新しい版の行として残ります（SQLite のトリガで更新・削除を禁止）。
  チェンジセット番号または時刻を指定して、過去の任意時点の知識を再現できます。

## CLI

```bash
export KGE_DB=build/kge.db             # 既定値
uv run kge ingest fixtures/docs/registry_v1.txt --uri registry        # ルール抽出
uv run kge ingest fixtures/docs/news_2026.txt --extractor llm          # LLM 抽出（録画応答）
uv run kge candidates                                                  # レビュー待ち一覧
uv run kge show C0008                                                  # 候補と検証レポート
uv run kge accept C0008 --reviewer human:you --reason "本文で確認"
uv run kge accept C0009 --reviewer human:you --reason "..." --supersede A00005   # 矛盾を解消して置換
uv run kge reject C0010 --reviewer human:you --reason "根拠なし"
uv run kge query --hypotheses          # --as-of-changeset N / --as-of 2026-10-15T00:00:00+00:00
uv run kge log                         # チェンジセット一覧
uv run kge diff 3 9                    # 2時点の差分
uv run kge impact A00003               # 影響分析
uv run kge retract A00003 --actor human:you --reason "..."   # 依存する推論ごと取り消し
uv run kge audit verify                # 監査ログの改ざん検査
uv run kge qa                          # 品質ゲート
uv run kge ontology                    # クラス階層・述語の特性・推論ルールの一覧
uv run kge ontology plan               # オントロジーファイルの変更が既存知識に与える影響
uv run kge ontology apply --actor human:you --reason "..."   # 変更を適用（版を記録して移行）
uv run kge ontology log                # オントロジーの版の一覧
uv run kge ontology diff 1 2           # 2つの版の差分
```

`KGE_CLOCK=fixed` を指定すると時計が固定され、実行結果が完全に再現されます。

## RDF / PROV-O 形式での出力

```bash
uv run kge export --format turtle --out build/kge.ttl          # 取り消し済みの記述も含む全履歴
uv run kge export --format turtle --active-only                # 確定中の知識だけ
```

各アサーションは `rdf:Statement` かつ `prov:Entity` として出力され、種別（`kgs:Fact` / `kgs:Inference` / `kgs:Hypothesis`）、信頼度、有効期間、出典（`prov:wasDerivedFrom` → 文書の版）、推論の前提とルール、抽出者（`prov:wasAttributedTo`）と承認者（`kgs:approvedBy`）、取り消し時刻（`prov:invalidatedAtTime`）を持ちます。
確定済みの事実と推論だけは素のトリプル（例: `kge:E0002 kgo:located_in kge:E0007`）としても出力するので、通常の SPARQL では確定知識だけが見えます。
オントロジーは OWL 2（`owl:Class`、`rdfs:subClassOf`、`owl:disjointWith`、`owl:inverseOf`、`owl:FunctionalProperty` など）として先頭に出力します。
IRI の基点は `--base` で変えられます。

## オントロジー

`ontology/base.yaml` には型、述語、推論ルールを書きます。型と述語には次の記述ができます。

- `subclass_of`: クラス階層。例えば `Company` は `Organization` の下位クラスなので、`works_for` の目的語になれます。`disjoint_with`（排他）は下位クラスにも継承されます。
- `inverse_of` / `symmetric` / `transitive` / `subproperty_of`: プロパティの特性です。読み込み時に `AX_` で始まる推論ルールへ変換され、導かれた知識は前提へのリンクを持つ推論として記録されます。前提を取り消すと、そこから導かれた知識も取り消されます。例えば `employs` は `works_for` の逆なので、台帳の勤務記録から「Acme社 employs 田中アリス」が導かれます。
- `functional` / `max_cardinality`: 同じ主語が同じ時期に取れる値の数の上限です。上限を超える候補は矛盾として人手のレビューに回ります。

定義に矛盾があるオントロジー（階層の循環、排他なクラス同士の下位になるクラス、逆プロパティの型が鏡像になっていないもの、など）は読み込み時に拒否されます。`uv run kge ontology` で、階層と変換後のルールを確認できます。

### オントロジーの版管理

オントロジーも知識と同じように版管理します。データベースには、その知識に適用されているオントロジーの版が記録されます（追記のみで、書き換えも削除もできません）。

- オントロジーファイルを書き換えると、そのままではデータベースを開けません。`kge ontology plan` で影響を確認し、`kge ontology apply` で適用します。
- `plan` は読み取り専用で、構造の差分（型・述語・ルール。特性から変換されたルールも含む）、新しい定義に合わなくなる記述、ルールが消えたり変わったりして取り下げる推論（連鎖を含む）、新たに生じる値の数の超過、排他になる型を持つ実体、検証結果が変わるレビュー待ち候補を示します。
- `apply` は人（`human:`）の承認と理由を必要とします。新しい版の記録、取り下げ（理由つきのチェンジセット）、推論のやり直し、レビュー待ち候補の再検証を、1つのトランザクションで行います。人が確認した事実が定義に合わなくなる場合は、`--retract-invalid` を付けない限り適用しません。値の数の超過は自動では解消せず、品質チェックで報告します。
- 各チェンジセットがどの版のもとで書かれたかを辿れるので、過去の時点の知識は当時のオントロジーで解釈できます。RDF 出力では `owl:versionIRI`、`owl:priorVersion`、版ごとの `prov:Entity` で表します。

デモの最後では、`fixtures/ontology/base_v2.yaml`（`works_in_region` を廃止し、組織の所在地域 `located_in_region` を追加）を適用します。そのため `make qa` はデモのデータベースを v2 で検査します。

## 実 LLM（Claude API）での抽出

```bash
uv sync --extra claude                          # anthropic SDK を追加（make setup では導入済み）
export ANTHROPIC_API_KEY=...                    # または `ant auth login` のプロファイル
uv run kge ingest fixtures/docs/news_2026.txt --extractor llm --llm claude --record
```

- 既定のモデルは `claude-opus-5-5`、effort は `medium` です（`--model` と `--effort` で変更できます）。
- 出力は JSON スキーマで構造を固定し（structured outputs）、安全性による拒否には API 側のフォールバック（`fallbacks: "default"`）を有効にしています。
- 実 LLM の応答も、録画応答と同じく候補として隔離され、検証と人の承認を経ないと確定しません。
- `--record` を付けると応答を `fixtures/llm/` に保存し、以後は `--llm replay`（既定）でネットワークなしに同じ結果を再現できます。
- 呼び出しが失敗した場合（認証情報なし、通信エラー、拒否、出力の打ち切り）は、その取り込み全体を取り消し、`ingest_failed` を監査ログに残します。再実行すれば通常どおり処理されます。

## 受け入れ基準とテストの対応

| AC | 内容 | テスト |
|---|---|---|
| AC1 | 事実・推論・仮説の区別 | `tests/test_ac1_epistemic_types.py` |
| AC2 | LLM 出力の隔離 | `tests/test_ac2_llm_isolation.py` |
| AC3 | 出典・時刻・期間・信頼度、過去時点の再現 | `tests/test_ac3_provenance_time.py` |
| AC4 | スキーマ検証 | `tests/test_ac4_schema.py` |
| AC5 | 重複検出 | `tests/test_ac5_duplicates.py` |
| AC6 | 矛盾検出 | `tests/test_ac6_conflicts.py` |
| AC7 | 差分更新 | `tests/test_ac7_incremental.py` |
| AC8 | 影響分析と連鎖取り消し | `tests/test_ac8_impact.py` |
| AC9 | 監査ログと改ざん検知 | `tests/test_ac9_audit.py` |
| AC10, AC11 | 再現性、品質ゲート、CLI 一連操作 | `tests/test_ac10_ac11_reproducibility_qa.py` |
| フェーズ6 | RDF/PROV-O 出力、Claude API 接続と録画 | `tests/test_phase6_export.py`, `tests/test_phase6_claude_adapter.py` |
| フェーズ7a | クラス階層、プロパティ特性、カーディナリティ、OWL 出力 | `tests/test_ontology_semantics.py` |
| フェーズ7b | オントロジーの版管理、影響分析、承認つきの移行 | `tests/test_ontology_versioning.py` |

## 構成

```
ontology/base.yaml        クラス階層・述語（domain/range/関数性/時間性/逆・対称・推移）・推論ルール
src/kge/models.py         データモデルと不変条件
src/kge/store.py          SQLite ストア（追記型・版管理・コミット時の権限検査）
src/kge/validate.py       検証ゲート（スキーマ・重複・矛盾・出典）
src/kge/extract.py        ルール抽出器 / LLM 抽出器 + 録画応答リプレイ
src/kge/pipeline.py       取り込み・差分更新・レビュー・取り消し
src/kge/reason.py         前向き推論
src/kge/versioning.py     差分・影響分析
src/kge/ontology_change.py オントロジーの差分・影響分析・移行
src/kge/audit.py          ハッシュ連鎖監査ログ
src/kge/qa.py             品質指標・不変条件チェック・golden 回帰
src/kge/export.py         RDF (Turtle) / PROV-O 出力
src/kge/llm_claude.py     Claude API アダプタと応答の録画
src/kge/demo.py, cli.py   参照シナリオと CLI
fixtures/                 デモ文書（架空データ）、録画済み LLM 応答、golden スナップショット
```

## 既知の制約（PoC の範囲外）

- 録画応答はプロンプトの SHA-256 先頭16桁をキーにしているため、プロンプトや文書を変えた場合は応答の再録画（`--llm claude --record`）が必要です。
- Claude API との接続は、テストでは偽のクライアントで要求の形と失敗時の動作を確認しています。実際の API 呼び出しは CI では行いません。
- 実体の統合は別名の登録（`kge alias`）までで、実体IDの付け替えは行いません。
- 同じ結論に複数の根拠がある場合、最初に見つかった根拠だけを記録します。その根拠が取り消されると、次の推論実行で別の根拠から再導出されます。
- ルール抽出器は台帳形式の定型文だけに対応しています。
- オントロジーの意味は、ルールに変換して前向き推論で扱える範囲（階層、排他、逆・対称・推移・部分プロパティ、値の数の上限）に限ります。OWL 全体の推論（存在量化や等価クラスなど）は行いません。
- 実体の型は実体そのものに記録しており、型の付与は版管理の対象ではありません。
