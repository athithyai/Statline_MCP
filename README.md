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
| `search_tables` | Search the catalog by keyword, in Dutch or English (`language`). |
| `browse_themes` | Walk the CBS subject taxonomy to find tables by topic. Has a full **English** tree. |
| `get_table_info` | Metadata for one table plus its **dimensions** (what you filter on) and **measures** (the numbers). |
| `get_dimension_codes` | The code list of one dimension, with `search` to narrow large ones like regions. |
| `get_data` | Filtered observations, with dimension codes resolved to readable labels. |

The flow is `search_tables` *or* `browse_themes` → `get_table_info` →
`get_dimension_codes` → `get_data`.

Two routes into the catalog because they fail differently: `search_tables` matches title
text and so needs a word in the right language, while `browse_themes` navigates a fixed
topic hierarchy. If one comes up empty, try the other.

## Dutch and English

The catalog is bilingual but lopsided:

| | Tables | Theme nodes |
| --- | --- | --- |
| Dutch (`nl`) | 4,889 | 1,060 |
| English (`en`) | 1,067 | 239 |

A table exists in **one** language only — its title, dimension names and code labels are
all in that language. English tables are a translated subset with identifiers ending
`ENG` (e.g. `85722ENG`), so English gives you less coverage but needs no translation.

Both entry points take a `language` argument:

```python
search_tables(query="population", language="en")   # English titles only
search_tables(query="bevolking",  language="nl")   # Dutch titles only
browse_themes(language="en")                       # English theme tree only
```

`get_table_info` reports each table's language, so you always know which vocabulary the
dimensions and codes will use.

One caveat worth knowing: the language filter constrains the **table's** language, not the
keyword's. An English table can still match a Dutch word when its description quotes a
Dutch source title — `37259eng` does exactly this, citing *'Loop van de bevolking per
gemeente'*. The filter guarantees what you get back, not what matched.

## Finding tables by topic

CBS files its ~6,000 tables under a subject taxonomy of ~1,300 nodes, joined to tables by
a catalog link table. `browse_themes` walks it:

```
browse_themes()                      -> 46 top-level themes across both trees
browse_themes(language="en")         -> just the English roots
browse_themes(search="Population")   -> matching themes, each with its full path
browse_themes(theme_id=1153)         -> sub-themes and the tables filed here
```

Tables sit on the leaves, so a parent theme often lists sub-themes and no tables — keep
descending. The whole taxonomy arrives in one request, so it is fetched once and cached
for the life of the process; paths are computed locally rather than by walking the API.

## Run it locally

```bash
pip install -r requirements.txt
fastmcp run server.py
```

Add it to Claude Code as a local subprocess:

```bash
claude mcp add statline -- fastmcp run /absolute/path/to/MCP_statline/server.py
```

Or serve it over HTTP on `http://localhost:8000/mcp` (override the port with `PORT`):

```bash
python server.py
```

## Deploy

### FastMCP Cloud

The repo is shaped for it — [FastMCP Cloud](https://gofastmcp.com/deployment/fastmcp-cloud)
builds from `requirements.txt` and imports the entrypoint **`server.py:mcp`**. Point it at
this repository and it redeploys on every push to `main`. You get a URL like
`https://<name>.fastmcp.app/mcp`.

### Connecting a client to a deployed server

If you enabled authentication when creating the server, it answers `401` to anonymous
requests. Two ways to connect:

**OAuth** (what FastMCP Cloud sets up for you):

```bash
claude mcp add --transport http statline https://<name>.fastmcp.app/mcp
```

Then authenticate — either `claude mcp login statline` from a shell, or `/mcp` inside an
interactive Claude Code session, which opens the browser sign-in. Check it with
`claude mcp list`; `! Needs authentication` means the sign-in has not completed yet.

**Static bearer token**, for CI or a non-interactive agent:

```bash
claude mcp add --transport http statline https://<name>.fastmcp.app/mcp \
  --header "Authorization: Bearer $TOKEN"
```

To make the server reachable without any of this, turn authentication off in its FastMCP
Cloud settings — it is a per-server toggle, not a paid feature. Since this server is
read-only over public CBS data, the exposure of a public endpoint is bandwidth rather
than data.

### Anywhere else

`Dockerfile` builds a self-contained HTTP server for Render, Fly, Railway or a plain VM:

```bash
docker build -t mcp-statline .
docker run --rm -p 8000:8000 mcp-statline
```

It runs unprivileged and ships a `HEALTHCHECK` that completes a real MCP handshake rather
than just probing the port. FastMCP Cloud does not use this file.

## Example

> How many employee jobs were there in Dutch manufacturing in December 2023?

```
search_tables      { "query": "banen werknemers bedrijfsgrootte", "language": "nl" }
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
| `server.py` | FastMCP server and the five tools. Entrypoint `server.py:mcp`. |
| `cbs.py` | Async client for the CBS OData feeds. No MCP dependency. |
| `smoke.py` | End-to-end test of every tool against the live CBS API. |
| `scripts/health_check.py` | Probe a running server; used by the container healthcheck and CI. |
| `Dockerfile` | HTTP server image for hosts other than FastMCP Cloud. |
| `.github/workflows/test.yml` | Lint, smoke test on three Python versions, container boot test. |

## Development

```bash
python smoke.py                                    # 38 checks against the live CBS API
python scripts/health_check.py --url <server-url>  # probe a running deployment
ruff check . && ruff format --check .              # lint
```

The smoke test drives the server through an in-memory FastMCP client and covers language
filtering, theme navigation, paging boundaries, label resolution, argument validation and
error handling. It needs network access.

CI runs the same suite on push and pull request, and weekly on a schedule — this server
depends on two undocumented quirks of the CBS feeds, so the cron catches upstream drift
rather than waiting for a user to hit it.

## License

MIT. Data is © CBS, licensed CC BY 4.0 — attribute Statistics Netherlands when you
publish figures obtained through this server.
