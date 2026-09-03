"""End-to-end smoke test against the live CBS API.

Connects an in-memory FastMCP client straight to the server object and
exercises every tool, including paging boundaries and error handling.

    python smoke.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys

from fastmcp import Client

import cbs
from server import mcp

FAILURES: list[str] = []

# One realistic question, reused by the caching checks.
CHAIN = [
    ("search_tables", {"query": "banen werknemers bedrijfsgrootte", "language": "nl", "limit": 3}),
    ("get_table_info", {"table_id": "83583NED"}),
    (
        "get_dimension_codes",
        {"table_id": "83583NED", "dimension": "Bedrijfsgrootte", "search": "totaal"},
    ),
    (
        "get_data",
        {
            "table_id": "83583NED",
            "filters": {"Perioden": ["2023JJ00"], "Bedrijfsgrootte": ["T001098"]},
            "limit": 5,
        },
    ),
]


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL  {name} {detail}")


async def main() -> int:
    async with Client(mcp) as client:
        tools = await client.list_tools()
        names = sorted(t.name for t in tools)
        print("tools:", ", ".join(names))
        check(
            "five tools registered",
            names
            == [
                "browse_themes",
                "get_data",
                "get_dimension_codes",
                "get_table_info",
                "search_tables",
            ],
            str(names),
        )

        async def call(name: str, **args):
            result = await client.call_tool(name, args, raise_on_error=False)
            text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
            return result, text, (result.structured_content or {})

        print("\nsearch_tables")
        _, text, data = await call("search_tables", query="bevolking", limit=3)
        check("returns tables", data.get("count", 0) > 0, text[:200])

        _, text, data = await call("search_tables", query="population", language="en", limit=5)
        check("english search returns english tables", data.get("count", 0) > 0, text[:200])
        check(
            "language filter is honoured",
            all(t["Language"] == "en" for t in data.get("tables", [])),
            str([t.get("Language") for t in data.get("tables", [])]),
        )
        check(
            "english results carry ENG identifiers",
            all(t["Identifier"].upper().endswith("ENG") for t in data.get("tables", [])),
            str([t["Identifier"] for t in data.get("tables", [])]),
        )

        _, text, data = await call("search_tables", query="bevolking", language="nl", limit=5)
        check(
            "dutch filter is honoured",
            data.get("count", 0) > 0 and all(t["Language"] == "nl" for t in data.get("tables", [])),
            text[:200],
        )

        # The filter's guarantee is about the *table's* language, not the keyword's:
        # an English table may still match a Dutch word if its description quotes a
        # Dutch source title (37259eng does exactly this), so assert the invariant
        # that actually holds rather than an empty result.
        _, text, data = await call("search_tables", query="bevolking", language="en", limit=5)
        check(
            "cross-language hits stay inside the requested language",
            all(t["Language"] == "en" for t in data.get("tables", [])),
            str([(t["Identifier"], t["Language"]) for t in data.get("tables", [])]),
        )

        _, text, data = await call(
            "search_tables", query="zzzznotarealword", language="en", limit=5
        )
        check(
            "empty english search explains the nl fallback",
            data.get("count") == 0 and "language='nl'" in text,
            text[:200],
        )

        print("\nget_table_info")
        _, text, data = await call("get_table_info", table_id="83583NED")
        check("has dimensions", len(data.get("dimensions", [])) >= 3, text[:200])
        check("has measures", len(data.get("measures", [])) >= 1)
        check("lists Perioden", "Perioden" in text)

        print("\nget_dimension_codes")
        _, text, data = await call(
            "get_dimension_codes", table_id="83583NED", dimension="Perioden", limit=5
        )
        check("returns codes", data.get("count") == 5, text[:200])

        _, text, data = await call(
            "get_dimension_codes",
            table_id="83583NED",
            dimension="Bedrijfsgrootte",
            search="totaal",
        )
        check("search narrows", 0 < data.get("count", 0) < 20, text[:200])

        # CBS labels Den Haag by its official name, "'s-Gravenhage", which shares
        # no substring with what anyone types - not even "Haag". A model searching
        # for it got zero hits and could not recover, so common names are aliased
        # onto the official spelling when the literal search misses.
        for term in ["Den Haag", "Haag", "The Hague"]:
            _, text, data = await call(
                "get_dimension_codes",
                table_id="85644NED",
                dimension="RegioS",
                search=term,
                limit=10,
            )
            hit = [c for c in data.get("codes", []) if c["Key"] == "GM0518"]
            check(f"'{term}' resolves to 's-Gravenhage", bool(hit), text[:160])

        # An alias must never override a term that already works.
        _, _, data = await call(
            "get_dimension_codes",
            table_id="85644NED",
            dimension="RegioS",
            search="Amsterdam",
            limit=10,
        )
        check(
            "a working search is left alone",
            any(c["Key"] == "GM0363" for c in data.get("codes", [])),
            str(data.get("codes", []))[:160],
        )

        print("\nget_data")
        _, text, data = await call(
            "get_data",
            table_id="83583NED",
            filters={"Perioden": ["2023*"], "Bedrijfsgrootte": ["T001098"]},
            limit=5,
        )
        rows = data.get("rows", [])
        check("returns exactly `limit` rows", data.get("returned") == 5, text[:300])
        check(
            "resolves labels",
            rows and rows[0].get("Bedrijfsgrootte_label") == "Totaal",
            json.dumps(rows[0] if rows else {}),
        )
        check(
            "label column sits next to its code",
            bool(re.search(r"\| Perioden \| Perioden_label \|", text)),
        )
        check(
            "codes are trimmed",
            all(not r["BedrijfstakkenBranchesSBI2008"].endswith(" ") for r in rows),
        )
        check("signals more pages", data.get("has_more") is True)
        print("\n".join(text.splitlines()[:6]))

        # The filter matches 124 rows; a page past the end must terminate.
        _, text, data = await call(
            "get_data",
            table_id="83583NED",
            filters={"Perioden": ["2023*"], "Bedrijfsgrootte": ["T001098"]},
            limit=50,
            offset=100,
        )
        check(
            "last page returns the remainder",
            data.get("returned") == 24,
            f"got {data.get('returned')}",
        )
        check("last page has_more is false", data.get("has_more") is False)

        _, text, data = await call(
            "get_data",
            table_id="83583NED",
            filters={"Perioden": ["2023JJ00"]},
            measures=["BanenVanWerknemersInDecember_1"],
            limit=2,
            labels=False,
        )
        rows = data.get("rows", [])
        check(
            "measure select keeps dimensions",
            rows and rows[0].get("Perioden") == "2023JJ00",
            text[:200],
        )
        check("labels:false omits label columns", "_label" not in text)

        # Regression: stored codes are padded to a fixed width per dimension
        # ("307500 "), while get_dimension_codes returns them unpadded. A plain
        # `eq` matched nothing for any code shorter than that width, which was
        # most branch and region codes. Filters now compare trim(dimension).
        _, text, data = await call(
            "get_data",
            table_id="83583NED",
            filters={
                "Perioden": ["2023JJ00"],
                "Bedrijfsgrootte": ["T001098"],
                "BedrijfstakkenBranchesSBI2008": ["307500"],
            },
            limit=5,
        )
        rows = data.get("rows", [])
        check("short padded code matches", data.get("returned") == 1, text[:200])
        check(
            "padded code resolves its label",
            rows and rows[0].get("BedrijfstakkenBranchesSBI2008_label") == "C Industrie",
            str(rows[:1]),
        )

        # The code as listed and the code as stored must behave identically.
        _, _, padded = await call(
            "get_data",
            table_id="83583NED",
            filters={
                "Perioden": ["2023JJ00"],
                "Bedrijfsgrootte": ["T001098"],
                "BedrijfstakkenBranchesSBI2008": ["307500 "],
            },
            limit=5,
        )
        check(
            "padded and unpadded forms agree",
            padded.get("returned") == data.get("returned") == 1,
            f"{padded.get('returned')} vs {data.get('returned')}",
        )

        print("\nerror handling")
        result, text, _ = await call("get_data", table_id="nope", filters={})
        check("rejects bad table id", result.is_error, text[:120])

        result, text, _ = await call("get_data", table_id="83583NED", filters={"NotADim": ["x"]})
        check("rejects unknown dimension", result.is_error and "dimension" in text, text[:160])

        result, text, _ = await call("get_data", table_id="83583NED", measures=["Bogus"], limit=1)
        check("rejects unknown measure", result.is_error, text[:160])

        result, text, data = await call(
            "get_data", table_id="83583NED", filters={"Perioden": ["1800JJ00"]}
        )
        check(
            "empty result is not an error",
            not result.is_error and "No observations" in text,
            text[:160],
        )

        print("\nbrowse_themes")
        _, text, data = await call("browse_themes")
        check("lists root themes", data.get("count", 0) > 40, text[:200])
        check(
            "both language trees present",
            {t["language"] for t in data.get("themes", [])} == {"nl", "en"},
        )

        _, text, data = await call("browse_themes", language="en")
        check(
            "language filter applies",
            all(t["language"] == "en" for t in data.get("themes", [])),
            text[:200],
        )

        _, text, data = await call("browse_themes", search="Population")
        check("search finds themes", data.get("count", 0) > 0, text[:200])
        check(
            "search results carry their path",
            all(len(t["path"]) >= 1 for t in data.get("themes", [])),
        )

        _, text, data = await call("browse_themes", theme_id=1153)
        check("descend lists tables", len(data.get("tables", [])) > 0, text[:200])
        check("descend reports its path", len(data["theme"]["path"]) >= 2, str(data.get("theme")))

        result, text, _ = await call("browse_themes", theme_id=999999)
        check("rejects unknown theme id", result.is_error, text[:120])

        # The point of the tool: an English topic must reach real data with no
        # Dutch involved. Theme titles are coarse topics, so descend to a leaf.
        _, _, data = await call("browse_themes", search="Population", language="en")
        english = data.get("themes", [])
        check("English topic search hits the en tree", bool(english), str(data)[:200])

        ids: list[str] = []
        frontier = [t["id"] for t in english][:3]
        for _ in range(4):  # bounded descent
            if ids or not frontier:
                break
            nxt: list[int] = []
            for tid in frontier[:4]:
                _, _, themed = await call("browse_themes", theme_id=tid)
                found = [t["Identifier"] for t in themed.get("tables", [])]
                if found:
                    ids = found
                    break
                nxt.extend(c["id"] for c in themed.get("children", []))
            frontier = nxt
        check("English theme descent reaches tables", bool(ids), "frontier exhausted")

        if ids:
            result, text, info = await call("get_table_info", table_id=ids[0])
            check(
                f"table {ids[0]} from theme resolves end to end",
                not result.is_error and len(info.get("dimensions", [])) >= 1,
                text[:200],
            )

        print("\nvalidation")
        result, text, _ = await call(
            "get_dimension_codes", table_id="83583NED", dimension="Perioden", limit=9999
        )
        check("limit bound is enforced", result.is_error, text[:120])

        print("\ncaching")
        # Count upstream requests directly: wall-clock is network-dependent and
        # would make this test flaky, but the request count is deterministic.
        original = cbs._get_json
        requests: list[str] = []

        async def counting(url, params=None):
            requests.append(url)
            return await original(url, params)

        cbs._get_json = counting
        try:
            cbs.clear_caches()
            requests.clear()
            for name, args in CHAIN:
                await call(name, **args)
            cold = len(requests)

            requests.clear()
            for name, args in CHAIN:
                await call(name, **args)
            warm = len(requests)
            warm_urls = list(requests)

            requests.clear()
            await call(
                "get_data",
                table_id="83583NED",
                filters={"Perioden": ["2022JJ00"], "Bedrijfsgrootte": ["T001098"]},
                limit=2,
            )
            follow_up = len(requests)
        finally:
            cbs._get_json = original

        print(f"  cold={cold} warm={warm} follow-up={follow_up} upstream requests")
        check("warm run makes fewer requests than cold", warm < cold, f"{warm} vs {cold}")
        # Exactly one, and it must be the observations: all metadata is cached,
        # while figures are always fetched fresh. Anything else would mean
        # either a metadata miss or, worse, a stale number.
        check("warm repeat costs exactly one request", warm == 1, f"{warm} requests")
        check(
            "the one warm request is the dataset, not metadata",
            warm_urls and warm_urls[0].endswith("TypedDataSet"),
            str(warm_urls),
        )
        check(
            "follow-up query costs one request",
            follow_up == 1,
            f"{follow_up} requests",
        )

        stats = cbs.cache_stats()
        check("every cache is registered", len(stats) == 7, str(list(stats)))
        check(
            "caches recorded hits",
            sum(s["hits"] for s in stats.values()) > 0,
            str({k: v["hits"] for k, v in stats.items()}),
        )

        # Observations must never be served stale.
        check(
            "observations are not cached",
            follow_up == 1,
            "a different period must still reach upstream",
        )

        print("\ntimings")
        t = cbs.timings()
        check(
            "per-tool timings recorded", any(k.startswith("tool:") for k in t), str(list(t))[:200]
        )
        check("client-function timings recorded", "get_data" in t, str(list(t))[:200])
        check("upstream timings recorded", "upstream" in t, str(list(t))[:200])

        print("\ndutch prompts")
        prompts = await client.list_prompts()
        names = sorted(p.name for p in prompts)
        check(
            "four dutch prompts registered",
            names == ["regio_vergelijken", "statistiek_vraag", "tabel_verkennen", "tijdreeks"],
            str(names),
        )

        rendered = await client.get_prompt(
            "statistiek_vraag", {"vraag": "Hoeveel inwoners heeft Nederland?"}
        )
        body = rendered.messages[0].content.text
        check("prompt embeds the question", "Hoeveel inwoners heeft Nederland?" in body)
        check("prompt is in dutch", "Werkwijze" in body and "tabel" in body.lower(), body[:120])
        check(
            "prompt names the tools to use",
            all(t in body for t in ("search_tables", "get_dimension_codes", "get_data")),
            body[:200],
        )

        series = await client.get_prompt(
            "tijdreeks", {"onderwerp": "werkloosheid", "van_jaar": "2015", "tot_jaar": "2024"}
        )
        check(
            "tijdreeks explains the period code format",
            "JJ00" in series.messages[0].content.text,
            series.messages[0].content.text[:150],
        )

    await cbs.close_client()
    print("\nALL PASS" if not FAILURES else f"\n{len(FAILURES)} FAILURE(S)")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
