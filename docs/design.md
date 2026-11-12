# 設計メモ

## 目的

日本の法令の改正を、実務者（情シス・CISO、人事総務、経理、法務、知財）が自分の分野だけ追えるようにする。
正本は e-Gov 法令検索と官報であり、本サイトは「何がいつ変わるか」を差分と要旨で先に知らせる入口に徹する。

## 差分検知の方式（e-Gov API v2 の実測に基づく）

| 事実（2026-11-12 実測） | 設計上の帰結 |
|---|---|
| 一部改正法は `/laws` に単独の法令として現れない（`law_id=508AC0000000056` → 0 件、`mission=Partial` → 0 件） | 改正は被改正法の `law_revisions` からしか辿れない |
| `/laws?amendment_law_id=X&asof=2099-12-31` は X で改正された法令と、その履歴を返す | 改正法 ID から被改正法へ逆引きできる（将来の拡張点） |
| `/laws` の `order` は `revision_info` 配下の項目では効かない | 「最近更新された履歴」をサーバ側で並べ替えて取る方法はない |
| `law_revisions/{law_id}` は施行日の降順で返る | 前回の最新履歴 ID より前にある要素が新しい履歴。直後の要素が「直前の版」 |
| `law_data/{revision_id}?law_full_text_format=json` は `{tag, attr, children}` の木 | 条・項・号ごとに平文化して行単位の diff を取る |
| 一部の履歴は `law_data` が 404（`404004` 本文ファイルが存在しない。例: 健康保険法 `211AC0000000070_20260525_506AC0000000052`） | 404 はメタデータのみのイベント（`text_available=False`）として残す。他のエラーは実行を止める |

スナップショット（`state.snapshot[law_id]`）は、その法令の候補をすべて今回処理できたときだけ進める。`limit` で切った残りや途中で失敗した実行では進めないので、
取りこぼしは次回また検知される（処理済み・除外済みの履歴は `seen_revision_ids` で除く）。

採用した方式は 2 つ。

1. **監視リスト（既定）** `data/watchlist.json` の法令ごとに `law_revisions` を取り、スナップショット（`state.snapshot[law_id]`）と比べる。
   66 法令で 66 リクエスト + 新履歴ごとに本文 2 件。日次で十分軽い。
2. **全件走査（任意）** `LAWTRACK_CATEGORY_CODES` を与えると `/laws?asof=2099-12-31&law_type=…&category_cd=…` を 100 件ずつ走査し、
   `revision_info.law_revision_id` がスナップショットと違う法令だけ `law_revisions` を取る。法律全件（約 2,200 本）で約 22 リクエスト。

Git ミラー（houritsu 形式）は `GitSource` で前回コミットと HEAD の差分を取る。公開ミラーの更新頻度と改正の対応付けはミラー側の設計に依存するため、既定にはしない。

## 平文化の規則（`textify.py`）

- 行を切る要素: 法令名、編・章・節・款・目の見出し、条（Article）、第 2 項以降の項番号（ParagraphNum に文字がある場合）、号（Item）、目（Subitem1〜4）、附則ラベル、別表の見出し、表の行。
- 第 1 項は条見出しと同じ行に置く（「第二条 事業者は、…」）。
- 落とす要素: 目次（TOC）、ルビ（Ruby/Rt）、法令番号（LawNum）。
- 空白は 1 個に畳む。

## ノイズ除去（`filter.py`）

| 規則 | 既定 |
|---|---|
| 本文差分なし | 落とす |
| 変更行数 < `LAWTRACK_NOISE_MIN_CHANGED_LINES` | 落とす（既定 2） |
| 変更文字数 < `LAWTRACK_NOISE_MIN_CHANGED_CHARS` | 落とす（既定 40） |
| 条番号・数字・空白・句読点を潰すと同一 | 落とす（繰り下げのみ） |
| 新設・廃止 | 常に残す |

2026-11-12 の実行（監視リスト 66 法令、2025-04-01 以降、上限なし）では 313 件検知 → 30 件除外 → 283 件生成（Anthropic `claude-haiku-5-5`、スキーマ再試行 0、失敗 0）。

## LLM 要約（`summarizer.py`）

- 入力は `differ.format_for_llm()` が作る新旧対照（変更ブロックごとに直前・直後 1 行の文脈、`LAWTRACK_LLM_INPUT_MAX_CHARS` で打ち切り）。
- システム文は固定（プロンプトキャッシュが効く）。ユーザー文にメタデータと新旧対照を入れる。
- Anthropic は `client.messages.parse(output_format=Summary)`、Gemini は `response_json_schema`。どちらも最終的に `Summary.model_validate_json` を通す。
- 構造化出力でも `max_length` 等の制約は API 側で強制されない。SDK の `parse` が投げる pydantic の例外は `SchemaMismatch` に変換して同じ再試行に乗せる（2026-11-12 に `target_audience` 6 件で実行が止まった不具合の対処）。
- `template` は LLM を使わず、e-Gov の分野分類 → 本サイトの分野のマッピングと差分行数だけで暫定値を付ける。本文に「LLM 要約を生成していない」と明記する。

## フロントエンド

- Astro 5（Content Collections, `glob` loader）+ Tailwind CSS 4（`@tailwindcss/vite`）+ lucide-astro + d3。
- 配色は navy + cyan、本文は BIZ UDPGothic。変遷図のページだけダーク背景。
- `/map/` は MBSD のランサムウェア変遷図を参考に、年の列・系譜の線・クリックで詳細パネル・検索・レイアウト切替（タイムライン / 分野 / カスタム）・ミニマップを持つ。
  ノード = 改正履歴、線 = 同一法令の連続する履歴。枠線の色が影響度、塗りが施行状態、右上の点が分野。

## 今後の拡張点

- 改正法 ID からの逆引き（`amendment_law_id`）で「この改正法が触った法令一覧」ページを作る。
- 政令・府省令（`CabinetOrder` / `MinisterialOrdinance`）を監視リストに加える。
- 要旨の人手レビュー（HITL）を挟む運用。GitHub Actions のコミットを PR にすればレビュー可能。
