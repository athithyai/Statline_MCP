/**
 * End-to-end smoke test: spawns the built server over stdio and exercises
 * every tool against the live CBS API. Run with `node scripts/smoke.mjs`.
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const transport = new StdioClientTransport({
  command: process.execPath,
  args: ["dist/index.js"],
});
const client = new Client({ name: "smoke", version: "1.0.0" });
await client.connect(transport);

let failures = 0;
const check = (name, cond, detail = "") => {
  if (cond) console.log(`  PASS  ${name}`);
  else {
    failures++;
    console.log(`  FAIL  ${name} ${detail}`);
  }
};

const tools = await client.listTools();
console.log("tools:", tools.tools.map((t) => t.name).join(", "));
check("four tools registered", tools.tools.length === 4);

const call = async (name, args) => {
  const r = await client.callTool({ name, arguments: args });
  return { text: r.content.map((c) => c.text).join("\n"), structured: r.structuredContent, isError: r.isError };
};

console.log("\nsearch_tables");
const s = await call("search_tables", { query: "bevolking", limit: 3 });
check("returns tables", !s.isError && s.structured?.count > 0, s.text.slice(0, 200));

console.log("\nget_table_info");
const i = await call("get_table_info", { table_id: "83583NED" });
check("has dimensions", i.structured?.dimensions?.length >= 3, i.text.slice(0, 200));
check("has measures", i.structured?.measures?.length >= 1);
check("lists Perioden", i.text.includes("Perioden"));

console.log("\nget_dimension_codes");
const c = await call("get_dimension_codes", { table_id: "83583NED", dimension: "Perioden", limit: 5 });
check("returns codes", c.structured?.count === 5, c.text.slice(0, 200));

const cs = await call("get_dimension_codes", { table_id: "83583NED", dimension: "Bedrijfsgrootte", search: "totaal" });
check("search narrows", cs.structured?.count > 0 && cs.structured.count < 20, cs.text.slice(0, 200));

console.log("\nget_data");
const d = await call("get_data", {
  table_id: "83583NED",
  filters: { Perioden: ["2023*"], Bedrijfsgrootte: ["T001098"] },
  limit: 5,
});
check("returns exactly `limit` rows", d.structured?.returned === 5, d.text.slice(0, 300));
check("resolves labels", d.structured?.rows?.[0]?.Bedrijfsgrootte_label === "Totaal", JSON.stringify(d.structured?.rows?.[0]));
check("label column sits next to its code", /\| Perioden \| Perioden_label \|/.test(d.text));
check("codes are trimmed", d.structured.rows.every((r) => !/\s$/.test(r.BedrijfstakkenBranchesSBI2008)));
check("signals more pages", d.structured?.has_more === true);
console.log(d.text.split("\n").slice(0, 6).join("\n"));

// The filter matches 124 rows; a page past the end must terminate, not loop.
const dEnd = await call("get_data", {
  table_id: "83583NED",
  filters: { Perioden: ["2023*"], Bedrijfsgrootte: ["T001098"] },
  limit: 50,
  offset: 100,
});
check("last page returns the remainder", dEnd.structured?.returned === 24, `got ${dEnd.structured?.returned}`);
check("last page has_more is false", dEnd.structured?.has_more === false);

const dSel = await call("get_data", {
  table_id: "83583NED",
  filters: { Perioden: ["2023JJ00"] },
  measures: ["BanenVanWerknemersInDecember_1"],
  limit: 2,
  labels: false,
});
check("measure select keeps dimensions", dSel.structured?.rows?.[0]?.Perioden === "2023JJ00", dSel.text.slice(0, 200));
check("labels:false omits label columns", !dSel.text.includes("_label"));

console.log("\nerror handling");
const e1 = await call("get_data", { table_id: "nope", filters: {} });
check("rejects bad table id", e1.isError === true, e1.text.slice(0, 120));
const e2 = await call("get_data", { table_id: "83583NED", filters: { NotADim: ["x"] } });
check("rejects unknown dimension", e2.isError === true && e2.text.includes("dimension"), e2.text.slice(0, 160));
const e3 = await call("get_data", { table_id: "83583NED", measures: ["Bogus"], limit: 1 });
check("rejects unknown measure", e3.isError === true, e3.text.slice(0, 160));
const e4 = await call("get_data", { table_id: "83583NED", filters: { Perioden: ["1800JJ00"] } });
check("empty result is not an error", !e4.isError && e4.text.includes("No observations"), e4.text.slice(0, 160));

await client.close();
console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
