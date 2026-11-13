# 開発・品質ガイドライン（leagal-biz / 法改正トラッカー）

本リポジトリで作業する自律エージェント（Claude Code など）と開発者は、以下の品質基準とテスト方針を厳格に遵守すること。
数値のゲートはすべて CI とローカルの同じコマンドで機械的に検証される。

## 0. このプロダクトの不変条件（テストより優先する設計ルール）

- **観測事実と推定を分ける。** 条文差分・日付・改正法令名は e-Gov の値をそのまま写す（`AmendmentEvent`）。分野・影響度・要旨は LLM の推定（`Summary`）であり、
  別の型・別の節に置く。Markdown でも「条文差分（機械抽出）」節と要旨を混ぜない。
- **LLM の出力はスキーマで検証する。** `Summary`（pydantic, `extra="forbid"`）を通らない出力は採用しない。再試行は 1 回まで。失敗は `SummarizerError` にして件数を報告する。
- **資格情報なしで完全に動く。** `LAWTRACK_LLM_PROVIDER=template` が既定で、LLM・ネットワーク（テスト時）なしでパイプラインが完走する。
- **外部 API には礼儀正しく。** e-Gov へのリクエストはスロットル（`LAWTRACK_REQUEST_INTERVAL_SEC`）とリトライ上限を設定に置く。1 回の処理件数は `LAWTRACK_MAX_EVENTS_PER_RUN` で縛る。
- **出典と免責を外さない。** 生成物には `egov_url`・`revision_id`・`provider`・`generated_at` を必ず入れ、サイトは「機械生成であり法的助言ではない」「官報が優先する」を表示する。
- **チケットは冪等に。** GitHub Issue は `State.tickets`（履歴 ID → 番号）を見て作成か更新かを決め、同じ履歴を二度起票しない。本文冒頭のマーカーを消さない。
  起票の失敗は `TicketError` にして件数を報告し、記事の生成は止めない。bot は Issue を閉じない（Close は人の「対応済み」）。
- **ノイズ除去の理由を残す。** 落としたイベントは理由文字列つきで報告する（`RunReport.dropped_reasons`）。黙って捨てない。

## 1. 開発フロー（TDD）

1. 機能実装の前に、要件を満たすテストを先に書く（`tests/test_*.py`）。正常・異常（4xx/5xx/タイムアウト/非 JSON）・境界（閾値の直前・同一・直後）を列挙してから書く。
2. テストが失敗することを確認してから最小限の実装を書く。
3. 外部 HTTP（e-Gov, Anthropic, Gemini）は `httpx.MockTransport`（`tests/conftest.py` の `FakeEgov`）や差し込み可能なクライアントで代替し、テストからネットワークに出ない。
4. 新しい設定値は `pipeline/config.py` に型付きで追加し、`.env.example`・README・ワークフローに反映する。
5. Astro 側のスキーマ（`frontend/src/content.config.ts`）と `generator.frontmatter()` は同じ項目を持つ。片方を変えたらもう片方も変え、`npm run build` で検証する。

## 2. カバレッジ・網羅性基準（ゲート）

| 指標 | 基準 | 計測 |
|---|---|---|
| 行カバレッジ（C0） | **100%** | `pytest`（`--cov-fail-under=100`） |
| 分岐カバレッジ（C1） | **100%**（現状） | 同上、`--cov-branch --cov-report=term-missing` |
| 境界値 | 閾値・件数上限は「直前・同一・直後」をテストする | `tests/test_filter.py`, `tests/test_config.py` |
| 異常系 | 4xx/5xx/接続不可/非 JSON/スキーマ不一致 を外部ソースごとにテストする | `tests/test_egov.py`, `tests/test_summarizer.py` |

到達不能な多層防御の分岐だけ `# pragma: no cover` を許可し、理由をコメントに書く。

## 3. 欠陥防止とコード品質（ゲート）

- `ruff check .` がエラーゼロ（`ruff.toml`）。
- `mypy pipeline` がエラーゼロ（`mypy.ini`、`disallow_untyped_defs`）。
- `npx astro check` と `npm run build` が通る。
- 例外の握りつぶし禁止。外部ソースの失敗は `SourceError` / `SummarizerError` に変換する。

## 4. 完了条件（Definition of Done）

- [ ] 新機能の単体テストが存在し、正常・異常・境界を含む
- [ ] `pytest` が全件パスし、行 100% / 分岐 100% を満たす
- [ ] `ruff check .` と `mypy pipeline` がエラーなし
- [ ] `npx astro check` と `npm run build` が通る
- [ ] `.env.example` / README / ワークフローの該当箇所を更新した
- [ ] 完了報告に「テスト件数、結果、カバレッジ（行・分岐）、静的解析の結果、ビルドしたページ数」を数値で明記した

## 5. コマンド

仮想環境は **`C:\Users\<you>\venvs\leagal-biz`**（リポジトリの外）。このワークスペースは OneDrive 同期下にあり、
リポジトリ内に `.venv` を置くと同期がファイルをロックして `pip install` が WinError 32 で失敗する。

```powershell
$VP = "C:\Users\$env:USERNAME\venvs\leagal-biz\Scripts\python.exe"
& $VP -m pytest                 # テスト + 行/分岐カバレッジ（ゲート込み）
& $VP -m ruff check .           # lint（--fix で機械的修正）
& $VP -m mypy pipeline          # 型チェック
& $VP -m pipeline.run --since 2026-01-01 --dry-run --no-state   # 実 API で検知だけ試す
cd frontend; npx astro check; npm run build; npm run preview
```

Windows で日本語を含む出力を扱うときは `PYTHONUTF8=1` を付ける。ファイルは LF で保存する（CRLF が混ざると Python の文字列リテラルが壊れた前例がある）。

## 6. 参照

- 設計メモ: `docs/design.md`
- e-Gov 法令 API v2 仕様: https://laws.e-gov.go.jp/api/2/swagger-ui/
