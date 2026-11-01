import httpx
import pytest

from jira_mcp.jira_client import JiraClient, JiraError, JiraNotFound, adf_to_text, text_to_adf

from .fake_jira import FakeJira


def make_client(fake: FakeJira) -> JiraClient:
    return JiraClient("https://example.atlassian.net", "bot@example.com", "secret-token", transport=fake.transport())


async def test_get_issue_uses_basic_auth_and_v3_api():
    fake = FakeJira()
    client = make_client(fake)
    issue = await client.get_issue("ENG-1")
    assert issue["key"] == "ENG-1"
    assert fake.calls[-1] == ("GET", "/rest/api/3/issue/ENG-1")
    await client.aclose()


async def test_search_uses_enhanced_jql_endpoint():
    fake = FakeJira()
    client = make_client(fake)
    await client.search_issues("project in (ENG) AND (status = Open)")
    assert fake.calls[-1] == ("POST", "/rest/api/3/search/jql")
    await client.aclose()


async def test_404_maps_to_not_found():
    client = make_client(FakeJira())
    with pytest.raises(JiraNotFound):
        await client.get_issue("ENG-999")
    await client.aclose()


async def test_400_surfaces_field_errors():
    client = make_client(FakeJira())
    with pytest.raises(JiraError, match="summary"):
        await client.create_issue({"project": {"key": "ENG"}, "summary": "", "issuetype": {"name": "Task"}})
    await client.aclose()


@pytest.mark.parametrize("status", [429, 503])
async def test_transient_errors_are_retried(status):
    fake = FakeJira()
    fake.fail_next = status
    client = make_client(fake)
    assert (await client.get_issue("ENG-1"))["key"] == "ENG-1"
    assert len(fake.calls) == 2
    await client.aclose()


async def test_auth_failure_does_not_leak_details():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errorMessages": ["Token secret-token invalid for bot@example.com"]})

    client = JiraClient("https://x.atlassian.net", "bot@example.com", "secret-token", transport=httpx.MockTransport(handler))
    with pytest.raises(JiraError) as info:
        await client.myself()
    assert "secret-token" not in str(info.value) and "bot@example.com" not in str(info.value)
    await client.aclose()


def test_adf_round_trip():
    doc = text_to_adf("First line\nsecond line\n\nNew paragraph")
    assert doc["type"] == "doc" and len(doc["content"]) == 2
    assert adf_to_text(doc).strip() == "First line\nsecond line\n\nNew paragraph"


def test_adf_to_text_handles_lists_and_mentions():
    doc = {"type": "doc", "content": [{"type": "bulletList", "content": [
        {"type": "listItem", "content": [{"type": "paragraph", "content": [
            {"type": "mention", "attrs": {"text": "@Alice"}}, {"type": "text", "text": " please check"}]}]}]}]}
    assert adf_to_text(doc).strip() == "- @Alice please check"
