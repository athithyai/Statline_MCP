/**
 * Thin client for the CBS (Statistics Netherlands) StatLine OData v3 feeds.
 *
 *   Catalog : https://opendata.cbs.nl/ODataCatalog/Tables
 *   Table   : https://opendata.cbs.nl/ODataFeed/odata/{tableId}/{resource}
 *
 * Every StatLine table exposes TableInfos, DataProperties, TypedDataSet,
 * UntypedDataSet, CategoryGroups and one entity set per dimension holding
 * that dimension's code list.
 */

const CATALOG = "https://opendata.cbs.nl/ODataCatalog/Tables";
// ODataFeed rather than ODataApi: the ODataApi endpoint rejects $skip, so it
// cannot page. ODataFeed serves the same resources and supports it.
const API = "https://opendata.cbs.nl/ODataFeed/odata";

const USER_AGENT = "mcp-statline/0.1 (+https://github.com/athithyai/MCP_statline)";
const TIMEOUT_MS = 30_000;

export class CbsError extends Error {}

/** Escape a string literal for an OData v3 filter expression. */
export function odataLiteral(value: string): string {
  return "'" + value.replace(/'/g, "''") + "'";
}

/** A StatLine table id, e.g. 83583NED. */
export function assertTableId(id: string): string {
  const trimmed = id.trim().toUpperCase();
  if (!/^[0-9]{5}[A-Z]{0,4}$/.test(trimmed)) {
    throw new CbsError(
      `"${id}" is not a StatLine table identifier. Expected five digits with an ` +
        `optional suffix, e.g. 83583NED or 85388NED. Use search_tables to find one.`,
    );
  }
  return trimmed;
}

async function getJson(url: string): Promise<any> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch(url, {
      headers: { Accept: "application/json", "User-Agent": USER_AGENT },
      signal: controller.signal,
    });
  } catch (err) {
    if (controller.signal.aborted) {
      throw new CbsError(`CBS did not respond within ${TIMEOUT_MS / 1000}s: ${url}`);
    }
    throw new CbsError(`Could not reach CBS: ${(err as Error).message}`);
  } finally {
    clearTimeout(timer);
  }

  if (!res.ok) {
    const body = (await res.text().catch(() => "")).slice(0, 400);
    if (res.status === 404) {
      throw new CbsError(
        `CBS returned 404 for ${url}. The table, dimension or measure probably ` +
          `does not exist - check it with get_table_info.`,
      );
    }
    throw new CbsError(`CBS returned ${res.status} ${res.statusText} for ${url}. ${body}`);
  }
  return res.json();
}

function query(params: Record<string, string | number | undefined>): string {
  const usp = new URLSearchParams();
  usp.set("$format", "json");
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") usp.set(k, String(v));
  }
  return usp.toString();
}

/* -------------------------------------------------------------- catalog -- */

export interface CatalogRow {
  Identifier: string;
  Title: string;
  ShortDescription?: string;
  Period?: string;
  Frequency?: string;
  Updated?: string;
  RecordCount?: number;
}

export async function searchTables(terms: string, top: number): Promise<CatalogRow[]> {
  const words = terms.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const filter = words
    .map(
      (w) =>
        `(substringof(${odataLiteral(w)},tolower(Title)) or ` +
        `substringof(${odataLiteral(w)},tolower(ShortDescription)))`,
    )
    .join(" and ");

  const url = `${CATALOG}?${query({
    $filter: filter || undefined,
    $select: "Identifier,Title,ShortDescription,Period,Frequency,Updated,RecordCount",
    $orderby: "Updated desc",
    $top: top,
  })}`;
  const data = await getJson(url);
  return (data.value ?? []) as CatalogRow[];
}

/* ---------------------------------------------------------------- table -- */

export interface TableInfo {
  Identifier: string;
  Title: string;
  ShortTitle?: string;
  Summary?: string;
  ShortDescription?: string;
  Period?: string;
  Frequency?: string;
  Modified?: string;
  OutputStatus?: string;
  Source?: string;
  Language?: string;
}

export async function getTableInfo(tableId: string): Promise<TableInfo> {
  const data = await getJson(`${API}/${tableId}/TableInfos?${query({})}`);
  const row = data.value?.[0];
  if (!row) throw new CbsError(`Table ${tableId} has no TableInfos record.`);
  return row as TableInfo;
}

export interface DataProperty {
  /** Dimension | TimeDimension | GeoDimension | GeoDetail | Topic | TopicGroup */
  Type: string;
  Key: string;
  Title: string;
  Description?: string;
  Unit?: string;
  Decimals?: number;
  Datatype?: string;
  ParentID?: number | null;
  ID?: number;
}

export async function getDataProperties(tableId: string): Promise<DataProperty[]> {
  const data = await getJson(`${API}/${tableId}/DataProperties?${query({})}`);
  return (data.value ?? []) as DataProperty[];
}

export function isDimension(p: DataProperty): boolean {
  return p.Type.endsWith("Dimension") || p.Type === "GeoDetail";
}

export function isMeasure(p: DataProperty): boolean {
  return p.Type === "Topic";
}

/* ------------------------------------------------------------ code list -- */

export interface Code {
  Key: string;
  Title: string;
  Description?: string;
  CategoryGroupID?: number;
}

export async function getCodes(
  tableId: string,
  dimensionKey: string,
  top: number,
  skip: number,
  search?: string,
): Promise<Code[]> {
  const filter = search
    ? `substringof(${odataLiteral(search.toLowerCase())},tolower(Title))`
    : undefined;
  const url = `${API}/${tableId}/${encodeURIComponent(dimensionKey)}?${query({
    $filter: filter,
    $top: top,
    $skip: skip || undefined,
  })}`;
  const data = await getJson(url);
  return ((data.value ?? []) as Code[]).map((c) => ({
    ...c,
    Key: c.Key?.trim(),
    Title: c.Title?.trim(),
  }));
}

/** code -> label map for one dimension, used to decorate data rows. */
export async function getCodeLabels(
  tableId: string,
  dimensionKey: string,
): Promise<Map<string, string>> {
  const url = `${API}/${tableId}/${encodeURIComponent(dimensionKey)}?${query({
    $select: "Key,Title",
    $top: 10000,
  })}`;
  const data = await getJson(url);
  const map = new Map<string, string>();
  for (const c of (data.value ?? []) as Code[]) {
    if (c.Key) map.set(c.Key.trim(), (c.Title ?? "").trim());
  }
  return map;
}

/* ----------------------------------------------------------------- data -- */

/**
 * Build the $filter for a data request.
 *
 * Each entry maps a dimension key to one or more codes. A code ending in `*`
 * becomes a prefix match, which is how you ask for "all of 2023" on the
 * Perioden dimension (`2023*` matches 2023JJ00, 2023KW01, 2023MM01, ...).
 */
export function buildDataFilter(filters: Record<string, string[]>): string | undefined {
  const clauses: string[] = [];
  for (const [dim, rawValues] of Object.entries(filters)) {
    const values = (rawValues ?? []).filter((v) => v !== undefined && v !== null && v !== "");
    if (values.length === 0) continue;
    const parts = values.map((v) =>
      v.endsWith("*")
        ? `startswith(${dim},${odataLiteral(v.slice(0, -1))})`
        : `${dim} eq ${odataLiteral(v)}`,
    );
    clauses.push(parts.length === 1 ? parts[0] : `(${parts.join(" or ")})`);
  }
  return clauses.length ? clauses.join(" and ") : undefined;
}

export interface DataResult {
  rows: Record<string, unknown>[];
  returned: number;
  /** True when more rows match beyond this page. */
  hasMore: boolean;
  filter?: string;
  url: string;
}

/**
 * Fetch a page of observations.
 *
 * Note: CBS ignores both `$count` and `$inlinecount=allpages` on these feeds -
 * `/$count` returns the table's *total* row count regardless of `$filter`, and
 * `$inlinecount` is dropped from the response. So there is no way to report how
 * many rows match a filter. We request one row more than asked for and report
 * `hasMore` instead of an unreliable total.
 */
export async function getData(opts: {
  tableId: string;
  filters: Record<string, string[]>;
  select?: string[];
  top: number;
  skip: number;
  typed: boolean;
  dimensionKeys: string[];
}): Promise<DataResult> {
  const { tableId, filters, select, top, skip, typed, dimensionKeys } = opts;
  const resource = typed ? "TypedDataSet" : "UntypedDataSet";
  const filter = buildDataFilter(filters);

  const dataUrl = `${API}/${tableId}/${resource}?${query({
    $filter: filter,
    $select: select?.length ? select.join(",") : undefined,
    $top: top + 1,
    $skip: skip || undefined,
  })}`;

  const data = await getJson(dataUrl);
  const all = (data.value ?? []) as Record<string, unknown>[];
  const hasMore = all.length > top;
  const rows = hasMore ? all.slice(0, top) : all;

  // CBS pads dimension codes to a fixed width ("300035 "); callers compare
  // these against code-list keys, so normalise them here.
  for (const row of rows) {
    for (const key of dimensionKeys) {
      if (typeof row[key] === "string") row[key] = (row[key] as string).trim();
    }
  }

  return { rows, returned: rows.length, hasMore, filter, url: dataUrl };
}

/** Human-facing StatLine page for a table. */
export function statlineUrl(tableId: string): string {
  return `https://opendata.cbs.nl/statline/#/CBS/nl/dataset/${tableId}/table`;
}
