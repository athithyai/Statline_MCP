"""mcp-statline - an MCP server over CBS StatLine (Statistics Netherlands).

Exposes four tools:
    search_tables        find a StatLine table by keyword
    get_table_info       metadata, dimensions and measures of one table
    get_dimension_codes  the code list of one dimension
    get_data             filtered observations, with code labels resolved

FastMCP Cloud entrypoint: `server.py:mcp`.
Locally: `fastmcp run server.py` (stdio) or `python server.py` (HTTP).
"""

from __future__ import annotations

import asyncio
import os
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from pydantic import Field

import cbs
from cbs import CbsError

mcp = FastMCP(
    name="mcp-statline",
    version="0.2.0",
    instructions=(
        "Query CBS StatLine, the open data platform of Statistics Netherlands. "
        "Tables, dimensions and codes are in Dutch. The normal flow is: "
        "search_tables to find a table id, get_table_info to see its dimensions "
        "and measures, get_dimension_codes to look up the codes you need, then "
        "get_data to pull observations. Dimension codes are opaque (T001081 is a "
        "total, 2023JJ00 is the year 2023, GM0363 is Amsterdam) - always look "
        "them up rather than guessing."
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
        "identifiers. StatLine titles and descriptions are in Dutch, so Dutch keywords "
        "match best (bevolking, werkloosheid, inkomen, bedrijven, criminaliteit, "
        "energie). All words must match. Start here when you do not already know the "
        "table id."
    ),
)
async def search_tables(
    query: Annotated[
        str,
        Field(description="Keywords matched against table title and description, e.g. 'bevolking regio'."),
    ],
    limit: Annotated[int, Field(description="Maximum tables to return.", ge=1, le=50)] = 10,
) -> ToolResult:
    try:
        rows = await cbs.search_tables(query, limit)
    except CbsError as err:
        raise ToolError(str(err)) from err

    if not rows:
        return _ok(
            f'No StatLine tables matched "{query}". Titles are in Dutch - try a Dutch '
            f"keyword, or fewer words (every word must match).",
            {"query": query, "count": 0, "tables": []},
        )

    blocks = [
        f"**{r['Identifier']}** - {r['Title'].strip()}\n"
        f"  period: {r.get('Period') or '?'} | frequency: {r.get('Frequency') or '?'} | "
        f"rows: {r.get('RecordCount') or '?'} | updated: {(r.get('Updated') or '')[:10]}\n"
        f"  {_truncate(r.get('ShortDescription'), 220)}"
        for r in rows
    ]
    return _ok(
        f'{len(rows)} table(s) matching "{query}":\n\n' + "\n\n".join(blocks) +
        "\n\nNext: get_table_info with one of these identifiers.",
        {"query": query, "count": len(rows), "tables": rows},
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
        info, props = await asyncio.gather(
            cbs.get_table_info(tid), cbs.get_data_properties(tid)
        )
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
        f"period: {info.get('Period') or '?'} | frequency: {info.get('Frequency') or '?'} | "
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
        str, Field(description="Dimension key exactly as returned by get_table_info, e.g. 'Perioden'.")
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
            f' matching "{search}".'
            if search
            else ". Check the dimension key with get_table_info."
        )
        return _ok(
            f"No codes in `{dimension}` of {tid}{detail}",
            {"table_id": tid, "dimension": dimension, "count": 0, "codes": []},
        )

    lines = "\n".join(f"- `{c['Key']}` - {c['Title']}" for c in codes)
    more = (
        f"\n\n(limit reached - page with offset={offset + limit})" if len(codes) == limit else ""
    )
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

        result = await cbs.get_data(
            table_id=tid,
            filters=active,
            dimension_keys=dim_keys,
            select=select,
            top=limit,
            skip=offset,
        )

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
            maps = dict(
                zip(
                    present,
                    await asyncio.gather(*(cbs.get_code_labels(tid, k) for k in present)),
                )
            )
            for row in result.rows:
                for key, mapping in maps.items():
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


if __name__ == "__main__":
    # FastMCP Cloud imports `mcp` directly and ignores this block; it is here so
    # the same file can be run locally as an HTTP server.
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
