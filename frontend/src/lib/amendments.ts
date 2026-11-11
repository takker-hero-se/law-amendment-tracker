import type { CollectionEntry } from "astro:content";
import { CATEGORIES, CATEGORY_SLUGS } from "../content.config";

export type Amendment = CollectionEntry<"amendments">;
export type Category = (typeof CATEGORIES)[number];
export type CategorySlug = (typeof CATEGORY_SLUGS)[number];

export const SITE_NAME = "法改正トラッカー";
export const SITE_TAGLINE = "e-Gov 法令データの更新差分を自動検知し、分野・実務影響度・3 行要旨を付けて配信するオープンソースの法改正ウォッチ";

export const categoryBySlug: Record<CategorySlug, Category> = Object.fromEntries(CATEGORY_SLUGS.map((s, i) => [s, CATEGORIES[i]])) as Record<CategorySlug, Category>;
export const slugByCategory: Record<Category, CategorySlug> = Object.fromEntries(CATEGORIES.map((c, i) => [c, CATEGORY_SLUGS[i]])) as Record<Category, CategorySlug>;

export const IMPACT_LABEL: Record<Amendment["data"]["impact_level"], string> = { High: "影響 大", Medium: "影響 中", Low: "影響 小" };
export const IMPACT_ORDER: Record<Amendment["data"]["impact_level"], number> = { High: 0, Medium: 1, Low: 2 };
export const STATUS_LABEL: Record<Amendment["data"]["enforcement_status"], string> = {
  CurrentEnforced: "施行済",
  UnEnforced: "未施行",
  PreviousEnforced: "過去の版",
  Repeal: "廃止",
};
export const LAW_TYPE_LABEL: Record<Amendment["data"]["law_type"], string> = {
  Constitution: "憲法",
  Act: "法律",
  CabinetOrder: "政令",
  ImperialOrder: "勅令",
  MinisterialOrdinance: "府省令",
  Rule: "規則",
  Misc: "その他",
};

export function byPromulgationDesc(a: Amendment, b: Amendment): number {
  return b.data.promulgation_date.localeCompare(a.data.promulgation_date) || a.data.title.localeCompare(b.data.title, "ja");
}

export function byEnforcementAsc(a: Amendment, b: Amendment): number {
  const ea = a.data.enforcement_date ?? "9999-12-31";
  const eb = b.data.enforcement_date ?? "9999-12-31";
  return ea.localeCompare(eb) || byPromulgationDesc(a, b);
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "未定";
  const [y, m, d] = iso.split("-");
  return `${y}年${Number(m)}月${Number(d)}日`;
}

export function yearOf(iso: string): number {
  return Number(iso.slice(0, 4));
}

export function amendmentPath(entry: Amendment): string {
  return `/amendments/${entry.id}/`;
}

export function categoryPath(slug: CategorySlug): string {
  return `/category/${slug}/`;
}

/** 変遷図用のグラフ。ノード = 改正、エッジ = 同一法令の連続する履歴。 */
export interface MapNode {
  id: string;
  slug: string;
  lawId: string;
  title: string;
  short: string;
  year: number;
  promulgation: string;
  enforcement: string | null;
  status: Amendment["data"]["enforcement_status"];
  impact: Amendment["data"]["impact_level"];
  type: Amendment["data"]["amendment_type"];
  categories: string[];
  categorySlugs: string[];
  points: string[];
  amendingLaw: string | null;
  isLatest: boolean;
}
export interface MapEdge {
  source: string;
  target: string;
}

export function shortTitle(title: string, max = 14): string {
  const t = title.replace(/に関する法律$/, "法").replace(/の一部を改正する法律$/, "改正法");
  return t.length > max ? t.slice(0, max - 1) + "…" : t;
}

export function buildGraph(entries: Amendment[]): { nodes: MapNode[]; edges: MapEdge[]; years: number[] } {
  const byLaw = new Map<string, Amendment[]>();
  for (const e of entries) {
    const list = byLaw.get(e.data.law_id) ?? [];
    list.push(e);
    byLaw.set(e.data.law_id, list);
  }
  const nodes: MapNode[] = [];
  const edges: MapEdge[] = [];
  for (const [, list] of byLaw) {
    list.sort((a, b) => a.data.promulgation_date.localeCompare(b.data.promulgation_date) || (a.data.enforcement_date ?? "").localeCompare(b.data.enforcement_date ?? ""));
    list.forEach((e, i) => {
      nodes.push({
        id: e.data.revision_id,
        slug: e.id,
        lawId: e.data.law_id,
        title: e.data.title,
        short: shortTitle(e.data.title),
        year: yearOf(e.data.promulgation_date),
        promulgation: e.data.promulgation_date,
        enforcement: e.data.enforcement_date ?? null,
        status: e.data.enforcement_status,
        impact: e.data.impact_level,
        type: e.data.amendment_type,
        categories: e.data.categories,
        categorySlugs: e.data.category_slugs,
        points: e.data.summary_points,
        amendingLaw: e.data.amending_law_title ?? null,
        isLatest: i === list.length - 1,
      });
      if (i > 0) edges.push({ source: list[i - 1].data.revision_id, target: e.data.revision_id });
    });
  }
  const yearSet = new Set(nodes.map((n) => n.year));
  const years = [...yearSet].sort((a, b) => a - b);
  return { nodes, edges, years };
}
