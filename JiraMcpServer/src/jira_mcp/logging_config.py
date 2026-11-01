"""Structured logging. JSON lines in containers, readable text locally."""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

AUDIT = logging.getLogger("jira_mcp.audit")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _RenameUvicornError(logging.Filter):
    """uvicorn logs normal startup messages under the logger name 'uvicorn.error', which looks alarming."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.error":
            record.name = "uvicorn"
        return True


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_RenameUvicornError())
    handler.setFormatter(JsonFormatter() if json_logs else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # they log full URLs at INFO


@contextmanager
def audit(action: str, client: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Audit one MCP operation: who, what, outcome, duration. Pass identifiers, never payloads."""
    record: dict[str, Any] = {"action": action, "client": client, **fields}
    start = time.perf_counter()
    try:
        yield record
        record.setdefault("outcome", "ok")
    except Exception as exc:
        record["outcome"] = "error"
        record["error_type"] = type(exc).__name__
        raise
    finally:
        record["duration_ms"] = round((time.perf_counter() - start) * 1000, 1)
        AUDIT.info("AUDIT %s", " ".join(f"{k}={v}" for k, v in record.items()), extra={"fields": record})
