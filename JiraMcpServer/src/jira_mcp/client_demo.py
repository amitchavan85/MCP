"""Minimal MCP client for the Jira MCP server.

    MCP_URL=http://localhost:8000/mcp MCP_API_KEY=... python -m jira_mcp.client_demo
    python -m jira_mcp.client_demo search "statusCategory != Done ORDER BY updated DESC"
    python -m jira_mcp.client_demo issue ENG-42
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError


def _leaves(exc: BaseException):
    """Flatten exception groups and causes (the client raises anyio ExceptionGroups)."""
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _leaves(inner)
        return
    yield exc
    if exc.__cause__ is not None:
        yield from _leaves(exc.__cause__)
    elif exc.__context__ is not None and not exc.__suppress_context__:
        yield from _leaves(exc.__context__)


def _print(result) -> None:  # noqa: ANN001
    if result.is_error:
        print("TOOL ERROR:", " ".join(getattr(c, "text", "") for c in result.content))
    elif result.structured_content is not None:
        print(json.dumps(result.structured_content, indent=2))
    else:
        print("\n".join(getattr(c, "text", "") for c in result.content))


async def _probe_status(url: str, key: str) -> int | None:
    """The SDK reports HTTP failures as a generic MCPError; ask the endpoint directly for the status code."""
    try:
        async with httpx2.AsyncClient(timeout=10) as probe:
            r = await probe.post(url, json={"jsonrpc": "2.0", "id": 0, "method": "tools/list"}, headers={
                "Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25"})
            return r.status_code
    except Exception:  # noqa: BLE001
        return None


async def run(argv: list[str]) -> int:
    url = os.environ.get("MCP_URL", "http://localhost:8000/mcp")
    key = os.environ.get("MCP_API_KEY")
    if not key:
        print("Set MCP_API_KEY.", file=sys.stderr)
        return 2

    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=30)
    try:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            command = argv[0] if argv else "list"
            if command == "list":
                tools = await client.list_tools()
                resources = await client.list_resources()
                prompts = await client.list_prompts()
                print("Tools:    ", ", ".join(t.name for t in tools.tools))
                print("Resources:", ", ".join(str(r.uri) for r in resources.resources))
                print("Prompts:  ", ", ".join(p.name for p in prompts.prompts))
            elif command == "projects":
                _print(await client.call_tool("list_projects", {}))
            elif command == "search":
                _print(await client.call_tool("search_issues", {"jql": argv[1] if len(argv) > 1 else "", "max_results": 10}))
            elif command == "issue":
                _print(await client.call_tool("get_issue", {"issue_key": argv[1]}))
            elif command == "sprint":
                _print(await client.call_tool("get_active_sprint", {"board_id": int(argv[1])}))
            elif command == "guide":
                res = await client.read_resource("jira://guides/jql")
                print(res.contents[0].text)
            else:
                print(f"Unknown command {command}. Use: list | projects | search <jql> | issue <KEY> | sprint <boardId> | guide")
                return 2
    except Exception as exc:  # noqa: BLE001 - map transport/auth failures to friendly messages
        causes = list(_leaves(exc))
        status = next((c.response.status_code for c in causes if isinstance(c, httpx2.HTTPStatusError)), None)
        if status is None and any(isinstance(c, MCPError) for c in causes):
            status = await _probe_status(url, key)
        if status == 401:
            print("HTTP 401: the server did not accept MCP_API_KEY. Either the key is wrong, or the server has no keys "
                  "loaded (its log then says 'No MCP_API_KEYS configured'; start it from the project folder or fix .env).",
                  file=sys.stderr)
            return 4
        if status == 403:
            print("HTTP 403: the key is valid but lacks the 'jira:read' scope.", file=sys.stderr)
            return 4
        if status in (400, 421):
            print(f"HTTP {status}: host not allowed. Add the host of MCP_URL to MCP_ALLOWED_HOSTS on the server.", file=sys.stderr)
            return 4
        if status == 429:
            print("Rate limited (HTTP 429). Try again in a minute.", file=sys.stderr)
            return 5
        if any(isinstance(c, (httpx2.ConnectError, OSError)) for c in causes):
            print(f"Cannot reach {url}. Is the server running?", file=sys.stderr)
            return 6
        raise
    finally:
        await http.aclose()
    return 0


def main() -> None:
    sys.exit(asyncio.run(run(sys.argv[1:])))


if __name__ == "__main__":
    main()
