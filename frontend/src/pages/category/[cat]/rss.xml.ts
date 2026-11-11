import rss from "@astrojs/rss";
import type { APIContext } from "astro";
import { getCollection } from "astro:content";
import { CATEGORY_SLUGS } from "../../../content.config";
import { amendmentPath, byPromulgationDesc, categoryBySlug, SITE_NAME, type CategorySlug } from "../../../lib/amendments";

export function getStaticPaths() {
  return CATEGORY_SLUGS.map((cat) => ({ params: { cat } }));
}

export async function GET(context: APIContext) {
  const cat = context.params.cat as CategorySlug;
  const category = categoryBySlug[cat];
  const entries = (await getCollection("amendments", (e) => e.data.category_slugs.includes(cat))).sort(byPromulgationDesc).slice(0, 50);
  return rss({
    title: `${SITE_NAME} — ${category}`,
    description: `${category} 分野の法改正（e-Gov 法令 API の差分から自動生成）`,
    site: context.site!,
    items: entries.map((e) => ({
      title: `[${e.data.impact_level}] ${e.data.title}（${e.data.amendment_type}）`,
      link: amendmentPath(e),
      pubDate: new Date(e.data.promulgation_date),
      description: e.data.summary_points.join(" / "),
      categories: e.data.categories,
    })),
    customData: "<language>ja</language>",
  });
}
