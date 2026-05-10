"""Agent layer: CLI agent abstraction and argv rendering.

Phase 1 ships :class:`CLIAgent` and :func:`CLIAgent.render_argv` only.  The
runner that ties this together with ``Host`` + ``Provider`` + ``Workspace`` +
``JobStore`` + ``Safety`` lives in ``agents/runner.py`` and lands in a later PR.
"""

from .base import AgentRenderError, CLIAgent

__all__ = ["AgentRenderError", "CLIAgent"]
