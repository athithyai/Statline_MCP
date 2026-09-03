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
import random
import re
import sys
import time
from collections import OrderedDict
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


# Entries are bounded as well as timed. A long-running server that is asked
# about many different tables would otherwise hold every code list it ever
# fetched: a single large one (municipalities) is around 60 KB, and the theme
# tree is 666 KB, so an unbounded cache grows without limit in exactly the
# deployment where it matters most.
CACHE_MAXSIZE = int(os.environ.get("STATLINE_CACHE_MAXSIZE", "128"))


class _TTLCache:
    """Async LRU cache with a time-to-live and one lock per key.

    The per-key lock matters under concurrency: without it, N simultaneous
    requests for a cold key would each issue their own upstream fetch. With it,
    the first fetches and the rest wait on the result.
    """

    def __init__(self, ttl: float, maxsize: int = CACHE_MAXSIZE) -> None:
        self.ttl = ttl
        self.maxsize = max(1, maxsize)
        self._entries: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._locks: dict[Any, asyncio.Lock] = {}
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def _live(self, key: Any) -> tuple[bool, Any]:
        """Look up a key, honouring the TTL and refreshing its LRU position."""
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        if time.monotonic() - entry[0] >= self.ttl:
            # Expired: drop it rather than leaving it to be evicted later.
            self._discard(key)
            return False, None
        self._entries.move_to_end(key)
        return True, entry[1]

    def _discard(self, key: Any) -> None:
        self._entries.pop(key, None)
        # The lock dictionary has to be pruned alongside the entries, or it
        # becomes the unbounded structure instead.
        lock = self._locks.get(key)
        if lock is not None and not lock.locked():
            self._locks.pop(key, None)

    def _store(self, key: Any, value: Any) -> None:
        self._entries[key] = (time.monotonic(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.maxsize:
            oldest, _ = self._entries.popitem(last=False)
            self.evictions += 1
            lock = self._locks.get(oldest)
            if lock is not None and not lock.locked():
                self._locks.pop(oldest, None)

    async def get_or_fetch(self, key: Any, fetch: Callable[[], Awaitable[Any]]) -> Any:
        if self.ttl <= 0:
            return await fetch()

        found, value = self._live(key)
        if found:
            self.hits += 1
            return value

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Re-check: another request may have populated it while we waited.
            found, value = self._live(key)
            if found:
                self.hits += 1
                return value
            value = await fetch()
            self._store(key, value)
            self.misses += 1
            return value

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()
        self.hits = self.misses = self.evictions = 0

    @property
    def size(self) -> int:
        return len(self._entries)


_table_info_cache = _TTLCache(CACHE_TTL)
_properties_cache = _TTLCache(CACHE_TTL)
_labels_cache = _TTLCache(CACHE_TTL)
_themes_cache = _TTLCache(CACHE_TTL, maxsize=2)
_search_cache = _TTLCache(SEARCH_TTL)
_codes_cache = _TTLCache(CACHE_TTL)
# One entry per language filter; the whole catalog, so it needs no LRU room.
_catalog_cache = _TTLCache(SEARCH_TTL, maxsize=2)

_ALL_CACHES = {
    "table_info": _table_info_cache,
    "data_properties": _properties_cache,
    "code_labels": _labels_cache,
    "themes": _themes_cache,
    "search": _search_cache,
    "codes": _codes_cache,
    "catalog": _catalog_cache,
}


def cache_stats() -> dict[str, dict[str, int | float]]:
    """Hit/miss counts per cache, for the health check and for tests."""
    return {
        name: {
            "hits": c.hits,
            "misses": c.misses,
            "evictions": c.evictions,
            "entries": c.size,
            "maxsize": c.maxsize,
            "ttl": c.ttl,
        }
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


def _backoff(attempt: int) -> float:
    """Exponential delay with jitter.

    The jitter matters when several requests fail together, which is the normal
    case here because get_data fans out: without it they would all wake at the
    same instant and hit the recovering server as one burst.
    """
    return RETRY_BASE_DELAY * (2**attempt) * (0.5 + random.random())


# A single dropped connection or brief 503 should not surface to the model as a
# failed answer, so transient faults are retried with exponential backoff.
# Only faults that a retry can plausibly fix are eligible: a 404 means the table
# does not exist and will still not exist in a second, and retrying it would
# just make a wrong argument slow as well as wrong.
RETRIES = int(os.environ.get("STATLINE_RETRIES", "2"))  # attempts after the first; 0 disables
RETRY_BASE_DELAY = float(os.environ.get("STATLINE_RETRY_DELAY", "0.25"))
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _retry_after(response: httpx.Response) -> float | None:
    """Honour a Retry-After header when the server sends one."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form; fall back to our own backoff


async def _get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = {"$format": "json", **{k: v for k, v in (params or {}).items() if v not in (None, "")}}
    last_error: Exception | None = None

    for attempt in range(RETRIES + 1):
        started = time.perf_counter()
        try:
            response = await _get_client().get(url, params=query)
        except (httpx.TimeoutException, httpx.TransportError) as err:
            # Network-level faults are always worth one more try.
            last_error = err
            elapsed = _ms(started)
            _record("upstream", elapsed)
            if attempt < RETRIES:
                delay = _backoff(attempt)
                log.warning(
                    "upstream %s failed after %.0fms (%s); retry %d/%d in %.2fs",
                    _short(url),
                    elapsed,
                    type(err).__name__,
                    attempt + 1,
                    RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            log.warning("upstream %s gave up after %d attempts", _short(url), attempt + 1)
            if isinstance(err, httpx.TimeoutException):
                raise CbsError(f"CBS did not respond within 30s: {url}") from err
            raise CbsError(f"Could not reach CBS: {err}") from err

        elapsed = _ms(started)
        _record("upstream", elapsed)
        log.info(
            "upstream %s %s %.0fms %s",
            response.status_code,
            _short(url),
            elapsed,
            _log_params(query),
        )

        if response.status_code in _RETRYABLE_STATUS and attempt < RETRIES:
            delay = _retry_after(response) or _backoff(attempt)
            log.warning(
                "upstream %s returned %s; retry %d/%d in %.2fs",
                _short(url),
                response.status_code,
                attempt + 1,
                RETRIES,
                delay,
            )
            await asyncio.sleep(delay)
            continue

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

    # Reached only when the final attempt was a retryable status.
    raise CbsError(
        f"CBS kept failing for {url} after {RETRIES + 1} attempts."
        + (f" Last error: {last_error}" if last_error else "")
    )


# ------------------------------------------------------------ catalog index --

# Why rank locally instead of letting the API filter.
#
# The upstream filter is literal substring matching joined with AND, so every
# word must appear verbatim or you get nothing at all. "bevolking groei"
# returns zero tables that way, while 337 contain one of the two words. Worse,
# Dutch compounds mean the word you want is often glued inside a longer one:
# someone searching "bevolking" should find "Bevolkingsontwikkeling".
#
# The whole catalog is 5,956 rows and about 6 MB once descriptions are capped,
# and it arrives in a single request. Holding it lets us score and rank instead
# of filter, which turns a brittle exact match into an ordered best-effort list.
CATALOG_FIELDS = (
    "Identifier,Title,ShortTitle,ShortDescription,Period,Frequency,Updated,RecordCount,Language"
)
DESCRIPTION_CAP = 200

_word = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _word.findall(text.lower())


def _prepare(row: dict[str, Any]) -> dict[str, Any]:
    """Precompute the lowercase forms used for scoring, once per row."""
    title = (row.get("Title") or "").strip()
    short = (row.get("ShortTitle") or "").strip()
    desc = (row.get("ShortDescription") or "")[:DESCRIPTION_CAP]
    return {
        **row,
        "Title": title,
        "ShortDescription": desc,
        "_title": title.lower(),
        "_title_words": set(_tokens(title)),
        "_short": short.lower(),
        "_desc": desc.lower(),
    }


@timed("get_catalog_index")
async def get_catalog_index() -> list[dict[str, Any]]:
    """The whole table catalog, prepared for scoring. Cached for SEARCH_TTL."""

    async def fetch() -> list[dict[str, Any]]:
        data = await _get_json(CATALOG, {"$select": CATALOG_FIELDS})
        return [_prepare(r) for r in data.get("value", [])]

    return await _catalog_cache.get_or_fetch("all", fetch)


# Weights, highest first. A word in the title is the strongest signal we have;
# a word buried in a description is the weakest and is mostly there to rescue
# searches that would otherwise return nothing.
_W_TITLE_WORD = 10.0  # exact word in the title
_W_TITLE_STEM = 6.0  # compound or inflection: bevolking <-> bevolkingsontwikkeling
_W_TITLE_SUB = 4.0  # appears somewhere in the title
_W_SHORT = 2.0  # in the short title
_W_DESC = 1.0  # in the description
_W_PHRASE = 15.0  # the whole query, in order, in the title


def score_row(row: dict[str, Any], terms: list[str], phrase: str) -> tuple[float, int]:
    """Score one table against the query. Returns (score, terms matched).

    Pure and side-effect free, so it can be tested without touching a network.
    """
    if not terms:
        return 0.0, 0

    total = 0.0
    matched = 0
    title_words = row["_title_words"]

    for term in terms:
        if term in title_words:
            hit = _W_TITLE_WORD
        elif any(w.startswith(term) or term.startswith(w) for w in title_words):
            hit = _W_TITLE_STEM
        elif term in row["_title"]:
            hit = _W_TITLE_SUB
        elif term in row["_short"]:
            hit = _W_SHORT
        elif term in row["_desc"]:
            hit = _W_DESC
        else:
            hit = 0.0
        if hit:
            matched += 1
            total += hit

    if not matched:
        return 0.0, 0

    # Covering every word matters more than scoring highly on one of them:
    # squaring the coverage keeps a full match ahead of a strong partial one.
    coverage = matched / len(terms)
    total *= coverage**2

    if len(terms) > 1 and phrase and phrase in row["_title"]:
        total += _W_PHRASE

    # Gentle tie-breakers. A table that is still being updated, and one with
    # more observations, is usually the one a person means.
    updated = row.get("Updated") or ""
    if updated >= "2024":
        total += 1.0
    if (row.get("RecordCount") or 0) > 10000:
        total += 0.5

    return total, matched


def rank_tables(
    index: list[dict[str, Any]],
    query: str,
    language: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Rank the catalog against a query.

    Returns the best rows and how many query words the best row matched, so the
    caller can tell the difference between a confident hit and a partial one.
    """
    terms = _tokens(query)
    phrase = " ".join(terms)
    scored: list[tuple[float, int, dict[str, Any]]] = []

    for row in index:
        if language and row.get("Language") != language:
            continue
        score, matched = score_row(row, terms, phrase)
        if score > 0:
            scored.append((score, matched, row))

    scored.sort(key=lambda s: (-s[0], s[2].get("Identifier", "")))
    best_matched = scored[0][1] if scored else 0
    rows = [{k: v for k, v in row.items() if not k.startswith("_")} for _, _, row in scored[:limit]]
    return rows, best_matched


# ------------------------------------------------------------------ catalog --


@dataclass
class SearchResult:
    """Ranked catalog hits, plus how well they matched.

    `matched_terms` lets the caller distinguish a confident hit from a partial
    one, so a model is told when nothing matched every word it asked for rather
    than being handed the best of a weak set as though it were exact.
    """

    rows: list[dict[str, Any]] = field(default_factory=list)
    matched_terms: int = 0
    total_terms: int = 0
    ranked: bool = True

    @property
    def matched_all(self) -> bool:
        return self.total_terms > 0 and self.matched_terms >= self.total_terms


@timed("search_tables")
async def search_tables(terms: str, top: int, language: str | None = None) -> SearchResult:
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

    async def fetch() -> SearchResult:
        try:
            index = await get_catalog_index()
        except CbsError:
            # If the index cannot be built, fall back to letting the API filter.
            # Worse results beat no results.
            log.warning("catalog index unavailable; falling back to server-side filtering")
            rows = await _search_tables_uncached(terms, top, language)
            return SearchResult(rows=rows, matched_terms=0, total_terms=0, ranked=False)
        rows, matched = rank_tables(index, terms, language, top)
        return SearchResult(
            rows=rows, matched_terms=matched, total_terms=len(_tokens(terms)), ranked=True
        )

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
