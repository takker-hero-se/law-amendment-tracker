import rss from "@astrojs/rss";
import type { APIContext } from "astro";
import { getCollection } from "astro:content";
import { amendmentPath, byPromulgationDesc, SITE_NAME, SITE_TAGLINE } from "../lib/amendments";

export async function GET(context: APIContext) {
  const entries = (await getCollection("amendments")).sort(byPromulgationDesc).slice(0, 100);
  return rss({
    title: SITE_NAME,
    description: SITE_TAGLINE,
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
