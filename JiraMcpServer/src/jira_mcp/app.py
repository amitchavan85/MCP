"""FastAPI host for the Jira MCP server.

    uvicorn jira_mcp.app:app --port 8000          (or: jira-mcp-server)

Routes
    POST /mcp                                      MCP Streamable HTTP endpoint (Bearer auth)
    GET  /.well-known/oauth-protected-resource/mcp OAuth protected-resource metadata (from the SDK)
    GET  /health                                   liveness, no auth
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .config import Settings, get_settings, loaded_env_files
from .jira_client import JiraClient, JiraError
from .logging_config import configure_logging
from .security import RateLimitMiddleware
from .server import build_mcp_server

logger = logging.getLogger("jira_mcp")


def create_app(settings: Settings | None = None, jira_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or get_settings()

    jira = JiraClient(
        settings.jira_base_url,
        settings.jira_email,
        settings.jira_api_token.get_secret_value(),
        timeout=settings.jira_timeout_seconds,
        transport=jira_transport,
    )
    mcp = build_mcp_server(settings, jira)
    configure_logging(settings.log_level, settings.log_json)  # after MCPServer, which sets up its own logging

    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,   # no sticky sessions: scale horizontally
        json_response=True,    # plain JSON responses (no SSE needed for these tools)
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.mcp_allowed_hosts,
            allowed_origins=settings.mcp_allowed_origins,
        ),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        env_files = loaded_env_files()
        logger.info("Configuration files: %s", ", ".join(env_files) or "none found (using environment variables only)")
        if settings.mcp_api_keys:
            logger.info("Loaded %d MCP API key(s): %s", len(settings.mcp_api_keys), ", ".join(k.name for k in settings.mcp_api_keys))
        else:
            logger.warning("No MCP_API_KEYS configured: every MCP request will be rejected with 401. "
                           "Create .env in the project folder (copy .env.example).")
        if not settings.jira_email or not settings.jira_api_token.get_secret_value():
            logger.warning("JIRA_EMAIL / JIRA_API_TOKEN not set: Jira calls will fail.")
        logger.info(
            "Jira MCP server %s starting: jira=%s read_only=%s projects=%s",
            __version__, settings.jira_base_url, settings.read_only, settings.jira_allowed_projects or "ALL",
        )
        # Mounted sub-app lifespans don't run under FastAPI, so start the MCP session manager here.
        async with mcp.session_manager.run():
            yield
        await jira.aclose()

    app = FastAPI(title="Jira MCP Server", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(RateLimitMiddleware, limit_per_minute=settings.rate_limit_per_minute)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        payload = {"status": "healthy", "version": __version__, "read_only": settings.read_only, "jira": {"status": "healthy"}}

        if not settings.jira_email or not settings.jira_api_token.get_secret_value():
            payload["status"] = "unhealthy"
            payload["jira"] = {"status": "unhealthy", "error": "JIRA_EMAIL / JIRA_API_TOKEN are not configured."}
            return JSONResponse(status_code=503, content=payload)

        try:
            await jira.myself()
        except JiraError as exc:
            payload["status"] = "unhealthy"
            payload["jira"] = {"status": "unhealthy", "error": str(exc)}
            return JSONResponse(status_code=503, content=payload)

        return payload

    # Mount last so FastAPI routes win; the MCP app serves /mcp and /.well-known/*.
    app.mount("/", mcp_app)
    app.state.mcp = mcp
    return app


def main() -> None:
    settings = get_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_config=None, proxy_headers=True)


# Module-level app for `uvicorn jira_mcp.app:app`; created lazily so tests can import the module freely.
def __getattr__(name: str):  # noqa: ANN202
    if name == "app":
        return create_app()
    raise AttributeError(name)


if __name__ == "__main__":
    main()
