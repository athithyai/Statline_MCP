"""Offline unit tests. No network, no live API.

`smoke.py` proves the server works against real data, but it cannot run when
the upstream service is down or an outbound proxy intercepts TLS, and it is too
slow to run on every keystroke. These tests cover the logic that does not need
the network: filter construction, escaping, validation, the cache, the retry
policy and the rendering helpers.

    pytest -q
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cbs  # noqa: E402
import server  # noqa: E402

# --------------------------------------------------------------- escaping --


def test_odata_literal_quotes_and_escapes():
    assert cbs.odata_literal("Amsterdam") == "'Amsterdam'"


def test_odata_literal_doubles_inner_quote():
    # Without this the literal terminates early and the filter is malformed,
    # which is the OData equivalent of an injection bug.
    assert cbs.odata_literal("men's") == "'men''s'"
    assert cbs.odata_literal("'; drop --") == "'''; drop --'"


# ------------------------------------------------------------- table ids --


@pytest.mark.parametrize("raw", ["83583NED", "83583ned", " 85388NED ", "00370", "37422eng"])
def test_assert_table_id_accepts_real_shapes(raw):
    assert cbs.assert_table_id(raw) == raw.strip().upper()


@pytest.mark.parametrize("raw", ["", "nope", "1234", "123456", "83583NEDXXX", "83583-NED"])
def test_assert_table_id_rejects_bad_shapes(raw):
    with pytest.raises(cbs.CbsError) as err:
        cbs.assert_table_id(raw)
    assert "search_tables" in str(err.value)  # the message must point somewhere useful


# ----------------------------------------------------------- data filter --


def test_filter_is_none_when_empty():
    assert cbs.build_data_filter({}) is None
    assert cbs.build_data_filter({"Perioden": []}) is None


def test_exact_match_trims_the_column():
    # Stored codes are space-padded to a fixed width per dimension while the
    # code lists return them unpadded, so a plain `eq` silently matches nothing.
    assert cbs.build_data_filter({"Perioden": ["2023JJ00"]}) == "trim(Perioden) eq '2023JJ00'"


def test_exact_match_trims_the_value_too():
    assert cbs.build_data_filter({"D": ["307500 "]}) == "trim(D) eq '307500'"


def test_star_becomes_a_prefix_match():
    assert cbs.build_data_filter({"Perioden": ["2023*"]}) == "startswith(Perioden,'2023')"


def test_same_dimension_values_are_ored():
    assert cbs.build_data_filter({"D": ["a", "b"]}) == "(trim(D) eq 'a' or trim(D) eq 'b')"


def test_different_dimensions_are_anded():
    built = cbs.build_data_filter({"A": ["1"], "B": ["2"]})
    assert built == "trim(A) eq '1' and trim(B) eq '2'"


def test_filter_values_are_escaped():
    assert "''" in cbs.build_data_filter({"D": ["'s-Gravenhage"]})


# ------------------------------------------------------- property typing --


@pytest.mark.parametrize(
    "kind,is_dim,is_measure",
    [
        ("Dimension", True, False),
        ("TimeDimension", True, False),
        ("GeoDimension", True, False),
        ("GeoDetail", True, False),
        ("Topic", False, True),
        ("TopicGroup", False, False),
    ],
)
def test_property_classification(kind, is_dim, is_measure):
    prop = {"Type": kind}
    assert cbs.is_dimension(prop) is is_dim
    assert cbs.is_measure(prop) is is_measure


# ------------------------------------------------------------ theme tree --

TREE = [
    {"ID": 0, "ParentID": None, "Title": "Root"},
    {"ID": 1, "ParentID": 0, "Title": "Middle"},
    {"ID": 2, "ParentID": 1, "Title": "Leaf"},
]


def test_theme_path_is_root_first():
    assert [t["Title"] for t in cbs.theme_path(TREE, 2)] == ["Root", "Middle", "Leaf"]


def test_theme_path_of_a_root_is_itself():
    assert [t["Title"] for t in cbs.theme_path(TREE, 0)] == ["Root"]


def test_theme_path_of_unknown_id_is_empty():
    assert cbs.theme_path(TREE, 999) == []


def test_theme_path_survives_a_cycle():
    # Defensive: a cyclic ParentID in upstream data must not hang the server.
    cyclic = [{"ID": 1, "ParentID": 2, "Title": "A"}, {"ID": 2, "ParentID": 1, "Title": "B"}]
    assert len(cbs.theme_path(cyclic, 1)) == 2


# ----------------------------------------------------------------- cache --


@pytest.mark.asyncio
async def test_cache_returns_stored_value_without_refetching():
    cache = cbs._TTLCache(ttl=60)
    calls = []

    async def fetch():
        calls.append(1)
        return "value"

    assert await cache.get_or_fetch("k", fetch) == "value"
    assert await cache.get_or_fetch("k", fetch) == "value"
    assert len(calls) == 1
    assert (cache.hits, cache.misses) == (1, 1)


@pytest.mark.asyncio
async def test_cache_refetches_after_ttl():
    cache = cbs._TTLCache(ttl=0.05)
    calls = []

    async def fetch():
        calls.append(1)
        return len(calls)

    await cache.get_or_fetch("k", fetch)
    await asyncio.sleep(0.08)
    assert await cache.get_or_fetch("k", fetch) == 2


@pytest.mark.asyncio
async def test_ttl_zero_disables_caching():
    cache = cbs._TTLCache(ttl=0)
    calls = []

    async def fetch():
        calls.append(1)
        return 1

    await cache.get_or_fetch("k", fetch)
    await cache.get_or_fetch("k", fetch)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cache_evicts_least_recently_used():
    cache = cbs._TTLCache(ttl=60, maxsize=2)

    async def fetch_for(value):
        async def fetch():
            return value

        return fetch

    await cache.get_or_fetch("a", await fetch_for("a"))
    await cache.get_or_fetch("b", await fetch_for("b"))
    await cache.get_or_fetch("a", await fetch_for("a"))  # touch 'a' so 'b' is oldest
    await cache.get_or_fetch("c", await fetch_for("c"))

    assert cache.size == 2
    assert cache.evictions == 1
    assert "b" not in cache._entries
    assert "a" in cache._entries and "c" in cache._entries


@pytest.mark.asyncio
async def test_eviction_prunes_locks_too():
    # The lock dictionary must not become the unbounded structure instead.
    cache = cbs._TTLCache(ttl=60, maxsize=2)

    async def fetch():
        return 1

    for key in range(10):
        await cache.get_or_fetch(key, fetch)
    assert cache.size == 2
    assert len(cache._locks) <= 2


@pytest.mark.asyncio
async def test_concurrent_misses_fetch_once():
    cache = cbs._TTLCache(ttl=60)
    calls = []

    async def slow():
        calls.append(1)
        await asyncio.sleep(0.05)
        return "v"

    results = await asyncio.gather(*(cache.get_or_fetch("k", slow) for _ in range(10)))
    assert results == ["v"] * 10
    assert len(calls) == 1  # the per-key lock collapsed 10 fetches into 1


@pytest.mark.asyncio
async def test_clear_resets_counters():
    cache = cbs._TTLCache(ttl=60)

    async def fetch():
        return 1

    await cache.get_or_fetch("k", fetch)
    cache.clear()
    assert cache.size == 0
    assert (cache.hits, cache.misses, cache.evictions) == (0, 0, 0)


# ----------------------------------------------------------------- retry --


def _transport(handler):
    """Swap in a mock transport so no real request leaves the process."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Every test starts with empty caches and no accumulated timings."""
    cbs.clear_caches()
    cbs.reset_timings()
    yield
    cbs.clear_caches()


@pytest.mark.asyncio
async def test_retries_a_transient_500_then_succeeds(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"value": [{"ok": True}]})

    monkeypatch.setattr(cbs, "_get_client", lambda: _transport(handler))
    monkeypatch.setattr(cbs, "RETRY_BASE_DELAY", 0.001)

    data = await cbs._get_json("https://example.test/x")
    assert data["value"] == [{"ok": True}]
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_gives_up_after_the_configured_attempts(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(503)

    monkeypatch.setattr(cbs, "_get_client", lambda: _transport(handler))
    monkeypatch.setattr(cbs, "RETRY_BASE_DELAY", 0.001)

    with pytest.raises(cbs.CbsError):
        await cbs._get_json("https://example.test/x")
    assert len(attempts) == cbs.RETRIES + 1


@pytest.mark.asyncio
async def test_404_is_not_retried(monkeypatch):
    # A missing table will still be missing a second later; retrying would only
    # make a wrong argument slow as well as wrong.
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(404)

    monkeypatch.setattr(cbs, "_get_client", lambda: _transport(handler))

    with pytest.raises(cbs.CbsError) as err:
        await cbs._get_json("https://example.test/x")
    assert len(attempts) == 1
    assert "get_table_info" in str(err.value)


@pytest.mark.asyncio
async def test_transport_error_is_retried(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 2:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"value": []})

    monkeypatch.setattr(cbs, "_get_client", lambda: _transport(handler))
    monkeypatch.setattr(cbs, "RETRY_BASE_DELAY", 0.001)

    assert await cbs._get_json("https://example.test/x") == {"value": []}
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_retry_after_header_is_honoured(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    def handler(request):
        if not slept:
            return httpx.Response(429, headers={"Retry-After": "1.5"})
        return httpx.Response(200, json={"value": []})

    monkeypatch.setattr(cbs, "_get_client", lambda: _transport(handler))
    monkeypatch.setattr(cbs.asyncio, "sleep", fake_sleep)

    await cbs._get_json("https://example.test/x")
    assert slept == [1.5]


def test_backoff_grows_and_is_jittered(monkeypatch):
    monkeypatch.setattr(cbs, "RETRY_BASE_DELAY", 1.0)
    first = [cbs._backoff(0) for _ in range(50)]
    second = [cbs._backoff(1) for _ in range(50)]
    assert all(0.5 <= d <= 1.5 for d in first)
    assert all(1.0 <= d <= 3.0 for d in second)
    assert len(set(first)) > 1  # jittered, not constant


# ------------------------------------------------------------- rendering --


def test_table_renders_header_and_rows():
    out = server._as_table([{"a": 1, "b": "x"}], ["a", "b"])
    assert out.splitlines()[0] == "| a | b |"
    assert out.splitlines()[2] == "| 1 | x |"


def test_table_escapes_pipes_in_values():
    out = server._as_table([{"a": "x|y"}], ["a"])
    assert r"x\|y" in out


def test_table_handles_no_rows():
    assert server._as_table([], ["a"]) == "(no rows)"


def test_truncate_collapses_whitespace_and_marks_elision():
    assert server._truncate("a   b\n c", 100) == "a b c"
    assert server._truncate("abcdef", 4).endswith("…")
    assert server._truncate(None, 10) == ""


# -------------------------------------------------------------- timings --


def test_timings_accumulate_count_mean_and_max():
    cbs.reset_timings()
    cbs._record("op", 10.0)
    cbs._record("op", 30.0)
    stats = cbs.timings()["op"]
    assert stats["count"] == 2
    assert stats["mean_ms"] == 20.0
    assert stats["max_ms"] == 30.0


@pytest.mark.asyncio
async def test_timed_decorator_records_the_call():
    cbs.reset_timings()

    @cbs.timed("sample")
    async def work():
        await asyncio.sleep(0.01)
        return "done"

    assert await work() == "done"
    assert cbs.timings()["sample"]["count"] == 1


@pytest.mark.asyncio
async def test_timed_records_even_when_the_call_raises():
    cbs.reset_timings()

    @cbs.timed("failing")
    async def work():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await work()
    assert cbs.timings()["failing"]["count"] == 1


# ---------------------------------------------------------- log helpers --


def test_short_url_strips_the_host():
    assert cbs._short("https://opendata.cbs.nl/ODataFeed/x") == "/ODataFeed/x"


def test_log_params_keeps_only_the_shaping_options():
    rendered = cbs._log_params({"$format": "json", "$filter": "a eq 1", "$top": 5})
    assert "$filter=a eq 1" in rendered
    assert "$format" not in rendered


# ------------------------------------------------------------ user agent --


def test_user_agent_is_overridable(monkeypatch):
    monkeypatch.setenv("STATLINE_USER_AGENT", "mine/1.0")
    import importlib

    reloaded = importlib.reload(cbs)
    try:
        assert reloaded.USER_AGENT == "mine/1.0"
    finally:
        monkeypatch.delenv("STATLINE_USER_AGENT")
        importlib.reload(cbs)


def test_cache_ttl_of_zero_is_respected_as_a_setting():
    assert cbs._TTLCache(ttl=0).ttl == 0


# --------------------------------------------------------------- ranking --


def _index(*titles: str, language: str = "nl", **extra) -> list[dict]:
    return [
        cbs._prepare(
            {
                "Identifier": f"{80000 + i}NED",
                "Title": title,
                "ShortTitle": "",
                "ShortDescription": "",
                "Language": language,
                "Updated": "2020-01-01",
                "RecordCount": 100,
                **extra,
            }
        )
        for i, title in enumerate(titles)
    ]


def test_exact_title_word_outranks_a_substring():
    index = _index("Bevolking naar leeftijd", "Iets over onderbevolkingsdruk elders")
    rows, _ = cbs.rank_tables(index, "bevolking", None, 10)
    assert rows[0]["Title"] == "Bevolking naar leeftijd"


def test_dutch_compound_is_found_by_its_stem():
    # The whole point: "bevolking" must reach "Bevolkingsontwikkeling", which
    # literal AND matching on separate words never would.
    index = _index("Bevolkingsontwikkeling per regio")
    rows, matched = cbs.rank_tables(index, "bevolking", None, 10)
    assert len(rows) == 1 and matched == 1


def test_covering_more_terms_wins():
    index = _index("Banen van werknemers naar bedrijfsgrootte", "Banen in de zorg")
    rows, matched = cbs.rank_tables(index, "banen werknemers bedrijfsgrootte", None, 10)
    assert rows[0]["Title"].startswith("Banen van werknemers")
    assert matched == 3


def test_partial_matches_still_returned_and_flagged():
    # Strict AND would return nothing here; ranking returns the best partial
    # and reports how many words it actually matched.
    index = _index("Werkloosheid naar leeftijd")
    rows, matched = cbs.rank_tables(index, "werkloosheid jongeren", None, 10)
    assert len(rows) == 1
    assert matched == 1  # only one of the two words


def test_nothing_matching_returns_nothing():
    index = _index("Bevolking naar leeftijd")
    rows, matched = cbs.rank_tables(index, "zzzznotarealword", None, 10)
    assert rows == [] and matched == 0


def test_language_filter_excludes_the_other_tree():
    index = _index("Population by age", language="en") + _index("Bevolking naar leeftijd")
    rows, _ = cbs.rank_tables(index, "population", "nl", 10)
    assert rows == []
    rows, _ = cbs.rank_tables(index, "population", "en", 10)
    assert len(rows) == 1


def test_phrase_in_order_is_boosted():
    index = _index("Banen van werknemers", "Werknemers en banen apart genoemd")
    rows, _ = cbs.rank_tables(index, "banen werknemers", None, 10)
    assert rows[0]["Title"] == "Banen van werknemers"


def test_limit_is_respected():
    index = _index(*[f"Bevolking tabel {i}" for i in range(20)])
    rows, _ = cbs.rank_tables(index, "bevolking", None, 5)
    assert len(rows) == 5


def test_ranking_strips_internal_fields():
    index = _index("Bevolking naar leeftijd")
    rows, _ = cbs.rank_tables(index, "bevolking", None, 10)
    assert not any(k.startswith("_") for k in rows[0])


def test_recent_tables_edge_out_stale_ones():
    old = _index("Bevolking cijfers", Updated="2015-01-01")
    new = _index("Bevolking cijfers", Updated="2025-01-01")
    rows, _ = cbs.rank_tables(old + new, "bevolking", None, 10)
    assert rows[0]["Updated"].startswith("2025")


def test_empty_query_matches_nothing():
    index = _index("Bevolking naar leeftijd")
    rows, matched = cbs.rank_tables(index, "   ", None, 10)
    assert rows == [] and matched == 0


def test_description_match_is_weakest():
    titled = cbs._prepare(
        {
            "Identifier": "1NED",
            "Title": "Energie verbruik",
            "ShortTitle": "",
            "ShortDescription": "",
            "Language": "nl",
            "Updated": "2020",
            "RecordCount": 1,
        }
    )
    described = cbs._prepare(
        {
            "Identifier": "2NED",
            "Title": "Iets anders",
            "ShortTitle": "",
            "ShortDescription": "gaat over energie",
            "Language": "nl",
            "Updated": "2020",
            "RecordCount": 1,
        }
    )
    rows, _ = cbs.rank_tables([described, titled], "energie", None, 10)
    assert rows[0]["Identifier"] == "1NED"


def test_search_result_reports_full_versus_partial():
    full = cbs.SearchResult(rows=[{}], matched_terms=2, total_terms=2)
    partial = cbs.SearchResult(rows=[{}], matched_terms=1, total_terms=2)
    assert full.matched_all is True
    assert partial.matched_all is False


def test_search_result_with_no_query_is_not_a_full_match():
    assert cbs.SearchResult().matched_all is False


def test_tokens_lowercases_and_drops_punctuation():
    assert cbs._tokens("Banen, werknemers; 2023!") == ["banen", "werknemers", "2023"]


def test_prepare_caps_the_description():
    row = cbs._prepare(
        {
            "Identifier": "1NED",
            "Title": "T",
            "ShortTitle": "",
            "Language": "nl",
            "ShortDescription": "x" * 1000,
        }
    )
    assert len(row["ShortDescription"]) == cbs.DESCRIPTION_CAP


# ------------------------------------------------------- data result shape --


def test_data_result_counts_its_rows():
    result = cbs.DataResult(rows=[{"a": 1}, {"a": 2}], has_more=True)
    assert result.returned == 2
    assert result.has_more is True


def test_data_result_defaults_are_empty():
    result = cbs.DataResult()
    assert result.returned == 0 and result.rows == [] and result.has_more is False


# ------------------------------------------------------------- monotonic --


def test_ms_measures_elapsed_time():
    started = time.perf_counter() - 0.05
    assert cbs._ms(started) >= 45
