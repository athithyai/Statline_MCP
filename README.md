# mcp-statline

An [MCP](https://modelcontextprotocol.io) server over **CBS StatLine** — the open data
platform of Statistics Netherlands (Centraal Bureau voor de Statistiek). It lets an LLM
find a StatLine table, inspect its dimensions, look up the codes it needs, and pull
filtered observations back as a readable table.

Built with [FastMCP](https://gofastmcp.com). No API key — CBS StatLine open data is free
to use under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.nl).

## Tools

| Tool | What it does |
| --- | --- |
| `search_tables` | Search the StatLine catalog by keyword. Titles are in Dutch, so Dutch keywords match best. |
| `get_table_info` | Metadata for one table plus its **dimensions** (what you filter on) and **measures** (the numbers). |
| `get_dimension_codes` | The code list of one dimension, with `search` to narrow large ones like regions. |
| `get_data` | Filtered observations, with dimension codes resolved to readable labels. |

The intended flow is `search_tables` → `get_table_info` → `get_dimension_codes` → `get_data`.

## Run it locally

```bash
pip install -r requirements.txt
fastmcp run server.py
```

Add it to Claude Code:

```bash
claude mcp add statline -- fastmcp run /absolute/path/to/MCP_statline/server.py
```

To serve it over HTTP instead of stdio:

```bash
python server.py
```

That listens on `http://localhost:8000/mcp` (override with `PORT`).

## Deploy to FastMCP Cloud

The repo is already shaped for it — [FastMCP Cloud](https://gofastmcp.com/deployment/fastmcp-cloud)
builds straight from GitHub and installs from `requirements.txt`.

1. Sign in at [fastmcp.cloud](https://fastmcp.cloud) with GitHub and authorise access to
   this repository (public or private both work).
2. Create a server pointing at this repo with entrypoint **`server.py:mcp`**.
3. It deploys in well under a minute and redeploys on every push to `main`.

You get a URL like `https://<your-server-name>.fastmcp.app/mcp`, which connects as:

```bash
claude mcp add --transport http statline https://<your-server-name>.fastmcp.app/mcp
```

The `if __name__ == "__main__"` block in `server.py` is ignored by the platform — it only
exists so the same file runs locally over HTTP.

## Example

> How many employee jobs were there in Dutch manufacturing in December 2023?

```
search_tables      { "query": "banen werknemers bedrijfsgrootte" }
                   -> 83583NED  Banen van werknemers; bedrijfsgrootte en economische activiteit

get_table_info     { "table_id": "83583NED" }
                   -> dimensions: BedrijfstakkenBranchesSBI2008, Bedrijfsgrootte, Perioden
                      measures:   BanenVanWerknemersInDecember_1 [x 1 000]

get_data           { "table_id": "83583NED",
                     "filters": { "Perioden": ["2023JJ00"], "Bedrijfsgrootte": ["T001098"] },
                     "limit": 5 }
```

```
| BedrijfstakkenBranchesSBI2008 | ..._label                         | Perioden | Perioden_label | BanenVanWerknemersInDecember_1 |
| T001081                       | A-U Alle economische activiteiten | 2023JJ00 | 2023 december  | 9020.8                         |
```

## Notes on the data

- **Codes are opaque.** `T001081` is a total, `2023JJ00` is the year 2023, `GM0363` is
  Amsterdam. Look them up with `get_dimension_codes` rather than guessing; `get_data`
  adds a `<dimension>_label` column so results stay readable.
- **Prefix filters.** A code ending in `*` becomes a prefix match. `"Perioden": ["2023*"]`
  takes every 2023 period — yearly (`2023JJ00`), quarterly (`2023KW01`), monthly
  (`2023MM01`) — which is usually what you want for "all of 2023".
- **Tables are large.** Some run to millions of rows, so always filter. Results page with
  `limit` / `offset`, and `has_more` tells you whether another page exists.

### Two CBS quirks this server works around

- `/$count` and `$inlinecount=allpages` are both ignored by these feeds — `/$count`
  returns the table's *total* row count no matter what `$filter` you pass. There is
  therefore no reliable "N rows match" number; the server fetches one row more than
  requested and reports `has_more` instead.
- The `ODataApi` endpoint rejects `$skip` with a 500, so it cannot page. This server uses
  the `ODataFeed` endpoint, which serves the same resources and supports it.

## Layout

| File | Purpose |
| --- | --- |
| `server.py` | FastMCP server and the four tools. Entrypoint `server.py:mcp`. |
| `cbs.py` | Async client for the CBS OData feeds. No MCP dependency. |
| `smoke.py` | End-to-end test of every tool against the live CBS API. |

```bash
python smoke.py
```

The smoke test drives the server through an in-memory FastMCP client and covers paging
boundaries, label resolution, argument validation and error handling. It needs network
access.

## License

MIT. Data is © CBS, licensed CC BY 4.0 — attribute Statistics Netherlands when you
publish figures obtained through this server.
