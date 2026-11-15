import type { APIContext } from "astro";
import { getCollection } from "astro:content";
import { amendmentPath, byPromulgationDesc } from "../../lib/amendments";

/**
 * 全記事の索引（本文なし）。MCP サーバ（pipeline/mcp_server.py, LAWTRACK_MCP_SOURCE=サイト URL）が読む。
 * 項目は frontmatter と同じで、`slug` と `url` を足す。Python 側の `AmendmentRecord` と同じ形に保つ。
 */
export async function GET(context: APIContext) {
  const site = context.site!.toString().replace(/\/$/, "");
  const entries = (await getCollection("amendments")).sort(byPromulgationDesc);
  const items = entries.map((e) => ({
    slug: e.id,
    url: site + amendmentPath(e),
    title: e.data.title,
    law_id: e.data.law_id,
    law_num: e.data.law_num,
    law_type: e.data.law_type,
    revision_id: e.data.revision_id,
    prev_revision_id: e.data.prev_revision_id ?? null,
    amendment_type: e.data.amendment_type,
    amending_law_id: e.data.amending_law_id ?? null,
    amending_law_title: e.data.amending_law_title ?? null,
    amending_law_num: e.data.amending_law_num ?? null,
    promulgation_date: e.data.promulgation_date,
    enforcement_date: e.data.enforcement_date ?? null,
    enforcement_note: e.data.enforcement_note,
    enforcement_status: e.data.enforcement_status,
    categories: e.data.categories,
    category_slugs: e.data.category_slugs,
    impact_level: e.data.impact_level,
    target_audience: e.data.target_audience,
    summary_points: e.data.summary_points,
    egov_url: e.data.egov_url,
    issue_url: e.data.issue_url ?? null,
    provider: e.data.provider,
    generated_at: e.data.generated_at,
  }));
  return new Response(JSON.stringify(items), { headers: { "Content-Type": "application/json; charset=utf-8" } });
}
