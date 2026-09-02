"""Measure how long each part of Statline MCP takes.

Runs a realistic question through the tool chain twice, once against a cold
cache and once warm, then prints per-operation timings. Use it to see where
time actually goes before trying to make anything faster.

    python scripts/benchmark.py                 # in-process, no server needed
    python scripts/benchmark.py --url <mcp-url> # against a running deployment

Two families of label appear in the output:

    tool:<name>   what the caller waits for, including validation and
                  label joining done in this server
    <name>        the client function underneath it
    upstream      one HTTP request to the data provider

A large gap between `tool:get_data` and `upstream` means time is going into
this server rather than the network, which is the interesting case.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import time

from fastmcp import Client

# Run from anywhere: the modules under test live in the repository root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cbs  # noqa: E402

# One realistic question: find a table, read it, resolve a code, fetch figures.
CHAIN: list[tuple[str, dict]] = [
    (
        "search_tables",
        {"query": "banen werknemers bedrijfsgrootte", "language": "nl", "limit": 3},
    ),
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

FOLLOW_UP = (
    "get_data",
    {
        "table_id": "83583NED",
        "filters": {"Perioden": ["2022JJ00"], "Bedrijfsgrootte": ["T001098"]},
        "limit": 5,
    },
)


async def _time_chain(client: Client, chain: list[tuple[str, dict]]) -> float:
    started = time.perf_counter()
    for name, args in chain:
        result = await client.call_tool(name, args, raise_on_error=False)
        if result.is_error:
            text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
            raise SystemExit(f"{name} failed: {text[:300]}")
    return (time.perf_counter() - started) * 1000


def _print_timings(title: str, rows: dict[str, dict[str, float]]) -> None:
    print(f"\n{title}")
    print(f"  {'operation':26} {'calls':>5} {'mean ms':>9} {'max ms':>9} {'total ms':>10}")
    for name, t in rows.items():
        print(
            f"  {name:26} {int(t['count']):>5} {t['mean_ms']:>9.1f} "
            f"{t['max_ms']:>9.1f} {t['total_ms']:>10.1f}"
        )


async def main(url: str | None) -> int:
    if url:
        client = Client(url)
        note = f"against {url} (timings are this process's view; server-side detail is at /metrics)"
    else:
        from server import mcp  # noqa: PLC0415

        client = Client(mcp)
        note = "in-process"

    print(f"Statline MCP benchmark, {note}")

    async with client:
        if not url:
            cbs.clear_caches()
            cbs.reset_timings()

        cold = await _time_chain(client, CHAIN)
        warm = await _time_chain(client, CHAIN)

        started = time.perf_counter()
        name, args = FOLLOW_UP
        await client.call_tool(name, args)
        follow_up = (time.perf_counter() - started) * 1000

    print()
    print(f"  four-call chain, cold cache : {cold:8.0f} ms")
    print(f"  four-call chain, warm cache : {warm:8.0f} ms", end="")
    print(f"   ({100 * (cold - warm) / cold:.0f}% faster)" if cold else "")
    print(f"  follow-up get_data, warm    : {follow_up:8.0f} ms")

    if not url:
        _print_timings("Where the time went (both runs combined)", cbs.timings())
        print("\nCache effectiveness")
        for cache_name, s in cbs.cache_stats().items():
            total = s["hits"] + s["misses"]
            rate = f"{100 * s['hits'] / total:.0f}%" if total else "n/a"
            print(
                f"  {cache_name:18} hits={int(s['hits']):<4} misses={int(s['misses']):<4} "
                f"entries={int(s['entries']):<4} hit rate {rate}"
            )
        await cbs.close_client()

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Benchmark a running server instead of an in-process one.")
    sys.exit(asyncio.run(main(parser.parse_args().url)))
