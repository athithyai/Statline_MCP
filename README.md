# mcp-statline

An [MCP](https://modelcontextprotocol.io) server over **CBS StatLine** — the open data
platform of Statistics Netherlands (Centraal Bureau voor de Statistiek). It gives a
language model a reliable path from a plain question to a real, cited statistic: find a
table, read its structure, resolve the codes, pull the observations.

Built with [FastMCP](https://gofastmcp.com). No API key — CBS StatLine open data is free
to use under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.nl).

**Contents**

- [Tools](#tools) · [Quick start](#quick-start)
- [How a question becomes API calls](#how-a-question-becomes-api-calls) — the full request trace
- [The StatLine data model](#the-statline-data-model) — tables, dimensions, measures, codes
- [Themes and the taxonomy](#themes-and-the-taxonomy) — how CBS classifies 6,000 tables
- [The StatLine API surface](#the-statline-api-surface) — endpoints, quirks, workarounds
- [Deployment](#deployment) — FastMCP Cloud, Docker, Kubernetes
- [Using it with an open LLM](#using-it-with-an-open-llm) — vLLM, Ollama, any OpenAI-compatible server

---

## Tools

| Tool | What it does |
| --- | --- |
| `search_tables` | Search the catalog by keyword, in Dutch or English (`language`). |
| `browse_themes` | Walk the CBS subject taxonomy to find tables by topic. Has a full **English** tree. |
| `get_table_info` | Metadata for one table plus its **dimensions** (what you filter on) and **measures** (the numbers). |
| `get_dimension_codes` | The code list of one dimension, with `search` to narrow large ones like regions. |
| `get_data` | Filtered observations, with dimension codes resolved to readable labels. |

The flow is `search_tables` *or* `browse_themes` → `get_table_info` →
`get_dimension_codes` → `get_data`. Each step's output is the next step's input, and every
tool ends its response with a `Next:` line naming the tool to call next.

## Quick start

```bash
pip install -r requirements.txt
fastmcp run server.py                  # stdio
python server.py                       # HTTP on :8000/mcp
python smoke.py                        # 38 checks against the live CBS API
```

```bash
claude mcp add statline -- fastmcp run /absolute/path/to/MCP_statline/server.py
```

---

## How a question becomes API calls

Nothing about the user's question reaches CBS. The model reads the tool descriptions,
chooses a tool, and fills in typed arguments; the server turns those arguments into OData
queries. Here is a complete trace of *"how many employee jobs were there in Dutch
manufacturing in December 2023?"* — **4 tool calls, 9 HTTP requests**:

```
1. search_tables(query="banen werknemers bedrijfsgrootte", language="nl")

   [1] GET /ODataCatalog/Tables
       $filter=(substringof('banen',tolower(Title)) or substringof('banen',tolower(ShortDescription)))
           and (substringof('werknemers',tolower(Title)) or substringof('werknemers',tolower(ShortDescription)))
           and (substringof('bedrijfsgrootte',tolower(Title)) or substringof('bedrijfsgrootte',tolower(ShortDescription)))
           and Language eq 'nl'
       $orderby=Updated desc  $top=3
       -> 83583NED

2. get_table_info(table_id="83583NED")

   [2] GET /ODataFeed/odata/83583NED/TableInfos        ┐ concurrent
   [3] GET /ODataFeed/odata/83583NED/DataProperties    ┘ (asyncio.gather)
       -> dimensions: BedrijfstakkenBranchesSBI2008, Bedrijfsgrootte, Perioden
          measures:   BanenVanWerknemersInDecember_1

3. get_dimension_codes(table_id="83583NED", dimension="Bedrijfsgrootte", search="totaal")

   [4] GET /ODataFeed/odata/83583NED/Bedrijfsgrootte
       $filter=substringof('totaal',tolower(Title))  $top=100
       -> T001098 = "Totaal"

4. get_data(table_id="83583NED", filters={Perioden:["2023JJ00"], Bedrijfsgrootte:["T001098"]})

   [5] GET /ODataFeed/odata/83583NED/DataProperties        (validate argument names)
   [6] GET /ODataFeed/odata/83583NED/TypedDataSet
       $filter=Perioden eq '2023JJ00' and Bedrijfsgrootte eq 'T001098'  $top=3
   [7] GET /ODataFeed/odata/83583NED/BedrijfstakkenBranchesSBI2008  $top=10000  ┐
   [8] GET /ODataFeed/odata/83583NED/Bedrijfsgrootte               $top=10000  ├ label lookups,
   [9] GET /ODataFeed/odata/83583NED/Perioden                      $top=10000  ┘ concurrent
```

### Keywords to `$filter`

Each search word becomes one clause matching **title or description**, and the clauses are
joined with `and` — so every word must appear somewhere, though not in the same field:

```python
words   = [w for w in terms.strip().lower().split() if w]
clauses = [f"(substringof({lit(w)},tolower(Title)) or "
           f"substringof({lit(w)},tolower(ShortDescription)))" for w in words]
filter_ = " and ".join(clauses)
```

Every user-supplied value passes through `odata_literal()` first. OData string literals are
single-quoted and escape an inner quote by doubling it; without this, a search for `men's`
would terminate the literal early and produce a malformed filter — the OData equivalent of
an injection bug.

```python
def odata_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
```

### Filters to observations

`get_data` compiles its `filters` dict into one `$filter`. Codes for the same dimension are
`or`-ed, different dimensions are `and`-ed, and a trailing `*` becomes a prefix match:

```python
{"Perioden": ["2023*"], "Bedrijfsgrootte": ["T001098", "T001099"]}
```
```sql
startswith(Perioden,'2023') and (Bedrijfsgrootte eq 'T001098' or Bedrijfsgrootte eq 'T001099')
```

Prefix matching exists because StatLine period codes are structured (`2023JJ00` yearly,
`2023KW01` quarterly, `2023MM01` monthly), so `2023*` means "everything in 2023" regardless
of frequency.

### Labels

Requests `[7]`–`[9]` are the `labels=True` cost: one code-list fetch per dimension, issued
concurrently, building `code -> title` maps so `T001098` is rendered next to `Totaal`. The
label column is inserted directly after the code it explains, not appended at the end, so
the pairing survives being read as text. Pass `labels=false` to skip all three requests.

### Cost profile

Of the 9 requests, 4 are metadata that a warm cache would eliminate — `DataProperties` is
fetched at both `[3]` and `[5]`, and the code lists at `[7]`–`[9]` are re-fetched on every
`get_data`. Only the themes tree is currently cached. Per-table metadata caching is a known
improvement, not yet implemented.

---

## The StatLine data model

A StatLine **table** is a hypercube, stored long-format. Understanding four terms is enough
to use every tool here.

| Term | What it is | Example |
| --- | --- | --- |
| **Table** | One dataset, identified by a code | `83583NED` |
| **Dimension** | An axis you filter on | `Perioden`, `Bedrijfsgrootte` |
| **Code** | One allowed value of a dimension | `2023JJ00`, `T001098` |
| **Measure** (*Topic*) | A column holding actual numbers | `BanenVanWerknemersInDecember_1` |

A row of `TypedDataSet` carries one code per dimension plus one value per measure:

```json
{ "ID": 13,
  "BedrijfstakkenBranchesSBI2008": "T001081",
  "Bedrijfsgrootte": "T001098",
  "Perioden": "2023JJ00",
  "BanenVanWerknemersInDecember_1": 9020.8 }
```

**Codes are opaque and must be looked up.** They are not human-readable and cannot be
guessed: `T001081` is a total, `2023JJ00` is the year 2023, `GM0363` is Amsterdam. Each
dimension publishes its own code list as a separate entity set named after the dimension.

`DataProperties` describes the columns. Its `Type` field distinguishes them:

| `Type` | Meaning |
| --- | --- |
| `Dimension` | An ordinary axis |
| `TimeDimension` | The time axis (usually `Perioden`) |
| `GeoDimension` / `GeoDetail` | A geography axis |
| `Topic` | A measure — an actual number, with `Unit` and `Decimals` |
| `TopicGroup` | A heading grouping measures; not a column |

`TypedDataSet` returns numbers as JSON numbers; `UntypedDataSet` returns everything as
strings, preserving CBS's original formatting. This server uses `TypedDataSet`.

### Scale

| | Count |
| --- | --- |
| Tables | 5,956 |
| Observations across all tables | 4,108,824,238 |
| Theme nodes | 1,299 |
| Table↔theme links | 8,122 |

The catalog is bilingual but lopsided — **4,889 Dutch tables and 1,067 English**, the
English set being a translated subset with identifiers ending `ENG`. A table exists in
**one** language only: its title, dimension names and code labels are all in that language.
Hence the `language` argument on both discovery tools.

One caveat: the filter constrains the **table's** language, not the keyword's. An English
table can still match a Dutch word when its description quotes a Dutch source — `37259eng`
does exactly this, citing *'Loop van de bevolking per gemeente'*.

---

## Themes and the taxonomy

CBS classifies its tables in a subject hierarchy — the same one behind the StatLine website's
navigation — published as ordinary OData and joined to tables by a link table.

```
ODataCatalog/Themes          1,299 nodes: ID, ParentID, Number, Title, Language
ODataCatalog/Tables_Themes   8,122 links: TableIdentifier <-> ThemeID
```

It is **two parallel trees**, not one translated tree:

| | Theme nodes | Leads to |
| --- | --- | --- |
| Dutch (`nl`) | 1,060 | Dutch tables |
| English (`en`) | 239 | English (`ENG`) tables |

Structure is expressed purely by `ParentID`, so the tree is rebuilt client-side. Since all
1,299 nodes arrive in a single request, the server fetches them once, caches them for the
process, and computes paths in memory:

```python
by_id = {t["ID"]: t for t in themes}
while node is not None and node["ID"] not in seen:   # `seen` guards a cyclic ParentID
    path.append(node)
    node = by_id.get(node.get("ParentID"))
return list(reversed(path))
```

An `asyncio.Lock` guards the cache: without it, concurrent requests against a cold cache
would each fire their own fetch.

A resolved path looks like this, and 8,122 links for 5,956 tables means many tables are
filed under several themes:

```
Arbeid en sociale zekerheid > Arbeid en arbeidsmarkt > Banen, vacatures, werkgelegenheid > Banen
  -> 83583NED, 83582NED, 86205NED, 84826NED, ...
```

**Tables sit on leaves.** A parent theme typically lists sub-themes and no tables, so
`browse_themes` is used by descending. Fetching a theme's tables takes two hops — the join
table gives identifiers, then one batched `or`-chain resolves them to titles in a single
request rather than N.

### Why both discovery tools exist

They fail in opposite directions:

| | `search_tables` | `browse_themes` |
| --- | --- | --- |
| Finds | specifically named tables | topic areas |
| Needs | the right word, right language | only a rough topic |
| English reach | ~1,100 of 5,956 tables | full parallel tree |
| Fails when | your vocabulary is wrong | your term is too specific |

Searching themes for `"Births"` returns nothing — that is a *table* name, not a topic
(`Population development` is the theme). Conversely an English question can navigate the
English tree to real data with nothing translated. When one route is empty, the other is
the intended recovery, and the server's instructions say so.

---

## The StatLine API surface

Two hosts, three base paths:

| Base | Purpose |
| --- | --- |
| `opendata.cbs.nl/ODataCatalog/` | Catalog: `Tables`, `Themes`, `Tables_Themes`, `Featured` |
| `opendata.cbs.nl/ODataFeed/odata/{id}/` | One table's data and metadata |
| `opendata.cbs.nl/ODataApi/odata/{id}/` | Same resources — **but cannot page** (see below) |

Per-table entity sets:

| Resource | Contents |
| --- | --- |
| `TableInfos` | Title, period, frequency, language, status, source |
| `DataProperties` | Dimensions and measures, with units and descriptions |
| `TypedDataSet` | Observations, numbers typed |
| `UntypedDataSet` | Observations, all strings |
| `CategoryGroups` | Groupings within dimensions |
| `{DimensionName}` | That dimension's code list |

This is OData **v3**, so filters use `substringof(...)`, `startswith(...)`, `tolower(...)`
and `eq` — not the v4 `contains(...)`. Always pass `$format=json`. CBS also runs a newer v4
service at `datasets.cbs.nl/odata/v1/CBS/{id}` with a long-format `Observations` set; this
server uses v3 because `TypedDataSet` returns wide rows that are far easier for a model to
read.

### Three quirks this server works around

**1. `$count` ignores `$filter`.** `/TypedDataSet/$count` returns the table's *total* row
count no matter what filter you pass, and `$inlinecount=allpages` is silently dropped from
the response. There is therefore no way to ask "how many rows match?". Reporting that
number would mean reporting a wrong one — so `get_data` requests one row more than asked
for and reports `has_more` instead.

**2. `ODataApi` rejects `$skip`.** It returns HTTP 500 with *"The 'Skip' query option is not
supported on the ODataApi… Please use ODataFeed"*. So the server uses `ODataFeed`
throughout, which serves the same resources and pages correctly.

**3. Codes are space-padded.** Dimension values come back fixed-width — `"300035 "` — while
code-list keys are not padded. Comparing them naively fails, so `get_data` trims every
dimension value before returning rows.

### A note for probes

The MCP endpoint cannot double as an HTTP health check: a bare `GET /mcp` returns **406**,
because Streamable HTTP requires specific `Accept` headers. `server.py` therefore exposes a
plain `GET /health` returning 200 for load balancers and Kubernetes.

---

## Deployment

### FastMCP Cloud

Builds from `requirements.txt` and imports entrypoint **`server.py:mcp`**; redeploys on
every push to `main`. You get `https://<name>.fastmcp.app/mcp`.

If you enabled authentication at setup, the endpoint answers `401` to anonymous requests.
Connect with OAuth:

```bash
claude mcp add --transport http statline https://<name>.fastmcp.app/mcp
```

then complete sign-in with `/mcp` inside an interactive Claude Code session. For CI or a
non-interactive agent, use a static header instead:

```bash
claude mcp add --transport http statline https://<name>.fastmcp.app/mcp \
  --header "Authorization: Bearer $TOKEN"
```

Authentication is a per-server toggle, not a paid feature — turn it off if you want the
endpoint open. This server is read-only over public data, so the exposure is bandwidth
rather than data.

### Docker

```bash
docker build -t statline-mcp .
docker run --rm -p 8000:8000 statline-mcp
```

Runs unprivileged as uid 10001 with a read-only root filesystem. Its `HEALTHCHECK`
completes a real MCP handshake rather than probing the port.

### Kubernetes

`deploy/k8s/` holds a Deployment, Service and Ingress for the
GitHub → registry → cluster pattern:

```bash
kubectl kustomize deploy/k8s | kubectl apply -f -
```

The server ends up at `https://<host>/mcp`. Points worth knowing:

- **Probes hit `/health`, not `/mcp`** — the latter returns 406 to a plain GET, so an
  `httpGet` probe against it would fail permanently.
- **`/health` does not call CBS** on purpose: a probe reports whether *this process* is
  serving, and a slow upstream should not restart your pods. For a deeper gate, use
  `scripts/health_check.py` as a `startupProbe` — it queries through to CBS.
- **Raise the ingress read timeout.** Streamable HTTP holds responses open; the default
  60s will cut long tool calls off mid-stream. The manifest sets 300s and disables
  proxy buffering for ingress-nginx.
- **No CPU limit.** This workload is IO-bound on CBS; a CPU limit only adds throttling.

`.github/workflows/release.yml` builds and pushes the image to GHCR on every push to
`main`, then boots it and probes `/health` before considering the release good.

---

## Using it with an open LLM

Nothing here is Claude-specific. The server is an HTTP service publishing typed tools, so
any model that supports tool calling can drive it — Qwen, Llama, Mistral, DeepSeek — served
by vLLM, Ollama, llama.cpp, TGI or SGLang.

The bridge is genuinely thin, because an MCP tool's `inputSchema` **is already** valid JSON
Schema and drops straight into an OpenAI `function.parameters` block:

```python
def to_openai_tools(mcp_tools):
    return [{"type": "function",
             "function": {"name": t.name,
                          "description": t.description or "",
                          "parameters": t.inputSchema}}
            for t in mcp_tools]
```

`examples/open_llm_client.py` is a complete working loop: it connects to the MCP server,
converts the tools, runs the tool-calling cycle against any OpenAI-compatible endpoint, and
feeds results back until the model answers.

```bash
# see the converted schemas and run one real tool call - no model required
python examples/open_llm_client.py --dry-run

# against a self-hosted model and a deployed server
python examples/open_llm_client.py \
  --mcp https://statline-mcp.example.k8s.nl/mcp \
  --api-base http://localhost:8000/v1 \
  --model Qwen/Qwen3-32B-Instruct \
  "How many births were registered in 2023?"
```

Four things matter when driving this from an open model:

1. **Keep the tool descriptions verbatim.** They carry the operational knowledge — codes are
   opaque, Dutch vs English coverage, use `browse_themes` when search fails. Truncating them
   to save context is what makes a model start inventing table identifiers.
2. **Return tool errors as text, not exceptions.** The error messages name the valid
   dimensions and measures, so a model that guessed wrong can correct itself on the next
   turn. `call_tool()` returns `TOOL ERROR: ...` rather than raising.
3. **Pin a system prompt against fabrication.** Small models will happily invent a
   plausible `8xxxxNED` identifier. Require every figure to come from a tool result and
   require the table id to be stated.
4. **Cap the loop.** The example stops after 12 turns rather than looping forever if the
   model never commits to an answer.

Anything speaking MCP works too, without this bridge — LangChain, LlamaIndex, Continue,
Cursor, and other MCP clients connect to the same URL.

---

## Layout

| Path | Purpose |
| --- | --- |
| `server.py` | FastMCP server, five tools, `/health`. Entrypoint `server.py:mcp`. |
| `cbs.py` | Async client for the CBS OData feeds. No MCP dependency. |
| `smoke.py` | 38-check end-to-end test against the live CBS API. |
| `scripts/health_check.py` | Probe a running server, optionally through to CBS. |
| `examples/open_llm_client.py` | Tool-calling loop for any OpenAI-compatible model. |
| `Dockerfile` | HTTP server image for self-hosting. |
| `deploy/k8s/` | Deployment, Service, Ingress, kustomization. |
| `.github/workflows/` | `test.yml` (lint, smoke, container boot), `release.yml` (image). |

## Development

```bash
python smoke.py                                     # end-to-end against live CBS
python scripts/health_check.py --url <server-url>   # probe a deployment
ruff check . && ruff format --check .               # lint
```

CI runs the suite on push and pull request, and weekly on a schedule — this server depends
on undocumented quirks of the CBS feeds, so the cron catches upstream drift rather than
waiting for a user to hit it.

## License

MIT. Data is © CBS, licensed CC BY 4.0 — attribute Statistics Netherlands when you publish
figures obtained through this server.
