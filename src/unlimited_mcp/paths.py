# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""XDG-compliant paths for unlimited-mcp config and runtime state.

User configuration lives under ``$XDG_CONFIG_HOME/unlimited-mcp/`` (defaults
to ``~/.config/unlimited-mcp/``); runtime state (jobs, logs, audit) lives
under ``$XDG_STATE_HOME/unlimited-mcp/`` (defaults to
``~/.local/state/unlimited-mcp/``). All accessors are pure functions; no
filesystem mutation happens here except in :func:`ensure_dirs`.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "unlimited-mcp"


def _xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _xdg_state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")


def config_dir() -> Path:
    """User configuration: ``config.yaml``, ``knowledge.local.yaml``, ``.env``."""
    return _xdg_config_home() / APP_NAME


def state_dir() -> Path:
    """Runtime state: ``jobs/``, ``logs/``, ``audit/``, ``runtime.json``."""
    return _xdg_state_home() / APP_NAME


def jobs_dir() -> Path:
    return state_dir() / "jobs"


def logs_dir() -> Path:
    return state_dir() / "logs"


def audit_dir() -> Path:
    return state_dir() / "audit"


def config_path() -> Path:
    return config_dir() / "config.yaml"


def env_path() -> Path:
    return config_dir() / ".env"


def knowledge_local_path() -> Path:
    return config_dir() / "knowledge.local.yaml"


def runtime_path() -> Path:
    return state_dir() / "runtime.json"


def job_dir(job_id: str) -> Path:
    """Directory holding all artifacts for a single job."""
    return jobs_dir() / job_id


def ensure_dirs() -> None:
    """Create all runtime directories. Safe to call repeatedly."""
    for d in (config_dir(), jobs_dir(), logs_dir(), audit_dir()):
        d.mkdir(parents=True, exist_ok=True)
