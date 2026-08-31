"""Thin async client for the CBS (Statistics Netherlands) StatLine OData v3 feeds.

    Catalog : https://opendata.cbs.nl/ODataCatalog/Tables
    Table   : https://opendata.cbs.nl/ODataFeed/odata/{table_id}/{resource}

Every StatLine table exposes TableInfos, DataProperties, TypedDataSet,
UntypedDataSet, CategoryGroups and one entity set per dimension holding that
dimension's code list.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

CATALOG = "https://opendata.cbs.nl/ODataCatalog/Tables"

# ODataFeed rather than ODataApi: the ODataApi endpoint rejects $skip with a
# 500, so it cannot page. ODataFeed serves the same resources and supports it.
API = "https://opendata.cbs.nl/ODataFeed/odata"

# Sent on every upstream request so the data provider can see who is calling and
# has somewhere to reach out if traffic causes trouble. If you run a fork or your
# own deployment, set STATLINE_USER_AGENT so your traffic is attributed to you
# rather than to this repository - otherwise your volume shows up under someone
# else's name, and any rate limit applied to that name would follow.
USER_AGENT = os.environ.get(
    "STATLINE_USER_AGENT",
    "statline-mcp/0.3 (+https://github.com/athithyai/Statline_MCP)",
)
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

_TABLE_ID = re.compile(r"^[0-9]{5}[A-Z]{0,4}$")


class CbsError(Exception):
    """An error worth showing to the model verbatim."""


def odata_literal(value: str) -> str:
    """Escape a string literal for an OData v3 filter expression."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def assert_table_id(table_id: str) -> str:
    """Validate a StatLine table id, e.g. 83583NED."""
    trimmed = table_id.strip().upper()
    if not _TABLE_ID.match(trimmed):
        raise CbsError(
            f'"{table_id}" is not a StatLine table identifier. Expected five digits '
            f"with an optional suffix, e.g. 83583NED or 85388NED. "
            f"Use search_tables to find one."
        )
    return trimmed


# A single connection pool for the process; FastMCP serves many requests.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=TIMEOUT,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = {"$format": "json", **{k: v for k, v in (params or {}).items() if v not in (None, "")}}
    try:
        response = await _get_client().get(url, params=query)
    except httpx.TimeoutException as err:
        raise CbsError(f"CBS did not respond within 30s: {url}") from err
    except httpx.HTTPError as err:
        raise CbsError(f"Could not reach CBS: {err}") from err

    if response.status_code == 404:
        raise CbsError(
            f"CBS returned 404 for {response.url}. The table, dimension or measure "
            f"probably does not exist - check it with get_table_info."
        )
    if response.is_error:
        raise CbsError(
            f"CBS returned {response.status_code} for {response.url}. {response.text[:400]}"
        )
    try:
        return response.json()
    except ValueError as err:
        raise CbsError(f"CBS returned a non-JSON response for {response.url}.") from err


# ------------------------------------------------------------------ catalog --


async def search_tables(terms: str, top: int, language: str | None = None) -> list[dict[str, Any]]:
    """Search the StatLine catalog. Every word must match title or description.

    CBS publishes the catalog in two languages - about 4900 Dutch tables and
    1100 English ones, the English set being a translated subset. A title is
    written only in its own table's language, so keywords in one language
    rarely match tables in the other; pass `language` to search one side
    cleanly. Note this is a tendency, not a guarantee: descriptions sometimes
    quote a Dutch source title, so a Dutch word can surface an English table.
    """
    words = [w for w in terms.strip().lower().split() if w]
    clauses = [
        f"(substringof({odata_literal(w)},tolower(Title)) or "
        f"substringof({odata_literal(w)},tolower(ShortDescription)))"
        for w in words
    ]
    if language:
        clauses.append(f"Language eq {odata_literal(language)}")
    data = await _get_json(
        CATALOG,
        {
            "$filter": " and ".join(clauses) or None,
            "$select": (
                "Identifier,Title,ShortDescription,Period,Frequency,Updated,RecordCount,Language"
            ),
            "$orderby": "Updated desc",
            "$top": top,
        },
    )
    return data.get("value", [])


async def count_tables(language: str | None = None) -> int:
    """How many tables the catalog holds, optionally in one language."""
    params = {"$filter": f"Language eq {odata_literal(language)}"} if language else None
    url = f"{CATALOG}/$count"
    try:
        response = await _get_client().get(url, params=params)
        return int(response.text.strip())
    except (httpx.HTTPError, ValueError):
        return 0


# -------------------------------------------------------------------- table --


async def get_table_info(table_id: str) -> dict[str, Any]:
    data = await _get_json(f"{API}/{table_id}/TableInfos")
    rows = data.get("value", [])
    if not rows:
        raise CbsError(f"Table {table_id} has no TableInfos record.")
    return rows[0]


async def get_data_properties(table_id: str) -> list[dict[str, Any]]:
    data = await _get_json(f"{API}/{table_id}/DataProperties")
    return data.get("value", [])


def is_dimension(prop: dict[str, Any]) -> bool:
    kind = prop.get("Type", "")
    return kind.endswith("Dimension") or kind == "GeoDetail"


def is_measure(prop: dict[str, Any]) -> bool:
    return prop.get("Type") == "Topic"


# ---------------------------------------------------------------- code list --


async def get_codes(
    table_id: str,
    dimension: str,
    top: int,
    skip: int,
    search: str | None = None,
) -> list[dict[str, Any]]:
    filt = f"substringof({odata_literal(search.lower())},tolower(Title))" if search else None
    data = await _get_json(
        f"{API}/{table_id}/{dimension}",
        {"$filter": filt, "$top": top, "$skip": skip or None},
    )
    return [
        {**c, "Key": (c.get("Key") or "").strip(), "Title": (c.get("Title") or "").strip()}
        for c in data.get("value", [])
    ]


async def get_code_labels(table_id: str, dimension: str) -> dict[str, str]:
    """code -> label map for one dimension, used to decorate data rows."""
    data = await _get_json(f"{API}/{table_id}/{dimension}", {"$select": "Key,Title", "$top": 10000})
    return {
        (c["Key"] or "").strip(): (c.get("Title") or "").strip()
        for c in data.get("value", [])
        if c.get("Key")
    }


# --------------------------------------------------------------------- data --


def build_data_filter(filters: dict[str, list[str]]) -> str | None:
    """Build the $filter for a data request.

    Each entry maps a dimension key to one or more codes. A code ending in `*`
    becomes a prefix match, which is how you ask for "all of 2023" on the
    Perioden dimension (`2023*` matches 2023JJ00, 2023KW01, 2023MM01, ...).

    Exact matches compare `trim(dimension)`, not the raw column. Stored codes
    are padded to a fixed width per dimension ("307500 "), while the code lists
    return them unpadded, so a plain `eq` against a code taken from
    get_dimension_codes silently matches nothing whenever the code is shorter
    than that width. Prefix matches need no trim: the padding is trailing.
    """
    clauses: list[str] = []
    for dimension, raw in filters.items():
        values = [v for v in (raw or []) if v]
        if not values:
            continue
        parts = [
            f"startswith({dimension},{odata_literal(v[:-1].strip())})"
            if v.endswith("*")
            else f"trim({dimension}) eq {odata_literal(v.strip())}"
            for v in values
        ]
        clauses.append(parts[0] if len(parts) == 1 else "(" + " or ".join(parts) + ")")
    return " and ".join(clauses) if clauses else None


@dataclass
class DataResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    has_more: bool = False
    filter: str | None = None
    url: str = ""

    @property
    def returned(self) -> int:
        return len(self.rows)


async def get_data(
    table_id: str,
    filters: dict[str, list[str]],
    dimension_keys: list[str],
    select: list[str] | None,
    top: int,
    skip: int,
    typed: bool = True,
) -> DataResult:
    """Fetch a page of observations.

    Note: CBS ignores both `$count` and `$inlinecount=allpages` on these feeds -
    `/$count` returns the table's *total* row count regardless of `$filter`, and
    `$inlinecount` is dropped from the response. So there is no way to report how
    many rows match a filter. We request one row more than asked for and report
    `has_more` instead of an unreliable total.
    """
    resource = "TypedDataSet" if typed else "UntypedDataSet"
    filt = build_data_filter(filters)
    params = {
        "$filter": filt,
        "$select": ",".join(select) if select else None,
        "$top": top + 1,
        "$skip": skip or None,
    }
    url = f"{API}/{table_id}/{resource}"
    data = await _get_json(url, params)

    rows = data.get("value", [])
    has_more = len(rows) > top
    rows = rows[:top]

    # CBS pads dimension codes to a fixed width ("300035 "); callers compare
    # these against code-list keys, so normalise them here.
    for row in rows:
        for key in dimension_keys:
            if isinstance(row.get(key), str):
                row[key] = row[key].strip()

    query = "&".join(f"{k}={v}" for k, v in params.items() if v not in (None, ""))
    return DataResult(rows=rows, has_more=has_more, filter=filt, url=f"{url}?{query}")


def statline_url(table_id: str) -> str:
    """Human-facing StatLine page for a table."""
    return f"https://opendata.cbs.nl/statline/#/CBS/nl/dataset/{table_id}/table"


# ------------------------------------------------------------------- themes --

# CBS publishes a hierarchical subject taxonomy alongside the table catalog:
# ~1300 nodes, in both Dutch (`nl`) and English (`en`) trees, joined to tables
# by Tables_Themes. The whole tree arrives in one request, so it is fetched once
# and kept for the life of the process - it changes on the order of months.
_themes_cache: list[dict[str, Any]] | None = None
_themes_lock = asyncio.Lock()


async def get_themes() -> list[dict[str, Any]]:
    """Every theme node. Cached after the first call."""
    global _themes_cache
    async with _themes_lock:
        if _themes_cache is None:
            data = await _get_json(f"{CATALOG.rsplit('/', 1)[0]}/Themes")
            _themes_cache = data.get("value", [])
    return _themes_cache


def theme_path(themes: list[dict[str, Any]], theme_id: int) -> list[dict[str, Any]]:
    """Root-to-node path, so a model can see where it is in the tree."""
    by_id = {t["ID"]: t for t in themes}
    path: list[dict[str, Any]] = []
    node = by_id.get(theme_id)
    seen: set[int] = set()
    while node is not None and node["ID"] not in seen:
        seen.add(node["ID"])
        path.append(node)
        parent = node.get("ParentID")
        node = by_id.get(parent) if parent is not None else None
    return list(reversed(path))


async def get_theme_tables(theme_id: int) -> list[str]:
    """Table identifiers filed directly under one theme."""
    data = await _get_json(
        f"{CATALOG.rsplit('/', 1)[0]}/Tables_Themes",
        {"$filter": f"ThemeID eq {int(theme_id)}", "$select": "TableIdentifier"},
    )
    return [row["TableIdentifier"] for row in data.get("value", []) if row.get("TableIdentifier")]


async def get_tables_by_identifier(identifiers: list[str]) -> list[dict[str, Any]]:
    """Batch-resolve table identifiers to titles in a single catalog request."""
    if not identifiers:
        return []
    clause = " or ".join(f"Identifier eq {odata_literal(i)}" for i in identifiers)
    data = await _get_json(
        CATALOG,
        {
            "$filter": clause,
            "$select": "Identifier,Title,Period,Frequency,Updated,RecordCount",
        },
    )
    rows = {r["Identifier"]: r for r in data.get("value", [])}
    # Preserve the order the theme listed them in.
    return [rows[i] for i in identifiers if i in rows]
