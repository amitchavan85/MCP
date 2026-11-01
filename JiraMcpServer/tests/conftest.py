"""Fixtures: a real uvicorn server (in a thread) backed by FakeJira, and a real MCP client."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import asynccontextmanager

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from jira_mcp.app import create_app
from jira_mcp.config import ApiKeyEntry, Settings
from jira_mcp.security import hash_key

from .fake_jira import FakeJira

READER_KEY = "reader-key-123"
WRITER_KEY = "writer-key-456"
NOSCOPE_KEY = "noscope-key-789"


def make_settings(port: int, **overrides) -> Settings:
    values = dict(
        jira_base_url="https://example.atlassian.net",
        jira_email="bot@example.com",
        jira_api_token="token",
        jira_allowed_projects=["ENG", "OPS"],
        read_only=False,
        rate_limit_per_minute=1000,
        mcp_api_keys=[
            ApiKeyEntry(name="reader", key_sha256=hash_key(READER_KEY), scopes=["jira:read"]),
            ApiKeyEntry(name="writer", key_sha256=hash_key(WRITER_KEY), scopes=["jira:read", "jira:write"]),
            ApiKeyEntry(name="noscope", key_sha256=hash_key(NOSCOPE_KEY), scopes=[]),
        ],
        mcp_issuer_url=f"http://127.0.0.1:{port}",
        mcp_resource_url=f"http://127.0.0.1:{port}/mcp",
        mcp_allowed_hosts=["127.0.0.1:*", "localhost:*"],
        mcp_allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RunningServer:
    def __init__(self, **overrides) -> None:
        self.port = _free_port()
        self.jira = FakeJira()
        self.settings = make_settings(self.port, **overrides)
        app = create_app(self.settings, jira_transport=self.jira.transport())
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", log_config=None))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> RunningServer:
        self._thread.start()
        deadline = time.time() + 15
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)

    @asynccontextmanager
    async def client(self, key: str | None):
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        http = httpx2.AsyncClient(headers=headers, timeout=15)
        try:
            async with Client(streamable_http_client(f"{self.url}/mcp", http_client=http)) as c:
                yield c
        finally:
            await http.aclose()


@pytest.fixture(scope="session")
def server():
    s = RunningServer().start()
    yield s
    s.stop()


@pytest.fixture(scope="session")
def readonly_server():
    s = RunningServer(read_only=True).start()
    yield s
    s.stop()
