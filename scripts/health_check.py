"""Health check for a running Statline MCP server.

Two levels:

    --transport-only   the MCP endpoint completes a handshake and lists its
                       tools. Used by the Docker HEALTHCHECK, so it stays fast
                       and does not depend on CBS being up.

    (default)          the above, plus a live query through to CBS. Use this to
                       confirm the whole path works after a deploy.

    python scripts/health_check.py
    python scripts/health_check.py --url https://your-server.fastmcp.app/mcp
    python scripts/health_check.py --transport-only

Exits 0 when healthy, 1 when not, so it works in CI and container probes.
Set MCP_STATLINE_URL to avoid passing --url. If the server needs a bearer
token, set MCP_STATLINE_TOKEN.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from fastmcp import Client

EXPECTED_TOOLS = {
    "browse_themes",
    "get_data",
    "get_dimension_codes",
    "get_table_info",
    "search_tables",
}

DEFAULT_URL = f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/mcp"


async def check(url: str, transport_only: bool, timeout: float) -> int:
    token = os.environ.get("MCP_STATLINE_TOKEN")
    client = Client(url, auth=token) if token else Client(url)

    try:
        async with asyncio.timeout(timeout):
            async with client:
                tools = {t.name for t in await client.list_tools()}

                missing = EXPECTED_TOOLS - tools
                if missing:
                    print(f"UNHEALTHY: server is missing tool(s): {', '.join(sorted(missing))}")
                    return 1
                print(f"ok: {url} serving {len(tools)} tools")

                if transport_only:
                    print("HEALTHY (transport only)")
                    return 0

                # One cheap end-to-end query: exercises the CBS path and the
                # code-label lookup without pulling a large page.
                result = await client.call_tool(
                    "get_data",
                    {
                        "table_id": "83583NED",
                        "filters": {"Perioden": ["2023JJ00"], "Bedrijfsgrootte": ["T001098"]},
                        "limit": 1,
                    },
                    raise_on_error=False,
                )
                if result.is_error:
                    body = "\n".join(c.text for c in result.content if hasattr(c, "text"))
                    print(f"UNHEALTHY: upstream CBS query failed: {body[:300]}")
                    return 1

                rows = (result.structured_content or {}).get("rows") or []
                if not rows:
                    print("UNHEALTHY: CBS query returned no rows for a known-good filter")
                    return 1
                if not rows[0].get("Bedrijfsgrootte_label"):
                    print("UNHEALTHY: dimension labels did not resolve")
                    return 1

                print(
                    f"ok: CBS reachable, sample row labelled '{rows[0]['Bedrijfsgrootte_label']}'"
                )
                print("HEALTHY")
                return 0
    except TimeoutError:
        print(f"UNHEALTHY: no response from {url} within {timeout:.0f}s")
        return 1
    except Exception as err:  # noqa: BLE001 - a probe must report, never raise
        print(f"UNHEALTHY: {type(err).__name__}: {err}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("MCP_STATLINE_URL", DEFAULT_URL),
        help=f"MCP endpoint to probe (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--transport-only",
        action="store_true",
        help="Skip the live CBS query; only check the server answers and lists its tools.",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="Seconds to allow.")
    args = parser.parse_args()
    return asyncio.run(check(args.url, args.transport_only, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
