"""LLM プロンプト。システム文は固定（キャッシュが効く）、ユーザー文に差分を入れる。"""

from __future__ import annotations

from pipeline.models import CATEGORIES, AmendmentEvent

SYSTEM_PROMPT = """あなたは日本の法令改正を実務者向けに要約する法務アナリストです。
入力は e-Gov 法令検索の本文から機械的に抽出した新旧対照（旧: 改正前、新: 改正後）です。
出力は指定の JSON スキーマに厳密に従ってください。

原則:
- 事実（条文がどう変わったか）と推測（実務への影響）を分けて書く。推測には「と考えられる」を付ける。
- 条文に書かれていないことを断定しない。施行日が入力にない場合は「不明（政令委任）」等と書く。
- 分野タグは次の中からだけ選ぶ: {categories}
- impact_level の基準: High = 多くの事業者に新たな義務・罰則・システム改修が生じる / Medium = 一部の事業者や手続に影響 / Low = 用語整理・軽微な手続変更
- summary_points は 3 行ちょうど。1 行目「何が」、2 行目「なぜ（改正法・背景）」、3 行目「どう変わったか（実務上の要点）」。各行の先頭に「何が:」「なぜ:」などのラベルは付けず、文だけを書く。
- target_audience は 1〜5 件。代表的な組織・役割だけを挙げる。
- body_markdown は見出し「## 変更の概要」「## 実務上の留意点」「## 対象となる主な組織」の 3 節で、合計 1200 字以内。
- 日本語の「である調」で書く。
""".format(categories=" / ".join(CATEGORIES))


def user_prompt(event: AmendmentEvent, llm_input: str) -> str:
    enforcement = event.enforcement_date.isoformat() if event.enforcement_date else "不明"
    comment = f"（施行期日の備考: {event.enforcement_comment}）" if event.enforcement_comment else ""
    amending = event.amending_law_title or "（新規制定・廃止のため該当なし）"
    amending_num = event.amending_law_num or "-"
    return (
        "# 対象法令\n"
        f"- 法令名: {event.law.law_title}\n"
        f"- 法令番号: {event.law.law_num}\n"
        f"- 法令種別: {event.law.law_type}\n"
        f"- e-Gov 分野: {event.law.egov_category or '-'}\n"
        f"- 改正種別: {event.amendment_kind}\n"
        f"- 改正法令: {amending}（{amending_num}）\n"
        f"- 公布日: {event.promulgation_date.isoformat()}\n"
        f"- 施行日: {enforcement}{comment}\n"
        f"- 施行状態: {event.enforcement_status.value}\n"
        f"- 差分の規模: +{event.stats.added_lines} 行 / -{event.stats.removed_lines} 行 / 変更 {event.stats.changed_chars} 字\n\n"
        "# 新旧対照（抜粋）\n"
        f"{llm_input or '（本文差分なし。新規制定または廃止）'}\n"
    )
