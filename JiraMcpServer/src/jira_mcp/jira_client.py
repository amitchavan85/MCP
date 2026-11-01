"""Thin async client for the Jira Cloud REST API v3 and Agile API 1.0.

Docs: https://developer.atlassian.com/cloud/jira/platform/rest/v3/
      https://developer.atlassian.com/cloud/jira/software/rest/
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ISSUE_FIELDS = [
    "summary", "status", "issuetype", "priority", "assignee", "reporter",
    "project", "labels", "created", "updated", "duedate", "description",
]


class JiraError(Exception):
    """A Jira failure translated into a message that is safe to show the model."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class JiraNotFound(JiraError):
    pass


class JiraClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            auth=httpx.BasicAuth(email, api_token),  # Jira Cloud: e-mail + API token
            headers={"Accept": "application/json", "User-Agent": "jira-mcp-server/1.0"},
            timeout=timeout,
            transport=transport,
        )
        self._max_retries = max_retries

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ core

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._http.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                if attempt == self._max_retries:
                    raise JiraError("Jira did not respond in time. Try again later.") from exc
                await asyncio.sleep(0.5 * attempt)
                continue
            except httpx.HTTPError as exc:
                raise JiraError("Could not reach Jira.") from exc

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self._max_retries:
                    delay = _retry_after(response, default=0.5 * 2 ** (attempt - 1))
                    logger.warning("Jira %s %s -> %s, retry %d in %.1fs", method, path, response.status_code, attempt, delay)
                    await asyncio.sleep(delay)
                    continue
            return self._handle(response, method, path)
        raise JiraError("Jira request failed after retries.")  # pragma: no cover

    @staticmethod
    def _handle(response: httpx.Response, method: str, path: str) -> Any:
        status = response.status_code
        if status < 400:
            return response.json() if response.content else None

        # Jira error bodies: {"errorMessages": [...], "errors": {"field": "message"}}
        details = ""
        try:
            body = response.json()
            parts = list(body.get("errorMessages") or []) + [f"{k}: {v}" for k, v in (body.get("errors") or {}).items()]
            details = "; ".join(parts)[:500]
        except Exception:  # noqa: BLE001 - body is not JSON
            pass

        logger.info("Jira %s %s -> %s %s", method, path, status, details)
        if status == 404:
            raise JiraNotFound("Not found, or the Jira account has no permission to see it.", status)
        if status == 400:
            raise JiraError(f"Jira rejected the request: {details or 'bad request'}", status)
        if status in (401, 403):
            # Do not echo auth details back to the model.
            raise JiraError("The server's Jira credentials are not permitted to do this.", status)
        if status == 429:
            raise JiraError("Jira rate limit reached. Try again in a minute.", status)
        raise JiraError(f"Jira returned an error (HTTP {status}).", status)

    # ------------------------------------------------------------------ read

    async def myself(self) -> dict[str, Any]:
        return await self._request("GET", "/rest/api/3/myself")

    async def search_issues(self, jql: str, max_results: int = 20, next_page_token: str | None = None) -> dict[str, Any]:
        # Enhanced JQL search (replaces the removed /rest/api/3/search endpoint).
        payload: dict[str, Any] = {"jql": jql, "maxResults": max_results, "fields": ISSUE_FIELDS}
        if next_page_token:
            payload["nextPageToken"] = next_page_token
        return await self._request("POST", "/rest/api/3/search/jql", json=payload)

    async def get_issue(self, key: str) -> dict[str, Any]:
        return await self._request("GET", f"/rest/api/3/issue/{key}", params={"fields": ",".join(ISSUE_FIELDS)})

    async def get_comments(self, key: str, max_results: int = 5) -> dict[str, Any]:
        return await self._request(
            "GET", f"/rest/api/3/issue/{key}/comment", params={"maxResults": max_results, "orderBy": "-created"}
        )

    async def list_projects(self, query: str | None = None, max_results: int = 50) -> dict[str, Any]:
        params: dict[str, Any] = {"maxResults": max_results, "orderBy": "key"}
        if query:
            params["query"] = query
        return await self._request("GET", "/rest/api/3/project/search", params=params)

    async def get_transitions(self, key: str) -> dict[str, Any]:
        return await self._request("GET", f"/rest/api/3/issue/{key}/transitions")

    async def find_users(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        return await self._request("GET", "/rest/api/3/user/search", params={"query": query, "maxResults": max_results})

    async def active_sprints(self, board_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/rest/agile/1.0/board/{board_id}/sprint", params={"state": "active"})

    async def sprint_issues(self, sprint_id: int, max_results: int = 50) -> dict[str, Any]:
        return await self._request(
            "GET", f"/rest/agile/1.0/sprint/{sprint_id}/issue",
            params={"maxResults": max_results, "fields": ",".join(ISSUE_FIELDS)},
        )

    # ------------------------------------------------------------------ write

    async def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/rest/api/3/issue", json={"fields": fields})

    async def add_comment(self, key: str, text: str) -> dict[str, Any]:
        return await self._request("POST", f"/rest/api/3/issue/{key}/comment", json={"body": text_to_adf(text)})

    async def transition_issue(self, key: str, transition_id: str) -> None:
        await self._request("POST", f"/rest/api/3/issue/{key}/transitions", json={"transition": {"id": transition_id}})

    async def assign_issue(self, key: str, account_id: str | None) -> None:
        await self._request("PUT", f"/rest/api/3/issue/{key}/assignee", json={"accountId": account_id})


def _retry_after(response: httpx.Response, default: float) -> float:
    try:
        return min(float(response.headers.get("Retry-After", default)), 30.0)
    except ValueError:
        return default


# ---------------------------------------------------------------------- ADF helpers


def text_to_adf(text: str) -> dict[str, Any]:
    """Plain text -> Atlassian Document Format (one paragraph per blank-line-separated block)."""
    paragraphs = [p for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()] or [text]
    content = []
    for para in paragraphs:
        lines = para.split("\n")
        nodes: list[dict[str, Any]] = []
        for i, line in enumerate(lines):
            if line:
                nodes.append({"type": "text", "text": line})
            if i < len(lines) - 1:
                nodes.append({"type": "hardBreak"})
        content.append({"type": "paragraph", "content": nodes})
    return {"type": "doc", "version": 1, "content": content}


def adf_to_text(node: Any) -> str:
    """Atlassian Document Format -> plain text (good enough for an LLM to read)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return node.get("attrs", {}).get("text", "@user")
    inner = adf_to_text(node.get("content", []))
    if kind in ("paragraph", "heading", "codeBlock", "blockquote"):
        return inner.rstrip() + "\n\n"
    if kind == "listItem":
        return "- " + inner.strip() + "\n"
    return inner
