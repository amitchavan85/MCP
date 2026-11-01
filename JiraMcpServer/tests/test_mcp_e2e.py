"""End-to-end: real MCP client -> HTTP -> FastAPI -> MCP server -> fake Jira."""

import httpx
import pytest

from .conftest import NOSCOPE_KEY, READER_KEY, WRITER_KEY

# ------------------------------------------------------------------ discovery & registration


async def test_lists_tools_resources_and_prompts(server):
    async with server.client(READER_KEY) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        resources = {str(r.uri) for r in (await c.list_resources()).resources}
        templates = {t.uri_template for t in (await c.list_resource_templates()).resource_templates}
        prompts = {p.name for p in (await c.list_prompts()).prompts}

    assert {"search_issues", "get_issue", "list_projects", "get_transitions", "find_users", "get_active_sprint",
            "create_issue", "add_comment", "transition_issue", "assign_issue"} <= tools.keys()
    assert tools["get_issue"].annotations.read_only_hint is True
    assert tools["create_issue"].annotations.read_only_hint is False
    assert tools["get_issue"].output_schema is not None
    assert {"jira://projects", "jira://guides/jql"} <= resources
    assert "jira://issue/{issue_key}" in templates
    assert {"bug_triage", "sprint_report"} <= prompts


async def test_health_checks_jira_connectivity(server):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{server.url}/health")
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "healthy"
    assert payload["jira"]["status"] == "healthy"
    assert server.jira.calls[-1] == ("GET", "/rest/api/3/myself")


async def test_health_reports_unhealthy_when_jira_is_down(server):
    server.jira.fail_next = 503
    server.jira.fail_next = 503
    server.jira.fail_next = 503
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{server.url}/health")
    assert r.status_code == 503
    payload = r.json()
    assert payload["status"] == "unhealthy"
    assert payload["jira"]["status"] == "unhealthy"


# ------------------------------------------------------------------ read tools


async def test_get_issue_returns_structured_detail_without_emails(server):
    async with server.client(READER_KEY) as c:
        r = await c.call_tool("get_issue", {"issue_key": "eng-1"})
    assert not r.is_error
    issue = r.structured_content
    assert issue["key"] == "ENG-1" and issue["status"] == "To Do"
    assert issue["recent_comments"][0]["body"] == "Seen on Safari too."
    assert "@example.com" not in str(issue)                      # PII minimisation
    assert any("prompt-injection" in w for w in issue["content_warnings"])


async def test_search_is_scoped_to_allowed_projects(server):
    async with server.client(READER_KEY) as c:
        r = await c.call_tool("search_issues", {"jql": "statusCategory != Done ORDER BY updated DESC"})
    result = r.structured_content
    assert result["jql"].startswith("project in (ENG, OPS) AND (")
    assert {i["key"] for i in result["issues"]} == {"ENG-1", "ENG-2", "OPS-7"}


async def test_search_post_filters_even_if_jira_returns_foreign_issues(server):
    async with server.client(READER_KEY) as c:
        # Balanced parentheses, so it passes validation; the fake Jira then "leaks" SECRET-1.
        r = await c.call_tool("search_issues", {"jql": "project = SECRET OR status = Open"})
    assert "SECRET-1" not in {i["key"] for i in r.structured_content["issues"]}


async def test_jql_breakout_is_a_tool_error(server):
    async with server.client(READER_KEY) as c:
        r = await c.call_tool("search_issues", {"jql": "status = Open) OR (project = SECRET"})
    assert r.is_error and "unbalanced" in r.content[0].text


async def test_disallowed_project_is_indistinguishable_from_missing(server):
    async with server.client(READER_KEY) as c:
        hidden = await c.call_tool("get_issue", {"issue_key": "SECRET-1"})
        projects = await c.call_tool("list_projects", {})
    assert hidden.is_error and "not found" in hidden.content[0].text
    keys = {p["key"] for p in projects.structured_content["result"]}
    assert keys == {"ENG", "OPS"}


async def test_missing_issue_is_a_tool_error(server):
    async with server.client(READER_KEY) as c:
        r = await c.call_tool("get_issue", {"issue_key": "ENG-999"})
    assert r.is_error and "Not found" in r.content[0].text


async def test_schema_validation_rejects_bad_arguments(server):
    async with server.client(READER_KEY) as c:
        r = await c.call_tool("search_issues", {"jql": "x", "max_results": 500})
    assert r.is_error


async def test_find_users_hides_emails_and_bots(server):
    async with server.client(READER_KEY) as c:
        r = await c.call_tool("find_users", {"query": "ali"})
    users = r.structured_content["result"]
    assert [u["display_name"] for u in users] == ["Alice Johnson"]
    assert "email" not in str(users).lower()


async def test_active_sprint_groups_by_status_and_filters_projects(server):
    async with server.client(READER_KEY) as c:
        r = await c.call_tool("get_active_sprint", {"board_id": 12})
        empty = await c.call_tool("get_active_sprint", {"board_id": 99})
    report = r.structured_content
    assert report["sprint"]["name"] == "Sprint 42"
    assert report["total_issues"] == 2                         # SECRET-1 filtered out
    assert set(report["issues_by_status"]) == {"To Do", "In Progress"}
    assert empty.structured_content["sprint"] is None


# ------------------------------------------------------------------ write tools & authorization


async def test_reader_cannot_write(server):
    async with server.client(READER_KEY) as c:
        r = await c.call_tool("add_comment", {"issue_key": "ENG-1", "body": "hi"})
    assert r.is_error and "jira:write" in r.content[0].text


async def test_writer_can_create_comment_transition_and_assign(server):
    async with server.client(WRITER_KEY) as c:
        created = await c.call_tool("create_issue", {
            "project_key": "ENG", "summary": "Crash on startup", "issue_type": "Bug",
            "description": "Steps:\n1. open app\n\nExpected: no crash", "labels": ["mobile"],
        })
        assert not created.is_error, created.content
        key = created.structured_content["key"]

        assert not (await c.call_tool("add_comment", {"issue_key": key, "body": "Investigating"})).is_error
        moved = await c.call_tool("transition_issue", {"issue_key": key, "transition": "in progress"})
        assert moved.structured_content["message"] == "Moved to 'In Progress'."
        assert not (await c.call_tool("assign_issue", {"issue_key": key, "account_id": "5b10ac8d82e05b22cc7d4ef5"})).is_error

    assert server.jira.issues[key]["fields"]["status"]["name"] == "In Progress"


async def test_unknown_transition_lists_alternatives(server):
    async with server.client(WRITER_KEY) as c:
        r = await c.call_tool("transition_issue", {"issue_key": "ENG-2", "transition": "Deploy to prod"})
    assert r.is_error and "Available: To Do (11)" in r.content[0].text


async def test_writer_cannot_write_outside_allowed_projects(server):
    async with server.client(WRITER_KEY) as c:
        r = await c.call_tool("create_issue", {"project_key": "SECRET", "summary": "x"})
    assert r.is_error


async def test_invalid_labels_rejected(server):
    async with server.client(WRITER_KEY) as c:
        r = await c.call_tool("create_issue", {"project_key": "ENG", "summary": "x", "labels": ["has space"]})
    assert r.is_error and "Labels cannot contain spaces" in r.content[0].text


async def test_read_only_mode_blocks_writes(readonly_server):
    async with readonly_server.client(WRITER_KEY) as c:
        r = await c.call_tool("create_issue", {"project_key": "ENG", "summary": "x"})
        ok = await c.call_tool("get_issue", {"issue_key": "ENG-2"})
    assert r.is_error and "read-only" in r.content[0].text
    assert not ok.is_error


# ------------------------------------------------------------------ resources & prompts


async def test_resources(server):
    async with server.client(READER_KEY) as c:
        guide = await c.read_resource("jira://guides/jql")
        issue = await c.read_resource("jira://issue/ENG-1")
        projects = await c.read_resource("jira://projects")
    assert "JQL quick reference" in guide.contents[0].text
    assert issue.contents[0].text.startswith("# ENG-1: Login times out")
    assert "untrusted user content" in issue.contents[0].text
    assert '"SECRET"' not in projects.contents[0].text


async def test_prompts(server):
    async with server.client(READER_KEY) as c:
        triage = await c.get_prompt("bug_triage", {"issue_key": "eng-1"})
        report = await c.get_prompt("sprint_report", {"board_id": "12", "audience": "management"})
    assert "Triage Jira bug ENG-1" in triage.messages[0].content.text
    assert "untrusted" in triage.messages[0].content.text
    assert "risk-focused" in report.messages[0].content.text


# ------------------------------------------------------------------ authentication & HTTP hardening


def test_health_is_public(server):
    assert httpx.get(f"{server.url}/health").json()["status"] == "healthy"


def test_missing_token_gets_401_with_resource_metadata(server):
    r = httpx.post(f"{server.url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")


def test_wrong_token_gets_401(server):
    r = httpx.post(f"{server.url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Authorization": "Bearer nope", "Accept": "application/json, text/event-stream"})
    assert r.status_code == 401


def test_token_without_read_scope_gets_403(server):
    r = httpx.post(f"{server.url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Authorization": f"Bearer {NOSCOPE_KEY}", "Accept": "application/json, text/event-stream"})
    assert r.status_code == 403


def test_protected_resource_metadata_published(server):
    r = httpx.get(f"{server.url}/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    assert "jira:read" in r.json().get("scopes_supported", [])


def test_dns_rebinding_protection_rejects_foreign_host(server):
    r = httpx.post(f"{server.url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Authorization": f"Bearer {READER_KEY}", "Host": "evil.example",
                            "Accept": "application/json, text/event-stream"})
    assert r.status_code in (400, 403, 421)


def test_rate_limit():
    from .conftest import RunningServer

    s = RunningServer(rate_limit_per_minute=3).start()
    try:
        codes = [httpx.post(f"{s.url}/mcp", json={}, headers={"Authorization": "Bearer nope"}).status_code for _ in range(5)]
    finally:
        s.stop()
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]
