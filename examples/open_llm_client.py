"""Drive Statline MCP from an open-weights LLM served behind an OpenAI-compatible API.

Works with anything that speaks /v1/chat/completions and supports tool calling:
vLLM, Ollama, llama.cpp's server, Text Generation Inference, SGLang.

The whole bridge is three steps:

  1. Ask the MCP server for its tools. Each carries a JSON Schema in
     `inputSchema` that is already valid as an OpenAI function `parameters`
     block, so conversion is a rename, not a translation.
  2. Run the normal tool-calling loop: send the tools, let the model emit
     tool_calls, execute each against the MCP server, feed results back.
  3. Stop when the model answers without requesting a tool.

Nothing here is Claude-specific. The MCP server is just an HTTP service that
publishes typed tools.

    # against a local server
    python examples/open_llm_client.py "Hoeveel banen waren er in december 2023?"

    # against a deployed one, with a model behind vLLM
    python examples/open_llm_client.py \
        --mcp https://statline-mcp.example.k8s.nl/mcp \
        --api-base http://localhost:8000/v1 \
        --model Qwen/Qwen3-32B-Instruct \
        "How many births were registered in 2023?"

    # interactive session, history kept between questions
    python examples/open_llm_client.py --chat --model code_assistant

    # no model needed: show the converted schemas and run one tool call
    python examples/open_llm_client.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from typing import Any
from urllib.parse import urlparse

from fastmcp import Client

DEFAULT_MCP = os.environ.get("MCP_STATLINE_URL", "http://127.0.0.1:8000/mcp")
DEFAULT_API_BASE = os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1")
DEFAULT_MODEL = os.environ.get("OPEN_LLM_MODEL", "Qwen/Qwen3-32B-Instruct")

MAX_TURNS = int(os.environ.get("OPEN_LLM_MAX_TURNS", "30"))


_LOOPBACK = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}


def _address(url: str) -> str:
    """host:port, with every spelling of the loopback address normalised."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in _LOOPBACK:
        host = "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{host}:{port}"


def to_openai_tools(mcp_tools: list[Any]) -> list[dict[str, Any]]:
    """MCP tool definitions -> OpenAI `tools` array.

    `inputSchema` is already JSON Schema, so this only re-labels fields. Keep
    the description verbatim: it carries the CBS-specific guidance (codes are
    opaque, Dutch vs English, use browse_themes when search fails) that the
    model needs in order to use these tools correctly.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema,
            },
        }
        for t in mcp_tools
    ]


async def call_tool(client: Client, name: str, arguments: dict[str, Any]) -> str:
    """Execute one MCP tool and return text for the model to read.

    Tool errors are returned as text rather than raised: a model that passed a
    bad dimension name should see the server's correction and retry, which is
    exactly what these tools' error messages are written to enable.
    """
    result = await client.call_tool(name, arguments, raise_on_error=False)
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    if result.is_error:
        return f"TOOL ERROR: {text}"
    return text or json.dumps(result.structured_content or {})[:4000]


# How the model should behave and how the answer should look. Domain knowledge
# (which tool to reach for, that codes are opaque, Dutch vs English coverage)
# deliberately lives in the server's tool descriptions instead, so every client
# gets it. What belongs here is what only this client can decide: grounding
# rules, search discipline, and answer shape.
SYSTEM_PROMPT = """You answer questions about Dutch official statistics using CBS StatLine,
through the provided tools only.

GROUNDING
- Never invent a figure, table identifier, dimension code, measure name or period code.
- Use only identifiers and codes that appeared literally in an earlier tool result.
- If you do not yet know a suitable table, find one with search_tables or browse_themes.
- Answer only from tool results. If the tools cannot answer, say so plainly.

WORKING METHOD
- browse_themes narrows by topic; search_tables matches title keywords. If one returns
  nothing, try the other rather than repeating the same query.
- Do not run the same search twice. If results are poor, use a shorter or broader term.
- get_table_info shows a table's dimensions and measures. get_dimension_codes turns a word
  into the code you need. Call get_data only once the table, codes and measures are known.
- A tool error is information: read what it says, correct the argument, and try again.

ANSWERING
- Be compact. No preamble, no restating the question, no closing summary.
- Report numbers as digits, and apply the measure's unit before answering: a value of 793.3
  with unit "x 1 000" is 793300.
- If one value is asked for, give that value.
- If several values are returned, never drop their labels. Put one per line as
  "label: value", and for a time series as "period: value".
- Name the table identifier you used.
- Reply in the language the question was asked in.
"""


def _new_history(system_prompt: str = SYSTEM_PROMPT) -> list[dict[str, Any]]:
    return [{"role": "system", "content": system_prompt}]


def load_system_prompt(path: str | None) -> str:
    """Read a custom system prompt, or fall back to the built-in one.

    Answer style is use-case specific: an evaluation harness may want bare
    values with no prose, a person at a terminal usually wants a sentence. Swap
    the whole prompt rather than editing the file.
    """
    if not path:
        return SYSTEM_PROMPT
    text = pathlib.Path(path).read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit(f"system prompt file is empty: {path}")
    return text


async def _answer(
    llm, mcp: Client, model: str, tools: list, messages: list, max_turns: int = MAX_TURNS
) -> str:
    """Run the tool-calling loop until the model replies without a tool call.

    `messages` is mutated, so the caller keeps the conversation across turns.
    """
    for _turn in range(max_turns):
        response = await llm.chat.completions.create(
            model=model, messages=messages, tools=tools, tool_choice="auto"
        )
        choice = response.choices[0].message
        messages.append(choice.model_dump(exclude_none=True))

        if not choice.tool_calls:
            return choice.content or "(no answer)"

        for tc in choice.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            print(f"  -> {tc.function.name}({json.dumps(args)[:120]})", file=sys.stderr)
            output = await call_tool(mcp, tc.function.name, args)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": output[:12000]})

    return (
        f"(gave up after {max_turns} tool-calling rounds. Raise it with --max-turns, "
        f"or ask for fewer things at once.)"
    )


def _connect(api_base: str, api_key: str):
    from openai import AsyncOpenAI

    return AsyncOpenAI(base_url=api_base, api_key=api_key)


async def run(
    question: str,
    mcp_url: str,
    api_base: str,
    model: str,
    api_key: str,
    system_prompt: str,
    max_turns: int,
) -> int:
    try:
        llm = _connect(api_base, api_key)
    except ImportError:
        print("pip install openai", file=sys.stderr)
        return 1

    async with Client(mcp_url) as mcp:
        tools = to_openai_tools(await mcp.list_tools())
        messages = _new_history(system_prompt)
        messages.append({"role": "user", "content": question})
        print(await _answer(llm, mcp, model, tools, messages, max_turns))
    return 0


async def chat(
    mcp_url: str,
    api_base: str,
    model: str,
    api_key: str,
    system_prompt: str,
    max_turns: int = MAX_TURNS,
) -> int:
    """Interactive REPL. Keeps the conversation, so follow-up questions work."""
    try:
        llm = _connect(api_base, api_key)
    except ImportError:
        print("pip install openai", file=sys.stderr)
        return 1

    async with Client(mcp_url) as mcp:
        tools = to_openai_tools(await mcp.list_tools())
        messages = _new_history(system_prompt)

        print(f"Statline MCP chat - model {model}, {len(tools)} tools")
        print("Ask in Dutch or English. /reset clears the history, /exit quits.\n")

        while True:
            try:
                # input() blocks, so keep it off the event loop.
                question = (await asyncio.to_thread(input, "you> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0

            if not question:
                continue
            if question.lower() in {"/exit", "/quit", "exit", "quit"}:
                return 0
            if question.lower() == "/reset":
                messages = _new_history()
                print("(history cleared)\n")
                continue

            messages.append({"role": "user", "content": question})
            try:
                answer = await _answer(llm, mcp, model, tools, messages, max_turns)
            except Exception as err:  # noqa: BLE001 - one bad turn must not end the session
                print(f"error: {type(err).__name__}: {err}\n", file=sys.stderr)
                messages.pop()
                continue
            print(f"\n{answer}\n")


async def dry_run(mcp_url: str) -> int:
    """Prove the bridge without a model: convert schemas, run one tool call."""
    async with Client(mcp_url) as mcp:
        tools = to_openai_tools(await mcp.list_tools())
        print(f"{len(tools)} tools converted to OpenAI format:\n")
        for t in tools:
            fn = t["function"]
            params = fn["parameters"].get("properties", {})
            required = set(fn["parameters"].get("required", []))
            args = ", ".join(f"{k}*" if k in required else k for k in params)
            print(f"  {fn['name']}({args})")
        print("\n(* = required)\n")

        print("sample schema, exactly as an OpenAI-compatible server expects it:")
        print(json.dumps(tools[0], indent=2)[:600] + "\n...\n")

        print("executing one tool call through the same path the model would use:")
        out = await call_tool(
            mcp, "search_tables", {"query": "bevolking", "language": "nl", "limit": 2}
        )
        print("\n".join(out.splitlines()[:6]))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?", help="The question to answer.")
    parser.add_argument("--mcp", default=DEFAULT_MCP, help=f"MCP endpoint (default {DEFAULT_MCP})")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="OpenAI-compatible base URL")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name to request")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENAI_API_KEY", "not-needed"),
        help="Most self-hosted servers ignore this.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show converted schemas and run one tool call, without contacting a model.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=MAX_TURNS,
        help=f"Tool-calling rounds before giving up (default {MAX_TURNS}).",
    )
    parser.add_argument(
        "--system-prompt",
        metavar="FILE",
        help="Replace the built-in system prompt with the contents of this file.",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Interactive session: ask follow-up questions, history is kept.",
    )
    args = parser.parse_args()

    if args.dry_run:
        return asyncio.run(dry_run(args.mcp))
    if not args.question and not args.chat:
        parser.error("give a question, or use --chat for an interactive session")

    # Two different services, easily pointed at the same port by accident. The
    # resulting error comes from deep inside the OpenAI client and says nothing
    # useful, so catch it here instead. localhost and 127.0.0.1 are the same
    # address, so compare normalised forms rather than the raw strings.
    if _address(args.mcp) == _address(args.api_base):
        parser.error(
            f"--mcp and --api-base both point at {_address(args.mcp)}. "
            f"These are different services: --mcp is this StatLine server, "
            f"--api-base is your LLM. Give the LLM its own host or port "
            f"(Open WebUI defaults to http://localhost:3000/api)."
        )

    prompt = load_system_prompt(args.system_prompt)
    if args.chat:
        return asyncio.run(
            chat(args.mcp, args.api_base, args.model, args.api_key, prompt, args.max_turns)
        )
    return asyncio.run(
        run(
            args.question,
            args.mcp,
            args.api_base,
            args.model,
            args.api_key,
            prompt,
            args.max_turns,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
