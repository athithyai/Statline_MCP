#!/usr/bin/env node
/**
 * mcp-statline - an MCP server over CBS StatLine (Statistics Netherlands).
 *
 * Exposes four tools:
 *   search_tables        find a StatLine table by keyword
 *   get_table_info       metadata, dimensions and measures of one table
 *   get_dimension_codes  the code list of one dimension
 *   get_data             filtered observations, with code labels resolved
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

import {
  CbsError,
  assertTableId,
  getCodeLabels,
  getCodes,
  getData,
  getDataProperties,
  getTableInfo,
  isDimension,
  isMeasure,
  searchTables,
  statlineUrl,
  type DataProperty,
} from "./cbs.js";

const server = new McpServer({ name: "mcp-statline", version: "0.1.0" });

/* --------------------------------------------------------------- helpers -- */

type ToolResult = {
  content: { type: "text"; text: string }[];
  structuredContent?: Record<string, unknown>;
  isError?: boolean;
};

function ok(text: string, structured?: Record<string, unknown>): ToolResult {
  const result: ToolResult = { content: [{ type: "text", text }] };
  if (structured) result.structuredContent = structured;
  return result;
}

function fail(err: unknown): ToolResult {
  const message = err instanceof CbsError ? err.message : `Unexpected error: ${String(err)}`;
  return { content: [{ type: "text", text: message }], isError: true };
}

function truncate(s: string | undefined, n: number): string {
  if (!s) return "";
  const flat = s.replace(/\s+/g, " ").trim();
  return flat.length > n ? flat.slice(0, n - 1) + "…" : flat;
}

/** Render rows as a pipe table so the model can read them without parsing JSON. */
function asTable(rows: Record<string, unknown>[], columns: string[]): string {
  if (rows.length === 0) return "(no rows)";
  const header = `| ${columns.join(" | ")} |`;
  const rule = `| ${columns.map(() => "---").join(" | ")} |`;
  const body = rows.map(
    (r) =>
      `| ${columns
        .map((c) => {
          const v = r[c];
          return v === null || v === undefined ? "" : String(v).replace(/\|/g, "\\|").trim();
        })
        .join(" | ")} |`,
  );
  return [header, rule, ...body].join("\n");
}

/* --------------------------------------------------------- search_tables -- */

server.registerTool(
  "search_tables",
  {
    title: "Search StatLine tables",
    description:
      "Search the CBS StatLine catalog for tables by keyword and return their identifiers. " +
      "StatLine titles and descriptions are in Dutch, so Dutch keywords match best " +
      "(bevolking, werkloosheid, inkomen, bedrijven, criminaliteit, energie). " +
      "All words must match. Start here when you do not already know the table id.",
    inputSchema: {
      query: z
        .string()
        .min(1)
        .describe("Keywords to match against table title and description, e.g. 'bevolking regio'."),
      limit: z.number().int().min(1).max(50).default(10).describe("Maximum tables to return."),
    },
  },
  async ({ query, limit }) => {
    try {
      const rows = await searchTables(query, limit);
      if (rows.length === 0) {
        return ok(
          `No StatLine tables matched "${query}". Titles are in Dutch - try a Dutch keyword, ` +
            `or fewer words (every word must match).`,
          { query, count: 0, tables: [] },
        );
      }
      const lines = rows.map(
        (r) =>
          `**${r.Identifier}** - ${r.Title.trim()}\n` +
          `  period: ${r.Period ?? "?"} | frequency: ${r.Frequency ?? "?"} | rows: ${
            r.RecordCount ?? "?"
          } | updated: ${(r.Updated ?? "").slice(0, 10)}\n` +
          `  ${truncate(r.ShortDescription, 220)}`,
      );
      return ok(
        `${rows.length} table(s) matching "${query}":\n\n${lines.join("\n\n")}\n\n` +
          `Next: get_table_info with one of these identifiers.`,
        { query, count: rows.length, tables: rows },
      );
    } catch (err) {
      return fail(err);
    }
  },
);

/* -------------------------------------------------------- get_table_info -- */

server.registerTool(
  "get_table_info",
  {
    title: "Describe a StatLine table",
    description:
      "Return the metadata of one StatLine table: title, period covered, update status, plus " +
      "its dimensions (the columns you filter on) and measures (the columns that hold numbers). " +
      "Call this before get_data so you know which dimension and measure keys exist.",
    inputSchema: {
      table_id: z.string().describe("StatLine table identifier, e.g. '83583NED'."),
    },
  },
  async ({ table_id }) => {
    try {
      const id = assertTableId(table_id);
      const [info, props] = await Promise.all([getTableInfo(id), getDataProperties(id)]);

      const dims = props.filter(isDimension);
      const measures = props.filter(isMeasure);

      const dimLines = dims.map(
        (d: DataProperty) =>
          `- \`${d.Key}\` (${d.Type}) - ${d.Title.trim()}${
            d.Description ? `\n    ${truncate(d.Description, 160)}` : ""
          }`,
      );
      const measureLines = measures.map(
        (m: DataProperty) =>
          `- \`${m.Key}\` - ${m.Title.trim()}${m.Unit ? ` [${m.Unit.trim()}]` : ""}${
            m.Description ? `\n    ${truncate(m.Description, 160)}` : ""
          }`,
      );

      const text =
        `# ${info.Title.trim()} (${id})\n\n` +
        `period: ${info.Period ?? "?"} | frequency: ${info.Frequency ?? "?"} | ` +
        `status: ${info.OutputStatus ?? "?"} | modified: ${(info.Modified ?? "").slice(0, 10)}\n` +
        `source: ${info.Source ?? "CBS"}\n` +
        `statline: ${statlineUrl(id)}\n\n` +
        `${truncate(info.ShortDescription ?? info.Summary, 900)}\n\n` +
        `## Dimensions (${dims.length}) - filter on these\n${
          dimLines.join("\n") || "(none)"
        }\n\n` +
        `## Measures (${measures.length}) - select these\n${measureLines.join("\n") || "(none)"}\n\n` +
        `Next: get_dimension_codes to see the allowed values of a dimension, then get_data.`;

      return ok(text, {
        table_id: id,
        info,
        dimensions: dims.map((d) => ({ key: d.Key, title: d.Title.trim(), type: d.Type })),
        measures: measures.map((m) => ({ key: m.Key, title: m.Title.trim(), unit: m.Unit })),
        statline_url: statlineUrl(id),
      });
    } catch (err) {
      return fail(err);
    }
  },
);

/* --------------------------------------------------- get_dimension_codes -- */

server.registerTool(
  "get_dimension_codes",
  {
    title: "List a dimension's codes",
    description:
      "List the codes of one dimension of a StatLine table - the values you may pass to get_data. " +
      "Codes are opaque (e.g. 'T001081' for a total, '2023JJ00' for the year 2023, 'GM0363' for " +
      "Amsterdam), so look them up here rather than guessing. Large dimensions such as regions " +
      "have thousands of codes: narrow with `search`.",
    inputSchema: {
      table_id: z.string().describe("StatLine table identifier, e.g. '83583NED'."),
      dimension: z
        .string()
        .describe("Dimension key exactly as returned by get_table_info, e.g. 'Perioden'."),
      search: z
        .string()
        .optional()
        .describe("Only return codes whose label contains this text (case-insensitive)."),
      limit: z.number().int().min(1).max(500).default(100).describe("Maximum codes to return."),
      offset: z.number().int().min(0).default(0).describe("Codes to skip, for paging."),
    },
  },
  async ({ table_id, dimension, search, limit, offset }) => {
    try {
      const id = assertTableId(table_id);
      const codes = await getCodes(id, dimension, limit, offset, search);
      if (codes.length === 0) {
        return ok(
          `No codes in \`${dimension}\` of ${id}` +
            (search ? ` matching "${search}".` : `. Check the dimension key with get_table_info.`),
          { table_id: id, dimension, count: 0, codes: [] },
        );
      }
      const lines = codes.map((c) => `- \`${c.Key}\` - ${c.Title}`);
      return ok(
        `${codes.length} code(s) in \`${dimension}\` of ${id}` +
          (search ? ` matching "${search}"` : "") +
          (offset ? ` (from offset ${offset})` : "") +
          `:\n\n${lines.join("\n")}` +
          (codes.length === limit ? `\n\n(limit reached - page with offset=${offset + limit})` : ""),
        { table_id: id, dimension, count: codes.length, codes },
      );
    } catch (err) {
      return fail(err);
    }
  },
);

/* --------------------------------------------------------------- get_data -- */

server.registerTool(
  "get_data",
  {
    title: "Query StatLine observations",
    description:
      "Fetch observations from a StatLine table. Filter by dimension codes and select the " +
      "measures you want. A code ending in '*' is a prefix match, which is how you take a whole " +
      "year from the time dimension ('2023*' matches 2023JJ00, 2023KW01, 2023MM01...). " +
      "Dimension codes are resolved to readable labels in extra `<dimension>_label` columns. " +
      "Always narrow with filters - StatLine tables run to millions of rows.",
    inputSchema: {
      table_id: z.string().describe("StatLine table identifier, e.g. '83583NED'."),
      filters: z
        .record(z.array(z.string()))
        .optional()
        .describe(
          "Dimension key -> list of codes, e.g. {\"Perioden\": [\"2023*\"], " +
            "\"Bedrijfsgrootte\": [\"T001098\"]}. Multiple codes for one dimension are OR-ed; " +
            "different dimensions are AND-ed. Omit a dimension to keep all of its values.",
        ),
      measures: z
        .array(z.string())
        .optional()
        .describe("Measure keys to return. Omit to return every measure in the table."),
      limit: z.number().int().min(1).max(1000).default(50).describe("Maximum rows to return."),
      offset: z.number().int().min(0).default(0).describe("Rows to skip, for paging."),
      labels: z
        .boolean()
        .default(true)
        .describe("Add a readable `<dimension>_label` column for each dimension. Costs one request per dimension."),
    },
  },
  async ({ table_id, filters, measures, limit, offset, labels }) => {
    try {
      const id = assertTableId(table_id);
      const props = await getDataProperties(id);
      const dimKeys = props.filter(isDimension).map((p) => p.Key);
      const measureKeys = props.filter(isMeasure).map((p) => p.Key);

      const activeFilters = filters ?? {};
      const unknownDims = Object.keys(activeFilters).filter((k) => !dimKeys.includes(k));
      if (unknownDims.length) {
        throw new CbsError(
          `Table ${id} has no dimension(s) ${unknownDims.map((d) => `"${d}"`).join(", ")}. ` +
            `Its dimensions are: ${dimKeys.join(", ")}.`,
        );
      }
      const unknownMeasures = (measures ?? []).filter((m) => !measureKeys.includes(m));
      if (unknownMeasures.length) {
        throw new CbsError(
          `Table ${id} has no measure(s) ${unknownMeasures.map((m) => `"${m}"`).join(", ")}. ` +
            `Its measures are: ${measureKeys.join(", ")}.`,
        );
      }

      // Selecting measures only would drop the dimensions that identify each row.
      const select = measures?.length ? [...dimKeys, ...measures] : undefined;

      const result = await getData({
        tableId: id,
        filters: activeFilters,
        select,
        top: limit,
        skip: offset,
        typed: true,
        dimensionKeys: dimKeys,
      });

      if (result.rows.length === 0) {
        return ok(
          `No observations in ${id} for that filter (${result.filter ?? "no filter"}). ` +
            `Verify your codes with get_dimension_codes - they must match exactly.`,
          { table_id: id, filter: result.filter, returned: 0, has_more: false, rows: [] },
        );
      }

      const present = dimKeys.filter((k) => k in result.rows[0]);
      if (labels) {
        const maps = await Promise.all(
          present.map(async (k) => [k, await getCodeLabels(id, k)] as const),
        );
        for (const row of result.rows) {
          for (const [key, map] of maps) {
            const code = String(row[key] ?? "");
            row[`${key}_label`] = map.get(code) ?? code;
          }
        }
      }

      // Keep each label column next to the code it explains.
      const rest = Object.keys(result.rows[0]).filter(
        (c) => c !== "ID" && !present.includes(c) && !present.some((p) => c === `${p}_label`),
      );
      const columns = [
        ...present.flatMap((k) => (labels ? [k, `${k}_label`] : [k])),
        ...rest,
      ];

      const shown = Math.min(result.rows.length, 200);
      const text =
        `${shown} observation(s) from table ${id}` +
        (offset ? ` (offset ${offset})` : "") +
        `.\nfilter: ${result.filter ?? "(none)"}\n\n` +
        asTable(result.rows.slice(0, shown), columns) +
        (result.hasMore
          ? `\n\n(more rows match - page with offset=${offset + result.returned})`
          : `\n\n(end of results)`);

      return ok(text, {
        table_id: id,
        filter: result.filter,
        returned: result.returned,
        has_more: result.hasMore,
        rows: result.rows,
        source_url: result.url,
      });
    } catch (err) {
      return fail(err);
    }
  },
);

/* ------------------------------------------------------------------ main -- */

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  process.stderr.write("mcp-statline running on stdio\n");
}

main().catch((err) => {
  process.stderr.write(`fatal: ${String(err)}\n`);
  process.exit(1);
});
