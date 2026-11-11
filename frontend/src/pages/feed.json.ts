import type { APIContext } from "astro";
import { getCollection } from "astro:content";
import { amendmentPath, byPromulgationDesc, SITE_NAME, SITE_TAGLINE } from "../lib/amendments";

/** JSON Feed 1.1。分野別は `?category=` ではなく各記事の tags で絞る。 */
export async function GET(context: APIContext) {
  const site = context.site!.toString().replace(/\/$/, "");
  const entries = (await getCollection("amendments")).sort(byPromulgationDesc).slice(0, 100);
  const feed = {
    version: "https://jsonfeed.org/version/1.1",
    title: SITE_NAME,
    description: SITE_TAGLINE,
    home_page_url: site + "/",
    feed_url: site + "/feed.json",
    language: "ja",
    items: entries.map((e) => ({
      id: e.data.revision_id,
      url: site + amendmentPath(e),
      title: `[${e.data.impact_level}] ${e.data.title}（${e.data.amendment_type}）`,
      summary: e.data.summary_points.join(" / "),
      content_text: e.data.summary_points.join("\n"),
      date_published: `${e.data.promulgation_date}T00:00:00+09:00`,
      tags: [...e.data.categories, e.data.impact_level, e.data.enforcement_status],
      _law: {
        law_id: e.data.law_id,
        law_num: e.data.law_num,
        enforcement_date: e.data.enforcement_date ?? null,
        enforcement_note: e.data.enforcement_note,
        amending_law_title: e.data.amending_law_title ?? null,
        egov_url: e.data.egov_url,
      },
    })),
  };
  return new Response(JSON.stringify(feed, null, 2), { headers: { "Content-Type": "application/feed+json; charset=utf-8" } });
}
