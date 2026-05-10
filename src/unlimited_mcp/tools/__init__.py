"""Tool-layer functions for unlimited-mcp.

Each public function in this package maps 1-to-1 to an MCP tool that will be
registered in a later phase.  Functions receive all dependencies as arguments;
there is no module-level state.
"""

from unlimited_mcp.tools.execution import delegate_to_agent, run_and_summarize, run_command
from unlimited_mcp.tools.jobs import cancel_job, get_job_result, list_jobs

__all__ = [
    "cancel_job",
    "delegate_to_agent",
    "get_job_result",
    "list_jobs",
    "run_and_summarize",
    "run_command",
]
