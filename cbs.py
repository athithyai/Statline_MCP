"""Thin async client for the CBS (Statistics Netherlands) StatLine OData v3 feeds.

    Catalog : https://opendata.cbs.nl/ODataCatalog/Tables
    Table   : https://opendata.cbs.nl/ODataFeed/odata/{table_id}/{resource}

Every StatLine table exposes TableInfos, DataProperties, TypedDataSet,
UntypedDataSet, CategoryGroups and one entity set per dimension holding that
dimension's code list.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
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
    "statline-mcp/0.4 (+https://github.com/athithyai/Statline_MCP)",
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


# ----------------------------------------------------------- logging + timing --

# Logs go to stderr, never stdout: under the stdio transport stdout carries the
# MCP protocol itself, and a stray print corrupts the stream.
log = logging.getLogger("statline")
if not log.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s"))
    log.addHandler(_handler)
log.setLevel(os.environ.get("STATLINE_LOG_LEVEL", "WARNING").upper())
log.propagate = False


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _log_params(query: dict[str, Any]) -> str:
    """One-line rendering of the OData options that actually shape a request."""
    interesting = {k: v for k, v in query.items() if k in ("$filter", "$top", "$skip")}
    rendered = " ".join(f"{k}={v}" for k, v in interesting.items())
    return rendered[:200]


@dataclass
class _Timing:
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0


# Timings are always collected, independent of log level: it is two dict lookups
# and three additions per call, far cheaper than the network it measures, and it
# means a slow deployment can be diagnosed without redeploying with new flags.
_timings: dict[str, _Timing] = {}


def _record(label: str, elapsed_ms: float) -> None:
    t = _timings.get(label)
    if t is None:
        t = _timings[label] = _Timing()
    t.count += 1
    t.total_ms += elapsed_ms
    t.max_ms = max(t.max_ms, elapsed_ms)


def timings() -> dict[str, dict[str, float]]:
    """Per-operation call count, mean and max in milliseconds."""
    return {
        name: {
            "count": t.count,
            "mean_ms": round(t.mean_ms, 1),
            "max_ms": round(t.max_ms, 1),
            "total_ms": round(t.total_ms, 1),
        }
        for name, t in sorted(_timings.items(), key=lambda kv: -kv[1].total_ms)
    }


def reset_timings() -> None:
    _timings.clear()


def timed(label: str):
    """Decorator recording how long one async operation takes."""

    def wrap(fn):
        @functools.wraps(fn)
        async def inner(*args, **kwargs):
            started = time.perf_counter()
            try:
                return await fn(*args, **kwargs)
            finally:
                elapsed = _ms(started)
                _record(label, elapsed)
                if log.isEnabledFor(logging.DEBUG):
                    log.debug("%s took %.0fms", label, elapsed)

        return inner

    return wrap


# --------------------------------------------------------------------- cache --

# Metadata is cached; observations are not.
#
# A single question costs several metadata requests, and they repeat: get_data
# re-reads DataProperties to validate its arguments after get_table_info has
# already fetched it, and re-reads every dimension's code list on each call to
# resolve labels. That metadata describes a table's shape, which changes when a
# table is revised - on the order of weeks - so a few hours of staleness is
# invisible, while the saving is most of the traffic in a multi-turn session.
#
# Observations are deliberately never cached: they are the volatile part, they
# are unbounded in size, and a stale figure is a wrong answer.
CACHE_TTL = float(os.environ.get("STATLINE_CACHE_TTL", "21600"))  # 6 hours; 0 disables

# Catalog searches are queries rather than metadata, so they get their own,
# much shorter life. Repeating an identical search is common within a single
# conversation - a model reformulates, then comes back to an earlier phrase -
# and five minutes is long enough to make that free while staying fresh enough
# that a newly published table shows up the same day.
SEARCH_TTL = float(os.environ.get("STATLINE_SEARCH_TTL", "300"))  # 5 minutes; 0 disables


class _TTLCache:
    """Async cache with a time-to-live and one lock per key.

    The per-key lock matters under concurrency: without it, N simultaneous
    requests for a cold key would each issue their own upstream fetch. With it,
    the first fetches and the rest wait on the result.
    """

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._entries: dict[Any, tuple[float, Any]] = {}
        self._locks: dict[Any, asyncio.Lock] = {}
        self.hits = 0
        self.misses = 0

    async def get_or_fetch(self, key: Any, fetch: Callable[[], Awaitable[Any]]) -> Any:
        if self.ttl <= 0:
            return await fetch()

        entry = self._entries.get(key)
        if entry is not None and time.monotonic() - entry[0] < self.ttl:
            self.hits += 1
            return entry[1]

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Re-check: another request may have populated it while we waited.
            entry = self._entries.get(key)
            if entry is not None and time.monotonic() - entry[0] < self.ttl:
                self.hits += 1
                return entry[1]
            value = await fetch()
            self._entries[key] = (time.monotonic(), value)
            self.misses += 1
            return value

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()
        self.hits = self.misses = 0

    @property
    def size(self) -> int:
        return len(self._entries)


_table_info_cache = _TTLCache(CACHE_TTL)
_properties_cache = _TTLCache(CACHE_TTL)
_labels_cache = _TTLCache(CACHE_TTL)
_themes_cache = _TTLCache(CACHE_TTL)
_search_cache = _TTLCache(SEARCH_TTL)
_codes_cache = _TTLCache(CACHE_TTL)

_ALL_CACHES = {
    "table_info": _table_info_cache,
    "data_properties": _properties_cache,
    "code_labels": _labels_cache,
    "themes": _themes_cache,
    "search": _search_cache,
    "codes": _codes_cache,
}


def cache_stats() -> dict[str, dict[str, int | float]]:
    """Hit/miss counts per cache, for the health check and for tests."""
    return {
        name: {"hits": c.hits, "misses": c.misses, "entries": c.size, "ttl": c.ttl}
        for name, c in _ALL_CACHES.items()
    }


def clear_caches() -> None:
    for cache in _ALL_CACHES.values():
        cache.clear()


# A single connection pool for the process; FastMCP serves many requests.
_client: httpx.AsyncClient | None = None


def _verify() -> str | bool:
    """What to validate upstream TLS against.

    Behind a proxy that terminates and re-signs TLS, the certificate presented
    for opendata.cbs.nl is the proxy's, not the real one, so the default trust
    store rejects it. Point STATLINE_CA_BUNDLE (or the conventional
    SSL_CERT_FILE / REQUESTS_CA_BUNDLE) at the organisation's CA bundle and
    verification succeeds against that instead. Verification is never disabled.
    """
    for var in ("STATLINE_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(var)
        if path:
            return path
    return True


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        # trust_env=True (the default) also picks up HTTP_PROXY, HTTPS_PROXY
        # and NO_PROXY, which is how this reaches the internet on a network
        # that routes outbound traffic through a proxy.
        _client = httpx.AsyncClient(
            timeout=TIMEOUT,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            follow_redirects=True,
            verify=_verify(),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _short(url: str) -> str:
    """The identifying tail of an upstream URL, for log lines."""
    return url.replace("https://opendata.cbs.nl", "")


async def _get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = {"$format": "json", **{k: v for k, v in (params or {}).items() if v not in (None, "")}}
    started = time.perf_counter()
    try:
        response = await _get_client().get(url, params=query)
    except httpx.TimeoutException as err:
        log.warning("upstream timeout %s after %.0fms", _short(url), _ms(started))
        raise CbsError(f"CBS did not respond within 30s: {url}") from err
    except httpx.HTTPError as err:
        log.warning("upstream error %s after %.0fms: %s", _short(url), _ms(started), err)
        raise CbsError(f"Could not reach CBS: {err}") from err

    elapsed = _ms(started)
    log.info(
        "upstream %s %s %.0fms %s",
        response.status_code,
        _short(url),
        elapsed,
        _log_params(query),
    )
    _record("upstream", elapsed)

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


@timed("search_tables")
async def search_tables(terms: str, top: int, language: str | None = None) -> list[dict[str, Any]]:
    """Search the StatLine catalog. Every word must match title or description.

    CBS publishes the catalog in two languages - about 4900 Dutch tables and
    1100 English ones, the English set being a translated subset. A title is
    written only in its own table's language, so keywords in one language
    rarely match tables in the other; pass `language` to search one side
    cleanly. Note this is a tendency, not a guarantee: descriptions sometimes
    quote a Dutch source title, so a Dutch word can surface an English table.

    Cached for SEARCH_TTL, keyed on the normalised query, so a model that
    circles back to a phrase it already tried pays nothing the second time.
    """

    async def fetch() -> list[dict[str, Any]]:
        return await _search_tables_uncached(terms, top, language)

    return await _search_cache.get_or_fetch((terms.strip().lower(), top, language), fetch)


async def _search_tables_uncached(
    terms: str, top: int, language: str | None
) -> list[dict[str, Any]]:
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


@timed("get_table_info")
async def get_table_info(table_id: str) -> dict[str, Any]:
    """A table's descriptive metadata. Cached for CACHE_TTL."""

    async def fetch() -> dict[str, Any]:
        data = await _get_json(f"{API}/{table_id}/TableInfos")
        rows = data.get("value", [])
        if not rows:
            raise CbsError(f"Table {table_id} has no TableInfos record.")
        return rows[0]

    return await _table_info_cache.get_or_fetch(table_id, fetch)


@timed("get_data_properties")
async def get_data_properties(table_id: str) -> list[dict[str, Any]]:
    """A table's dimensions and measures. Cached for CACHE_TTL.

    Read on nearly every call - get_data validates its arguments against it -
    so this is the single most repeated request in a session.
    """

    async def fetch() -> list[dict[str, Any]]:
        data = await _get_json(f"{API}/{table_id}/DataProperties")
        return data.get("value", [])

    return await _properties_cache.get_or_fetch(table_id, fetch)


def is_dimension(prop: dict[str, Any]) -> bool:
    kind = prop.get("Type", "")
    return kind.endswith("Dimension") or kind == "GeoDetail"


def is_measure(prop: dict[str, Any]) -> bool:
    return prop.get("Type") == "Topic"


# ---------------------------------------------------------------- code list --


# CBS labels places with their official names, which for several well-known
# Dutch cities share no substring with the name people actually use: searching
# "Den Haag", or even "Haag", cannot match "'s-Gravenhage". A caller with no way
# to guess the official spelling simply gets nothing back, so map the common
# name onto a fragment of the official one.
_SEARCH_ALIASES = {
    "den haag": "gravenhage",
    "the hague": "gravenhage",
    "haag": "gravenhage",
    "den bosch": "hertogenbosch",
    "s-hertogenbosch": "hertogenbosch",
    "the netherlands": "nederland",
    "holland": "nederland",
}


def alias_for(term: str) -> str | None:
    """A better search fragment for a term CBS spells differently, if any."""
    key = term.strip().lower().strip("'\"")
    return _SEARCH_ALIASES.get(key)


@timed("get_codes")
async def get_codes(
    table_id: str,
    dimension: str,
    top: int,
    skip: int,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """A dimension's codes, optionally filtered. Cached for CACHE_TTL.

    Cached on the whole argument tuple, so the alias fallback below runs once
    per distinct search term rather than on every repeat of it.
    """

    async def fetch() -> list[dict[str, Any]]:
        return await _get_codes_uncached(table_id, dimension, top, skip, search)

    return await _codes_cache.get_or_fetch((table_id, dimension, top, skip, search), fetch)


async def _get_codes_uncached(
    table_id: str,
    dimension: str,
    top: int,
    skip: int,
    search: str | None = None,
) -> list[dict[str, Any]]:
    async def fetch(term: str | None) -> list[dict[str, Any]]:
        filt = f"substringof({odata_literal(term.lower())},tolower(Title))" if term else None
        data = await _get_json(
            f"{API}/{table_id}/{dimension}",
            {"$filter": filt, "$top": top, "$skip": skip or None},
        )
        return [
            {**c, "Key": (c.get("Key") or "").strip(), "Title": (c.get("Title") or "").strip()}
            for c in data.get("value", [])
        ]

    rows = await fetch(search)
    if not rows and search:
        # Only on a miss, so a term that already works is never second-guessed.
        alias = alias_for(search)
        if alias:
            rows = await fetch(alias)
    return rows


@timed("get_code_labels")
async def get_code_labels(table_id: str, dimension: str) -> dict[str, str]:
    """code -> label map for one dimension, used to decorate data rows.

    Cached for CACHE_TTL: get_data fetches one of these per dimension on every
    call, so an uncached three-dimension table costs three extra requests each
    time the same table is queried.
    """

    async def fetch() -> dict[str, str]:
        data = await _get_json(
            f"{API}/{table_id}/{dimension}", {"$select": "Key,Title", "$top": 10000}
        )
        return {
            (c["Key"] or "").strip(): (c.get("Title") or "").strip()
            for c in data.get("value", [])
            if c.get("Key")
        }

    return await _labels_cache.get_or_fetch((table_id, dimension), fetch)


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


@timed("get_data")
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
# and cached - it changes on the order of months.


@timed("get_themes")
async def get_themes() -> list[dict[str, Any]]:
    """Every theme node. Cached for CACHE_TTL."""

    async def fetch() -> list[dict[str, Any]]:
        data = await _get_json(f"{CATALOG.rsplit('/', 1)[0]}/Themes")
        return data.get("value", [])

    return await _themes_cache.get_or_fetch("all", fetch)


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


@timed("get_theme_tables")
async def get_theme_tables(theme_id: int) -> list[str]:
    """Table identifiers filed directly under one theme."""
    data = await _get_json(
        f"{CATALOG.rsplit('/', 1)[0]}/Tables_Themes",
        {"$filter": f"ThemeID eq {int(theme_id)}", "$select": "TableIdentifier"},
    )
    return [row["TableIdentifier"] for row in data.get("value", []) if row.get("TableIdentifier")]


@timed("get_tables_by_identifier")
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
