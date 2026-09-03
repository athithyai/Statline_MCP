"""Statline MCP - an MCP server over CBS StatLine (Statistics Netherlands).

Exposes five tools:
    search_tables        find a StatLine table by keyword
    browse_themes        find a table by topic, via the CBS subject taxonomy
    get_table_info       metadata, dimensions and measures of one table
    get_dimension_codes  the code list of one dimension
    get_data             filtered observations, with code labels resolved

FastMCP Cloud entrypoint: `server.py:mcp`.
Locally: `fastmcp run server.py` (stdio) or `python server.py` (HTTP).
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

import cbs
from cbs import CbsError

SERVER_NAME = "statline-mcp"
SERVER_VERSION = "0.4.0"


class TimingMiddleware(Middleware):
    """Record and log how long each tool call takes.

    Timing at this layer measures what the caller actually waits for, including
    argument validation and label joining, rather than only the upstream
    requests. Both are recorded, so a slow answer can be attributed either to
    the network or to this server.
    """

    async def on_call_tool(self, context, call_next):
        started = time.perf_counter()
        name = getattr(context.message, "name", "unknown")
        try:
            result = await call_next(context)
        except Exception:
            cbs._record(f"tool:{name}", (time.perf_counter() - started) * 1000)
            cbs.log.warning(
                "tool %s failed after %.0fms", name, (time.perf_counter() - started) * 1000
            )
            raise
        elapsed = (time.perf_counter() - started) * 1000
        cbs._record(f"tool:{name}", elapsed)
        cbs.log.info("tool %s %.0fms", name, elapsed)
        return result


# Building the catalog index costs one 6 MB request, about a second. Doing it
# at startup rather than on first use means no user ever waits for it. It runs
# in the background so the server starts serving immediately, and a failure is
# logged rather than raised: the index is an optimisation, and search falls back
# to server-side filtering without it.
PREWARM = os.environ.get("STATLINE_PREWARM", "1") not in ("0", "false", "no")


@asynccontextmanager
async def lifespan(_server: FastMCP):
    task = None
    if PREWARM:

        async def warm() -> None:
            try:
                index = await cbs.get_catalog_index()
                cbs.log.info("catalog index ready: %d tables", len(index))
            except Exception as err:  # noqa: BLE001 - never block startup
                cbs.log.warning("catalog prewarm failed, will build on demand: %s", err)

        task = asyncio.create_task(warm())
    try:
        yield
    finally:
        if task and not task.done():
            task.cancel()
        await cbs.close_client()


mcp = FastMCP(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    middleware=[TimingMiddleware()],
    lifespan=lifespan,
    instructions=(
        "Query CBS StatLine, the open data platform of Statistics Netherlands.\n\n"
        "LANGUAGE. The catalog is bilingual but lopsided: ~4900 Dutch tables and ~1100 "
        "English ones, the English set being a translated subset with identifiers ending "
        "ENG. A table exists in one language only - its title, dimension names and code "
        "labels are all in that language - so keywords in one language rarely match "
        "tables in the other (descriptions occasionally quote a Dutch source title, "
        "which is the exception). Prefer English tables when the user writes English and a "
        "translated table covers the topic; fall back to Dutch for anything the English "
        "subset does not reach, and translate the labels when you report the answer.\n\n"
        "FINDING A TABLE. Two routes, which fail differently. search_tables matches "
        "keywords against titles, so it needs a word in the right language - pass "
        "`language` to match your keywords. browse_themes walks the CBS subject "
        "taxonomy, which has a full English tree, so it works from an English topic "
        "without translating anything. If one route comes up empty, try the other.\n\n"
        "THEN. get_table_info to see the table's dimensions and measures, "
        "get_dimension_codes to look up the codes you need, get_data to pull "
        "observations. Dimension codes are opaque (T001081 is a total, 2023JJ00 is the "
        "year 2023, GM0363 is Amsterdam) - always look them up rather than guessing."
    ),
)


# ----------------------------------------------------------------- helpers --


def _ok(text: str, structured: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(content=[TextContent(type="text", text=text)], structured_content=structured)


def _truncate(value: str | None, limit: int) -> str:
    if not value:
        return ""
    flat = " ".join(value.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _as_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    """Render rows as a pipe table so the model reads them without parsing JSON."""
    if not rows:
        return "(no rows)"
    header = "| " + " | ".join(columns) + " |"
    rule = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| "
        + " | ".join(
            "" if row.get(c) is None else str(row.get(c)).replace("|", "\\|").strip()
            for c in columns
        )
        + " |"
        for row in rows
    ]
    return "\n".join([header, rule, *body])


# ------------------------------------------------------------ search_tables --


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": True},
    description=(
        "Search the CBS StatLine catalog for tables by keyword and return their "
        "identifiers, best match first. Results are ranked, not filtered: a table that "
        "matches most of your words still appears, and the response says so when nothing "
        "matched all of them. Word stems count, so 'bevolking' finds "
        "'Bevolkingsontwikkeling'. The catalog is bilingual: ~4900 Dutch tables and ~1100 "
        "English ones (a translated subset, identifiers ending ENG). A title is written "
        "only in its table's own language, so set `language` to match the language of "
        "your keywords. Dutch gives far wider coverage (bevolking, werkloosheid, inkomen, "
        "bedrijven, criminaliteit, energie); English covers less but needs no "
        "translation. Two or three specific words work better than a long phrase. If a "
        "search still comes up empty, try browse_themes instead."
    ),
)
async def search_tables(
    query: Annotated[
        str,
        Field(
            description="Keywords matched against table title and description, e.g. 'bevolking regio'."
        ),
    ],
    language: Annotated[
        Literal["nl", "en"] | None,
        Field(
            description=(
                "Restrict to Dutch ('nl') or English ('en') tables. Match this to the "
                "language of your keywords. Both are searched if omitted."
            )
        ),
    ] = None,
    limit: Annotated[int, Field(description="Maximum tables to return.", ge=1, le=50)] = 10,
) -> ToolResult:
    try:
        found = await cbs.search_tables(query, limit, language)
    except CbsError as err:
        raise ToolError(str(err)) from err

    rows = found.rows
    if not rows:
        hint = (
            "the English catalog is a translated subset, so try language='nl' with a Dutch keyword"
            if language == "en"
            else "most titles are Dutch, so try a Dutch keyword"
        )
        return _ok(
            f'No StatLine tables matched "{query}"'
            + (f" in {language}" if language else "")
            + f". Note that {hint}, use fewer words (every word must match), or "
            f"browse_themes to navigate by topic instead.",
            {"query": query, "language": language, "count": 0, "tables": []},
        )

    blocks = [
        f"**{r['Identifier']}** - {r['Title'].strip()}\n"
        f"  [{r.get('Language') or '?'}] period: {r.get('Period') or '?'} | "
        f"frequency: {r.get('Frequency') or '?'} | "
        f"rows: {r.get('RecordCount') or '?'} | updated: {(r.get('Updated') or '')[:10]}\n"
        f"  {_truncate(r.get('ShortDescription'), 220)}"
        for r in rows
    ]
    # Say plainly when nothing matched every word. Otherwise a model reads the
    # best of a weak set as though it were an exact hit.
    if found.ranked and not found.matched_all and found.total_terms > 1:
        quality = (
            f"\n\nNo table matched all {found.total_terms} words. These match "
            f"{found.matched_terms} of them, best first. Consider a shorter query, or "
            f"browse_themes to navigate by topic."
        )
    else:
        quality = ""

    return _ok(
        f'{len(rows)} table(s) matching "{query}"'
        + (f" in {language}" if language else "")
        + ", best first:\n\n"
        + "\n\n".join(blocks)
        + quality
        + "\n\nNext: get_table_info with one of these identifiers.",
        {
            "query": query,
            "language": language,
            "count": len(rows),
            "matched_all_terms": found.matched_all,
            "tables": rows,
        },
    )


# ----------------------------------------------------------- get_table_info --


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": True},
    description=(
        "Return the metadata of one StatLine table: title, period covered, update "
        "status, plus its dimensions (the columns you filter on) and measures (the "
        "columns that hold numbers). Call this before get_data so you know which "
        "dimension and measure keys exist."
    ),
)
async def get_table_info(
    table_id: Annotated[str, Field(description="StatLine table identifier, e.g. '83583NED'.")],
) -> ToolResult:
    try:
        tid = cbs.assert_table_id(table_id)
        info, props = await asyncio.gather(cbs.get_table_info(tid), cbs.get_data_properties(tid))
    except CbsError as err:
        raise ToolError(str(err)) from err

    dims = [p for p in props if cbs.is_dimension(p)]
    measures = [p for p in props if cbs.is_measure(p)]

    dim_lines = [
        f"- `{d['Key']}` ({d['Type']}) - {d['Title'].strip()}"
        + (f"\n    {_truncate(d.get('Description'), 160)}" if d.get("Description") else "")
        for d in dims
    ]
    measure_lines = [
        f"- `{m['Key']}` - {m['Title'].strip()}"
        + (f" [{(m.get('Unit') or '').strip()}]" if m.get("Unit") else "")
        + (f"\n    {_truncate(m.get('Description'), 160)}" if m.get("Description") else "")
        for m in measures
    ]

    text = (
        f"# {info['Title'].strip()} ({tid})\n\n"
        f"language: {info.get('Language') or '?'} | period: {info.get('Period') or '?'} | "
        f"frequency: {info.get('Frequency') or '?'} | "
        f"status: {info.get('OutputStatus') or '?'} | modified: {(info.get('Modified') or '')[:10]}\n"
        f"source: {info.get('Source') or 'CBS'}\n"
        f"statline: {cbs.statline_url(tid)}\n\n"
        f"{_truncate(info.get('ShortDescription') or info.get('Summary'), 900)}\n\n"
        f"## Dimensions ({len(dims)}) - filter on these\n"
        + ("\n".join(dim_lines) or "(none)")
        + f"\n\n## Measures ({len(measures)}) - select these\n"
        + ("\n".join(measure_lines) or "(none)")
        + "\n\nNext: get_dimension_codes to see the allowed values of a dimension, then get_data."
    )
    return _ok(
        text,
        {
            "table_id": tid,
            "info": info,
            "dimensions": [
                {"key": d["Key"], "title": d["Title"].strip(), "type": d["Type"]} for d in dims
            ],
            "measures": [
                {"key": m["Key"], "title": m["Title"].strip(), "unit": m.get("Unit")}
                for m in measures
            ],
            "statline_url": cbs.statline_url(tid),
        },
    )


# ------------------------------------------------------ get_dimension_codes --


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": True},
    description=(
        "List the codes of one dimension of a StatLine table - the values you may pass "
        "to get_data. Codes are opaque (e.g. 'T001081' for a total, '2023JJ00' for the "
        "year 2023, 'GM0363' for Amsterdam), so look them up here rather than guessing. "
        "Large dimensions such as regions have thousands of codes: narrow with `search`."
    ),
)
async def get_dimension_codes(
    table_id: Annotated[str, Field(description="StatLine table identifier, e.g. '83583NED'.")],
    dimension: Annotated[
        str,
        Field(description="Dimension key exactly as returned by get_table_info, e.g. 'Perioden'."),
    ],
    search: Annotated[
        str | None,
        Field(description="Only return codes whose label contains this text (case-insensitive)."),
    ] = None,
    limit: Annotated[int, Field(description="Maximum codes to return.", ge=1, le=500)] = 100,
    offset: Annotated[int, Field(description="Codes to skip, for paging.", ge=0)] = 0,
) -> ToolResult:
    try:
        tid = cbs.assert_table_id(table_id)
        codes = await cbs.get_codes(tid, dimension, limit, offset, search)
    except CbsError as err:
        raise ToolError(str(err)) from err

    if not codes:
        detail = (
            f' matching "{search}".' if search else ". Check the dimension key with get_table_info."
        )
        return _ok(
            f"No codes in `{dimension}` of {tid}{detail}",
            {"table_id": tid, "dimension": dimension, "count": 0, "codes": []},
        )

    lines = "\n".join(f"- `{c['Key']}` - {c['Title']}" for c in codes)
    more = f"\n\n(limit reached - page with offset={offset + limit})" if len(codes) == limit else ""
    return _ok(
        f"{len(codes)} code(s) in `{dimension}` of {tid}"
        + (f' matching "{search}"' if search else "")
        + (f" (from offset {offset})" if offset else "")
        + f":\n\n{lines}{more}",
        {"table_id": tid, "dimension": dimension, "count": len(codes), "codes": codes},
    )


# ------------------------------------------------------------------ get_data --


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": True},
    description=(
        "Fetch observations from a StatLine table. Filter by dimension codes and select "
        "the measures you want. A code ending in '*' is a prefix match, which is how you "
        "take a whole year from the time dimension ('2023*' matches 2023JJ00, 2023KW01, "
        "2023MM01...). Dimension codes are resolved to readable labels in extra "
        "`<dimension>_label` columns. Always narrow with filters - StatLine tables run "
        "to millions of rows."
    ),
)
async def get_data(
    table_id: Annotated[str, Field(description="StatLine table identifier, e.g. '83583NED'.")],
    filters: Annotated[
        dict[str, list[str]] | None,
        Field(
            description=(
                'Dimension key -> list of codes, e.g. {"Perioden": ["2023*"], '
                '"Bedrijfsgrootte": ["T001098"]}. Multiple codes for one dimension are '
                "OR-ed; different dimensions are AND-ed. Omit a dimension to keep all "
                "of its values."
            )
        ),
    ] = None,
    measures: Annotated[
        list[str] | None,
        Field(description="Measure keys to return. Omit to return every measure in the table."),
    ] = None,
    limit: Annotated[int, Field(description="Maximum rows to return.", ge=1, le=1000)] = 50,
    offset: Annotated[int, Field(description="Rows to skip, for paging.", ge=0)] = 0,
    labels: Annotated[
        bool,
        Field(
            description=(
                "Add a readable `<dimension>_label` column for each dimension. "
                "Costs one request per dimension."
            )
        ),
    ] = True,
) -> ToolResult:
    try:
        tid = cbs.assert_table_id(table_id)
        props = await cbs.get_data_properties(tid)
        dim_keys = [p["Key"] for p in props if cbs.is_dimension(p)]
        measure_keys = [p["Key"] for p in props if cbs.is_measure(p)]

        active = filters or {}
        unknown_dims = [k for k in active if k not in dim_keys]
        if unknown_dims:
            raise CbsError(
                f"Table {tid} has no dimension(s) "
                + ", ".join(f'"{d}"' for d in unknown_dims)
                + f". Its dimensions are: {', '.join(dim_keys)}."
            )
        unknown_measures = [m for m in (measures or []) if m not in measure_keys]
        if unknown_measures:
            raise CbsError(
                f"Table {tid} has no measure(s) "
                + ", ".join(f'"{m}"' for m in unknown_measures)
                + f". Its measures are: {', '.join(measure_keys)}."
            )

        # Selecting measures only would drop the dimensions identifying each row.
        select = [*dim_keys, *measures] if measures else None

        # The label lookups depend only on the dimension names, which are
        # already known, so they do not have to wait for the observations.
        # Running them alongside the data request takes a whole round trip off
        # the critical path on a cold cache, and costs nothing on a warm one.
        data_task = cbs.get_data(
            table_id=tid,
            filters=active,
            dimension_keys=dim_keys,
            select=select,
            top=limit,
            skip=offset,
        )
        if labels:
            label_task = asyncio.gather(*(cbs.get_code_labels(tid, k) for k in dim_keys))
            result, label_maps = await asyncio.gather(data_task, label_task)
            maps_by_dim = dict(zip(dim_keys, label_maps, strict=False))
        else:
            result = await data_task
            maps_by_dim = {}

        if not result.rows:
            return _ok(
                f"No observations in {tid} for that filter ({result.filter or 'no filter'}). "
                f"Verify your codes with get_dimension_codes - they must match exactly.",
                {
                    "table_id": tid,
                    "filter": result.filter,
                    "returned": 0,
                    "has_more": False,
                    "rows": [],
                },
            )

        present = [k for k in dim_keys if k in result.rows[0]]
        if labels:
            for row in result.rows:
                for key in present:
                    mapping = maps_by_dim.get(key) or {}
                    code = str(row.get(key) or "")
                    row[f"{key}_label"] = mapping.get(code, code)
    except CbsError as err:
        raise ToolError(str(err)) from err

    # Keep each label column next to the code it explains.
    label_cols = {f"{k}_label" for k in present}
    rest = [c for c in result.rows[0] if c != "ID" and c not in present and c not in label_cols]
    columns = [c for k in present for c in ((k, f"{k}_label") if labels else (k,))] + rest

    shown = result.rows[:200]
    more = (
        f"\n\n(more rows match - page with offset={offset + result.returned})"
        if result.has_more
        else "\n\n(end of results)"
    )
    text = (
        f"{len(shown)} observation(s) from table {tid}"
        + (f" (offset {offset})" if offset else "")
        + f".\nfilter: {result.filter or '(none)'}\n\n"
        + _as_table(shown, columns)
        + more
    )
    return _ok(
        text,
        {
            "table_id": tid,
            "filter": result.filter,
            "returned": result.returned,
            "has_more": result.has_more,
            "rows": result.rows,
            "source_url": result.url,
        },
    )


# -------------------------------------------------------------- browse_themes --


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": True},
    description=(
        "Browse the CBS subject taxonomy to find tables by topic instead of by keyword. "
        "CBS files its ~6000 tables under a hierarchy of themes, published in both a "
        "Dutch and an English tree - so this works from English topic words where "
        "search_tables needs Dutch. Call with no arguments for the top-level themes, "
        "`theme_id` to descend into one and see the tables filed under it, or `search` "
        "to find a theme by name anywhere in the tree. Tables sit on the leaves, so "
        "keep descending until they appear."
    ),
)
async def browse_themes(
    theme_id: Annotated[
        int | None,
        Field(description="Descend into this theme: shows its children and its tables."),
    ] = None,
    search: Annotated[
        str | None,
        Field(description="Find themes whose name contains this text, at any depth."),
    ] = None,
    language: Annotated[
        Literal["nl", "en"] | None,
        Field(description="Restrict to the Dutch or English tree. Both are shown by default."),
    ] = None,
    limit: Annotated[int, Field(description="Maximum themes to list.", ge=1, le=200)] = 60,
) -> ToolResult:
    try:
        themes = await cbs.get_themes()
    except CbsError as err:
        raise ToolError(str(err)) from err

    def in_language(node: dict[str, Any]) -> bool:
        return language is None or node.get("Language") == language

    # --- search: match anywhere in the tree, showing each hit's full path ---
    if search:
        needle = search.strip().lower()
        hits = [t for t in themes if needle in (t.get("Title") or "").lower() and in_language(t)][
            :limit
        ]
        if not hits:
            return _ok(
                f'No themes matched "{search}". Try a broader word, or call '
                f"browse_themes with no arguments to see the top-level themes.",
                {"search": search, "count": 0, "themes": []},
            )
        lines = [
            f"- `{t['ID']}` [{t['Language']}] "
            + " > ".join(p["Title"].strip() for p in cbs.theme_path(themes, t["ID"]))
            for t in hits
        ]
        return _ok(
            f'{len(hits)} theme(s) matching "{search}":\n\n'
            + "\n".join(lines)
            + "\n\nNext: browse_themes with one of these theme_id values.",
            {
                "search": search,
                "count": len(hits),
                "themes": [
                    {
                        "id": t["ID"],
                        "title": t["Title"].strip(),
                        "language": t["Language"],
                        "path": [p["Title"].strip() for p in cbs.theme_path(themes, t["ID"])],
                    }
                    for t in hits
                ],
            },
        )

    # --- no theme_id: the roots of both trees ---
    if theme_id is None:
        roots = [t for t in themes if t.get("ParentID") is None and in_language(t)][:limit]
        lines = [f"- `{t['ID']}` [{t['Language']}] {t['Title'].strip()}" for t in roots]
        return _ok(
            f"{len(roots)} top-level CBS theme(s). The `nl` and `en` trees are separate "
            f"and lead to Dutch- and English-language tables respectively.\n\n"
            + "\n".join(lines)
            + "\n\nNext: browse_themes with a theme_id to descend. Tables appear on the leaves.",
            {
                "count": len(roots),
                "themes": [
                    {"id": t["ID"], "title": t["Title"].strip(), "language": t["Language"]}
                    for t in roots
                ],
            },
        )

    # --- descend into one theme ---
    node = next((t for t in themes if t["ID"] == theme_id), None)
    if node is None:
        raise ToolError(
            f"No theme with id {theme_id}. Call browse_themes with no arguments for the "
            f"top-level themes, or use `search` to find one by name."
        )

    children = [t for t in themes if t.get("ParentID") == theme_id][:limit]
    try:
        identifiers = await cbs.get_theme_tables(theme_id)
        tables = await cbs.get_tables_by_identifier(identifiers[:limit])
    except CbsError as err:
        raise ToolError(str(err)) from err

    path = cbs.theme_path(themes, theme_id)
    parts = [
        "# " + " > ".join(p["Title"].strip() for p in path) + f"  [{node['Language']}]",
    ]
    if children:
        parts.append(
            f"\n## Sub-themes ({len(children)})\n"
            + "\n".join(f"- `{c['ID']}` {c['Title'].strip()}" for c in children)
        )
    if tables:
        parts.append(
            f"\n## Tables ({len(tables)})\n"
            + "\n".join(
                f"- **{t['Identifier']}** - {t['Title'].strip()}\n"
                f"  period: {t.get('Period') or '?'} | rows: {t.get('RecordCount') or '?'} "
                f"| updated: {(t.get('Updated') or '')[:10]}"
                for t in tables
            )
        )
    if not children and not tables:
        parts.append("\n(This theme has no sub-themes and no tables filed directly under it.)")
    parts.append(
        "\nNext: "
        + (
            "get_table_info with a table identifier."
            if tables
            else "browse_themes with a sub-theme id."
        )
    )

    return _ok(
        "\n".join(parts),
        {
            "theme": {
                "id": node["ID"],
                "title": node["Title"].strip(),
                "language": node["Language"],
                "path": [p["Title"].strip() for p in path],
            },
            "children": [{"id": c["ID"], "title": c["Title"].strip()} for c in children],
            "tables": tables,
        },
    )


# ---------------------------------------------------------------- prompts --

# Dutch-language prompts. StatLine is a Dutch source: most tables, every code
# label and the wider half of the catalog are in Dutch, and most people asking
# these questions ask them in Dutch. These are MCP prompts, so any client can
# surface them as ready-made starting points rather than each user having to
# work out the search-then-drill-down method themselves.
#
# Each one states the method as well as the question, because the failure mode
# is not a model that cannot call the tools - it is a model that guesses a code
# instead of looking it up.

_WERKWIJZE = (
    "Werkwijze:\n"
    "1. Zoek eerst een geschikte tabel met search_tables (language='nl') of "
    "browse_themes. Levert de ene niets op, probeer dan de andere.\n"
    "2. Bekijk de tabel met get_table_info, zodat je de dimensies en de "
    "meetwaarden kent.\n"
    "3. Zoek de codes op met get_dimension_codes. Codes zijn betekenisloos van "
    "zichzelf, dus raad ze nooit.\n"
    "4. Haal de cijfers op met get_data.\n\n"
    "Noem de gebruikte tabel bij de identificatie, bijvoorbeeld 83583NED, en "
    "let op de eenheid van de meetwaarde: bij 'x 1 000' is 793,3 gelijk aan "
    "793300. Antwoord in het Nederlands."
)


@mcp.prompt(
    name="statistiek_vraag",
    title="Statistiekvraag beantwoorden",
    description=(
        "Beantwoord een vraag over Nederlandse statistiek met CBS StatLine, "
        "volgens de vaste werkwijze: eerst de tabel zoeken, dan de codes "
        "opzoeken, dan pas de cijfers ophalen."
    ),
)
def statistiek_vraag(
    vraag: Annotated[str, Field(description="De vraag, in het Nederlands.")],
) -> str:
    return (
        f"Beantwoord deze vraag met cijfers uit CBS StatLine.\n\n"
        f"Vraag: {vraag}\n\n"
        f"{_WERKWIJZE}\n\n"
        f"Verzin nooit een cijfer, tabelnummer of code: gebruik alleen wat "
        f"letterlijk uit een tool is teruggekomen. Kun je het niet vinden, zeg "
        f"dat dan."
    )


@mcp.prompt(
    name="tabel_verkennen",
    title="Tabel verkennen",
    description=(
        "Vat samen wat er in een StatLine-tabel zit: welke dimensies, welke "
        "meetwaarden, welke periode, en welke vragen je ermee kunt beantwoorden."
    ),
)
def tabel_verkennen(
    tabel_id: Annotated[str, Field(description="Het tabelnummer, bijvoorbeeld 83583NED.")],
) -> str:
    return (
        f"Verken StatLine-tabel {tabel_id}.\n\n"
        f"Gebruik get_table_info voor de opzet van de tabel, en "
        f"get_dimension_codes om per dimensie een paar voorbeeldcodes te laten "
        f"zien. Gebruik get_data alleen voor een enkele voorbeeldrij.\n\n"
        f"Geef daarna kort weer:\n"
        f"- waar de tabel over gaat en welke periode hij beslaat;\n"
        f"- welke dimensies er zijn en waarop je dus kunt filteren;\n"
        f"- welke meetwaarden er zijn, met hun eenheid;\n"
        f"- drie vragen die deze tabel goed kan beantwoorden.\n\n"
        f"Antwoord in het Nederlands."
    )


@mcp.prompt(
    name="regio_vergelijken",
    title="Regio's vergelijken",
    description=(
        "Vergelijk een onderwerp tussen Nederlandse gemeenten, provincies of "
        "landsdelen voor een bepaalde periode."
    ),
)
def regio_vergelijken(
    onderwerp: Annotated[
        str, Field(description="Wat je wilt vergelijken, bijvoorbeeld 'bevolking'.")
    ],
    regios: Annotated[
        str, Field(description="De regio's, bijvoorbeeld 'Amsterdam, Rotterdam, Utrecht'.")
    ],
    periode: Annotated[str, Field(description="De periode, bijvoorbeeld '2023'.")] = "2023",
) -> str:
    return (
        f"Vergelijk {onderwerp} tussen {regios} voor {periode}, met cijfers uit "
        f"CBS StatLine.\n\n"
        f"{_WERKWIJZE}\n\n"
        f"Let op: regio's hebben eigen codes, zoals GM0363 voor Amsterdam. Zoek "
        f"ze op met get_dimension_codes en gebruik de zoekterm om de juiste "
        f"gemeente te vinden. Zet het antwoord in een tabel met per regio de "
        f"naam en de waarde, en noem het gebruikte tabelnummer."
    )


@mcp.prompt(
    name="tijdreeks",
    title="Ontwikkeling door de tijd",
    description=(
        "Laat zien hoe een onderwerp zich over een reeks jaren heeft ontwikkeld, "
        "met de cijfers per periode."
    ),
)
def tijdreeks(
    onderwerp: Annotated[str, Field(description="Het onderwerp, bijvoorbeeld 'werkloosheid'.")],
    van_jaar: Annotated[str, Field(description="Beginjaar, bijvoorbeeld '2015'.")],
    tot_jaar: Annotated[str, Field(description="Eindjaar, bijvoorbeeld '2024'.")],
) -> str:
    return (
        f"Laat de ontwikkeling van {onderwerp} zien van {van_jaar} tot en met "
        f"{tot_jaar}, met cijfers uit CBS StatLine.\n\n"
        f"{_WERKWIJZE}\n\n"
        f"Voor jaarcijfers eindigt de periodecode op JJ00, dus 2023 wordt "
        f"2023JJ00. Wil je alles binnen een jaar, gebruik dan 2023* als filter. "
        f"Zet het antwoord per periode op een eigen regel als 'periode: waarde', "
        f"en benoem kort wat de trend is."
    )


# ---------------------------------------------------------------- health --


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Liveness/readiness probe for load balancers and Kubernetes.

    The MCP endpoint itself is unusable as an HTTP probe: a bare GET /mcp
    returns 406, since the protocol requires specific Accept headers. Probes
    need a plain 200, so expose one. This deliberately does not call CBS - a
    probe reports whether *this* process is serving, and should not take the
    pod down because an upstream is briefly slow. Use
    `scripts/health_check.py` for a check that includes the CBS path.
    """
    return JSONResponse({"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION})


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(_request: Request) -> JSONResponse:
    """Per-operation timings and cache effectiveness.

    Separate from /health so probes stay trivial. Answers "which call is slow"
    and "is the cache working" on a running deployment, without redeploying.
    """
    return JSONResponse(
        {
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "timings_ms": cbs.timings(),
            "caches": cbs.cache_stats(),
        }
    )


if __name__ == "__main__":
    # FastMCP Cloud imports `mcp` directly and ignores this block; it is here so
    # the same file can be run locally as an HTTP server.
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
