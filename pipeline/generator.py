"""`Amendment` を frontmatter 付き Markdown に書き出す（Astro Content Collections 用）。"""

from __future__ import annotations

from pathlib import Path

import yaml

from pipeline.models import CATEGORY_SLUGS, Amendment

MAX_DIFF_CHARS = 60000


def frontmatter(record: Amendment) -> dict[str, object]:
    ev, sm = record.event, record.summary
    return {
        "title": ev.law.law_title,  # 観測事実（e-Gov の法令名）。LLM の law_title は題名には使わない
        "law_id": ev.law.law_id,
        "law_num": sm.law_num,
        "law_type": ev.law.law_type,
        "egov_category": ev.law.egov_category,
        "revision_id": ev.revision_id,
        "prev_revision_id": ev.prev_revision_id,
        "amendment_type": sm.amendment_type,
        "amending_law_id": ev.amending_law_id,
        "amending_law_title": ev.amending_law_title,
        "amending_law_num": ev.amending_law_num,
        "promulgation_date": ev.promulgation_date.isoformat(),
        "enforcement_date": ev.enforcement_date.isoformat() if ev.enforcement_date else None,
        "enforcement_note": sm.enforcement_date,
        "enforcement_status": ev.enforcement_status.value,
        "categories": sm.categories,
        "category_slugs": [CATEGORY_SLUGS[c] for c in sm.categories],
        "impact_level": sm.impact_level,
        "target_audience": sm.target_audience,
        "summary_points": sm.summary_points,
        "stats": ev.stats.model_dump(),
        "source": ev.source,
        "text_available": ev.text_available,
        "provider": record.provider,
        "generated_at": record.generated_at,
        "egov_url": f"https://laws.e-gov.go.jp/law/{ev.law.law_id}",
        "issue_url": record.issue_url,
    }


def render_markdown(record: Amendment) -> str:
    fm = yaml.safe_dump(frontmatter(record), allow_unicode=True, sort_keys=False, default_flow_style=False)
    diff = record.event.diff
    truncated = ""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS]
        truncated = "\n（差分が長いため切り詰めた）\n"
    body = record.summary.body_markdown.rstrip() + "\n"
    diff_section = ""
    if diff.strip():
        diff_section = "\n## 条文差分（機械抽出）\n\n```diff\n" + diff.rstrip() + "\n```\n" + truncated
    return f"---\n{fm}---\n\n{body}{diff_section}"


def write(record: Amendment, content_dir: Path) -> Path:
    content_dir.mkdir(parents=True, exist_ok=True)
    path = content_dir / f"{record.slug}.md"
    path.write_text(render_markdown(record), encoding="utf-8", newline="\n")
    return path
