import { getCollection } from "astro:content";
import { buildGraph } from "../../lib/amendments";

/** 変遷図のグラフデータ。HTML に埋め込むと 1 改正あたり約 1.4KB で膨らむので、別ファイルにして fetch する。 */
export async function GET() {
  const graph = buildGraph(await getCollection("amendments"));
  return new Response(JSON.stringify(graph), { headers: { "Content-Type": "application/json; charset=utf-8" } });
}
