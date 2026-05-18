# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Structured JSON-line logging for unlimited-mcp.

All logs are written as one JSON object per line to
``logs/server.jsonl`` under the runtime state directory. **Logs never go
to stdout** — stdout is reserved for the MCP stdio JSON-RPC stream and
mixing the two would corrupt the protocol. Daily rotation keeps the last
seven files.

Per-line fields are:

* ``timestamp`` (ISO-8601 UTC)
* ``level``
* ``event`` (the message)
* arbitrary bound context: ``tool``, ``job_id``, ``agent``, ``host``,
  ``duration_ms``, ``error_code`` and so on.

Prompts and command outputs are **not** logged by default. Set the
environment variable :data:`LOG_PROMPTS_ENV` to ``"1"`` to opt in to
storing the first 200 chars of each prompt — only do that on a trusted
machine.

Secret redaction is the responsibility of the caller (the safety
``redactor`` module). This module exposes the JSONL surface; it does not
police what callers put into bound context.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Any

import structlog

#: Environment variable that enables prompt logging when set to ``"1"``.
LOG_PROMPTS_ENV = "UNLIMITED_MCP_LOG_PROMPTS"

_LOGFILE_NAME = "server.jsonl"
_DEFAULT_LEVEL = "INFO"


def configure_logging(
    log_dir: Path,
    *,
    level: str = _DEFAULT_LEVEL,
    backup_count: int = 7,
) -> Path:
    """Configure structlog to emit JSONL to ``log_dir/server.jsonl``.

    Idempotent: calling repeatedly resets handlers cleanly so tests and
    re-init flows work. Returns the log file path so callers can surface
    it in ``unlimited-mcp doctor`` output.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / _LOGFILE_NAME

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    handler = logging.handlers.TimedRotatingFileHandler(
        log_path,
        when="midnight",
        backupCount=backup_count,
        encoding="utf-8",
        utc=True,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    return log_path


def get_logger(name: str = "unlimited_mcp") -> Any:
    """Return a bound structlog logger.

    Falls back to a no-op logger if :func:`configure_logging` has not run
    yet — library use and tests stay safe.
    """
    return structlog.get_logger(name)


def log_prompts_enabled() -> bool:
    """``True`` if the operator opted in to prompt logging."""
    return os.environ.get(LOG_PROMPTS_ENV) == "1"
