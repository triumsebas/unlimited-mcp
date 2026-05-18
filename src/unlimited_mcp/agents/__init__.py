# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Agent layer: CLI agent abstraction, argv rendering, and runner glue.

* :class:`CLIAgent` — resolved agent definition + ``render_argv`` pipeline.
* :class:`AgentRunner` — composes ``CLIAgent`` with the safety pipeline and
  :class:`~unlimited_mcp.jobs.runner_local.LocalRunner` to dispatch jobs.
* :class:`AgentRenderError` — raised when an invocation cannot be rendered.
"""

from .base import AgentRenderError, CLIAgent
from .runner import DEFAULT_TOOL_NAME, AgentRunner

__all__ = ["DEFAULT_TOOL_NAME", "AgentRenderError", "AgentRunner", "CLIAgent"]
