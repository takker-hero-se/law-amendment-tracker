# 法改正トラッカー（Law Amendment Tracker）

日本の法令データの更新差分を自動検知し、LLM で「分野分類」「実務影響度」「3 行要旨」を付けた構造化 Markdown を生成して、
静的サイト（Astro + Tailwind CSS）として配信するオープンソースの法改正ウォッチである。

- 公開サイト: https://law.quietforensics.com/ （GitHub Pages、毎日 06:00 JST に更新）
- データ源: [e-Gov 法令検索 法令 API v2](https://laws.e-gov.go.jp/api/2/swagger-ui/)（デジタル庁、認証不要の公開 API）
- 参考 UI: 法令ごとの改正の系譜を年の列に並べる「法令変遷図」（`/map/`）

```
e-Gov API ──▶ fetcher ──▶ filter ──▶ summarizer ──▶ generator ──▶ frontend/src/content/amendments/*.md ──▶ Astro ──▶ Cloudflare Pages / GitHub Pages
 (法令履歴)   (差分検知)   (ノイズ除去)  (LLM/JSON)     (frontmatter)                                         (SSG)
```

## 何ができるか

| 画面 | 内容 |
|---|---|
| `/` | 新着・影響度順の一覧。分野・影響度・施行状態・キーワードで絞り込み |
| `/category/<slug>/` | 分野別一覧（`it-security` / `hr-labor` / `tax-accounting` / `legal-compliance` / `ip` / `other`） |
| `/amendments/<slug>/` | 個別改正。3 行要旨、公布・施行日、解説、条文差分（機械抽出）、同一法令の改正系譜 |
| `/timeline/` | 施行日タイムライン（これから施行される改正を先頭に） |
| `/map/` | 法令変遷図。公布年の列にノードを置き、同一法令の履歴を線でつなぐ。検索・分野フィルタ・ドラッグ配置 |
| `/rss.xml` `/feed.json` `/category/<slug>/rss.xml` | RSS 2.0 / JSON Feed 1.1（全体・分野別） |

## 仕組み

1. **差分検知（`pipeline/fetcher.py`）** `data/watchlist.json` の監視対象法令（既定 66 本: IT・労務・税務・法務・知財）について
   `GET /law_revisions/{law_id}` を取り、`data/state.json` のスナップショット（law_id → 最新履歴 ID）より新しい履歴を改正イベントにする。
   各履歴の本文を `GET /law_data/{revision_id}` で取り、条・項・号ごとに平文化（`textify.py`）して直前の履歴との unified diff を取る（`differ.py`）。
   `LAWTRACK_CATEGORY_CODES` を与えると `GET /laws?asof=2099-12-31&category_cd=…` の全件走査でも新履歴を拾う。
   houritsu 形式（1 法令 = 1 Markdown + YAML frontmatter）のローカル Git ミラーも `LAWTRACK_SOURCE=git` で扱える。
2. **ノイズ除去（`pipeline/filter.py`）** 本文差分なし、変更行数・文字数が閾値未満、条番号の繰り下げや句読点だけの変更を落とす。新設・廃止は常に残す。
3. **LLM 要約（`pipeline/summarizer.py`）** 新旧対照（`differ.format_for_llm`）を LLM に渡し、`Summary` スキーマ（pydantic）で検証する。
   検証に失敗したら指摘を添えて 1 回だけ再試行する。プロバイダは Anthropic（`messages.parse` の構造化出力）、Gemini（`response_json_schema`）、
   そして LLM を使わない `template`（差分の計測値だけから暫定値を付ける。資格情報がなくても完全に動く既定値）。
4. **チケット（`pipeline/tickets.py`）** `LAWTRACK_ISSUES_REPO` を設定すると、改正 1 件につき GitHub Issue を 1 件起票する。
   題名は `[影響度] 法令名：改正法名（施行 日付）`、ラベルは `分野:`・`影響度:`・`施行:`・`種別:`、本文は 3 行要旨・対象・公布/施行日・記事と e-Gov へのリンク・条文差分（折りたたみ）。
   `data/state.json` の `tickets`（履歴 ID → Issue 番号）で冪等にし、本文冒頭のマーカー `<!-- lawtrack:revision_id=… -->` で逆引きできる。
   既存の記事は `python -m pipeline.tickets_backfill` で一括起票する（`--update` で起票済みも更新、`--limit N`）。Issue の Close は人が「対応済み」の印として使う想定で、bot は閉じない。
5. **生成（`pipeline/generator.py`）** frontmatter 付き Markdown を `frontend/src/content/amendments/<slug>.md` に書く。Astro の Content Collections
   （`frontend/src/content.config.ts`）が同じスキーマで型検証する。

### 注意（e-Gov API の性質）

- 一部改正法（「○○の一部を改正する法律」）は `/laws` に単独の法令として現れない。被改正法の `law_revisions` の `amendment_law_id` 経由でしか辿れないため、
  検知は「監視対象法令の履歴」または「全件走査のスナップショット差分」で行う。
- `/laws` の `order` は `revision_info` 配下の項目では効かない（2026-11 時点の実測）。
- `asof=2099-12-31` を付けると未施行の最新履歴が `revision_info` に入る。
- 施行期日には e-Gov の「擬似的な施行期日」が含まれる。政令委任の場合は `enforcement_note` を見る。
- 一部の履歴は本文ファイルが提供されず `law_data` が 404 を返す。その履歴は `text_available: false` としてメタデータだけ公開する。

## セットアップ

```powershell
# venv は OneDrive 同期の外に作る（リポジトリ内 .venv は WinError 32 で壊れる）
python -m venv C:\Users\$env:USERNAME\venvs\leagal-biz
$VP = "C:\Users\$env:USERNAME\venvs\leagal-biz\Scripts\python.exe"
& $VP -m pip install -r requirements-dev.txt
cd frontend; npm install; cd ..
```

```powershell
# 1 法令だけ、2026 年以降の改正を検知して除外理由まで表示（書き込みなし）
& $VP -m pipeline.run --law-id 415AC0000000057 --since 2026-01-01 --dry-run --no-state
# 監視リスト全体を処理し、Markdown と状態ファイルを更新
& $VP -m pipeline.run --since 2025-04-01 --limit 60
# サイトをビルド
cd frontend; npm run build; npm run preview
```

主な設定は `.env.example` を参照。`LAWTRACK_LLM_PROVIDER=anthropic` と `ANTHROPIC_API_KEY`（または `gemini` と `GEMINI_API_KEY`）で LLM 要約が有効になる。
既定モデルは `claude-haiku-5-5` / `gemini-2.5-flash`。
リポジトリ直下の `.env`（git 管理外）は `pipeline.run` が起動時に読む。既に設定済みの環境変数は上書きしない。別のファイルを使うときは `--env-file` を指定する。

## 品質ゲート

```powershell
& $VP -m pytest            # 単体 + 結合（httpx.MockTransport で API を模擬、ネットワークに出ない）。行 100% / 分岐カバレッジをゲート
& $VP -m ruff check .
& $VP -m mypy pipeline
cd frontend; npx astro check; npm run build
```

## 自動運用（GitHub Actions）

- `.github/workflows/daily-law-tracker.yml` 毎日 06:00 JST に差分取得 → 要約 → `frontend/src/content` と `data/state.json` をコミット → ビルド → デプロイ。
  `workflow_dispatch` で `since` と `limit` を手動指定できる。
- デプロイ先: Variables に `CF_PAGES_PROJECT` と Secrets に `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` を置くと Cloudflare Pages。
  置かなければ GitHub Pages（リポジトリ設定で Pages の Source を "GitHub Actions" にする）。`SITE_URL` を Variables に置くと canonical / Feed の URL になる。
- LLM: Secrets に `ANTHROPIC_API_KEY` か `GEMINI_API_KEY`、Variables に `LAWTRACK_LLM_PROVIDER` を置く。
- チケット: Variables に `LAWTRACK_ISSUES_REPO`（`owner/name`）。同じリポジトリなら `GITHUB_TOKEN` で起票できる（ワークフローに `issues: write` 付与済み）。
  別リポジトリに起票するなら Secrets に `LAWTRACK_ISSUES_TOKEN`（issues:write の fine-grained PAT）を置く。
- `.github/workflows/ci.yml` push / PR で ruff・mypy・pytest・astro check・build を走らせる。

## ディレクトリ

```
pipeline/        差分検知・ノイズ除去・LLM 要約・Markdown 生成（Python 3.11）
  egov.py        e-Gov API v2 クライアント（リトライ・スロットル）
  textify.py     法令 JSON 木 / XML → 条・項・号ごとの平文
  fetcher.py     EgovSource（監視リスト + 全件走査）/ GitSource（ローカルミラー）
  filter.py      ノイズ除去
  differ.py      unified diff・新旧対照の整形
  summarizer.py  Anthropic / Gemini / template プロバイダ
  generator.py   frontmatter 付き Markdown
  tickets.py     GitHub Issue（チケット）の起票・更新
  tickets_backfill.py  既存記事の一括起票 CLI
  run.py         CLI
tests/           pytest（FakeEgov で API を模擬）
data/            watchlist.json（監視対象）、state.json（スナップショット・処理済み履歴）
frontend/        Astro 5 + Tailwind CSS 4 + lucide-astro + d3（変遷図）
.github/         日次ワークフロー、CI
```

## ライセンスと出典

MIT License。法令データは e-Gov 法令検索（デジタル庁）が提供するものであり、官報と異なる場合は官報が優先する。
要旨・影響度・分野は機械生成であり、法的助言ではない。
