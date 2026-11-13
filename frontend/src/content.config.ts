import { defineCollection, z } from "astro:content";
import { glob } from "astro/loaders";

export const CATEGORIES = [
  "情報セキュリティ・IT",
  "人事・労務",
  "税務・会計",
  "企業法務・コンプライアンス",
  "知財",
  "その他",
] as const;

export const CATEGORY_SLUGS = ["it-security", "hr-labor", "tax-accounting", "legal-compliance", "ip", "other"] as const;

const isoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, "YYYY-MM-DD");

const amendments = defineCollection({
  loader: glob({ pattern: "**/*.md", base: "./src/content/amendments" }),
  schema: z.object({
    title: z.string().min(1),
    law_id: z.string().min(1),
    law_num: z.string(),
    law_type: z.enum(["Constitution", "Act", "CabinetOrder", "ImperialOrder", "MinisterialOrdinance", "Rule", "Misc"]),
    egov_category: z.string().nullable().optional(),
    revision_id: z.string().min(1),
    prev_revision_id: z.string().nullable().optional(),
    amendment_type: z.enum(["新設", "一部改正", "廃止"]),
    amending_law_id: z.string().nullable().optional(),
    amending_law_title: z.string().nullable().optional(),
    amending_law_num: z.string().nullable().optional(),
    promulgation_date: isoDate,
    enforcement_date: isoDate.nullable().optional(),
    enforcement_note: z.string(),
    enforcement_status: z.enum(["CurrentEnforced", "UnEnforced", "PreviousEnforced", "Repeal"]),
    categories: z.array(z.enum(CATEGORIES)).min(1).max(3),
    category_slugs: z.array(z.enum(CATEGORY_SLUGS)).min(1).max(3),
    impact_level: z.enum(["High", "Medium", "Low"]),
    target_audience: z.array(z.string()).min(1).max(5),
    summary_points: z.array(z.string()).length(3),
    stats: z.object({ added_lines: z.number().int(), removed_lines: z.number().int(), changed_chars: z.number().int() }),
    source: z.enum(["egov", "git"]),
    text_available: z.boolean().default(true),
    issue_url: z.string().url().nullable().optional(),
    provider: z.string(),
    generated_at: z.string(),
    egov_url: z.string().url(),
  }),
});

export const collections = { amendments };
