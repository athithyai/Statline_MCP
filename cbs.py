"""Thin async client for the CBS (Statistics Netherlands) StatLine OData v3 feeds.

    Catalog : https://opendata.cbs.nl/ODataCatalog/Tables
    Table   : https://opendata.cbs.nl/ODataFeed/odata/{table_id}/{resource}

Every StatLine table exposes TableInfos, DataProperties, TypedDataSet,
UntypedDataSet, CategoryGroups and one entity set per dimension holding that
dimension's code list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

CATALOG = "https://opendata.cbs.nl/ODataCatalog/Tables"

# ODataFeed rather than ODataApi: the ODataApi endpoint rejects $skip with a
# 500, so it cannot page. ODataFeed serves the same resources and supports it.
API = "https://opendata.cbs.nl/ODataFeed/odata"

USER_AGENT = "mcp-statline/0.2 (+https://github.com/athithyai/MCP_statline)"
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


async def search_tables(terms: str, top: int) -> list[dict[str, Any]]:
    """Search the StatLine catalog. Every word must match title or description."""
    words = [w for w in terms.strip().lower().split() if w]
    clauses = [
        f"(substringof({odata_literal(w)},tolower(Title)) or "
        f"substringof({odata_literal(w)},tolower(ShortDescription)))"
        for w in words
    ]
    data = await _get_json(
        CATALOG,
        {
            "$filter": " and ".join(clauses) or None,
            "$select": "Identifier,Title,ShortDescription,Period,Frequency,Updated,RecordCount",
            "$orderby": "Updated desc",
            "$top": top,
        },
    )
    return data.get("value", [])


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
    filt = (
        f"substringof({odata_literal(search.lower())},tolower(Title))" if search else None
    )
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
    data = await _get_json(
        f"{API}/{table_id}/{dimension}", {"$select": "Key,Title", "$top": 10000}
    )
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
    """
    clauses: list[str] = []
    for dimension, raw in filters.items():
        values = [v for v in (raw or []) if v]
        if not values:
            continue
        parts = [
            f"startswith({dimension},{odata_literal(v[:-1])})"
            if v.endswith("*")
            else f"{dimension} eq {odata_literal(v)}"
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
