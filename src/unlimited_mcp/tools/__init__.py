"""Tool-layer functions for unlimited-mcp.

Each public function in this package maps 1-to-1 to an MCP tool that will be
registered in a later phase.  Functions receive all dependencies as arguments;
there is no module-level state.
"""

from unlimited_mcp.tools.execution import delegate_to_agent, run_and_summarize, run_command

__all__ = ["delegate_to_agent", "run_and_summarize", "run_command"]
