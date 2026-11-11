import { defineConfig } from "astro/config";
import tailwindcss from "@tailwindcss/vite";

// SITE_URL は Cloudflare Pages / GitHub Pages のデプロイ先に合わせて環境変数で上書きする。
const site = process.env.SITE_URL || "https://law-tracker.example.com";

export default defineConfig({
  site,
  output: "static",
  trailingSlash: "ignore",
  build: { format: "directory" },
  vite: { plugins: [tailwindcss()] },
});
