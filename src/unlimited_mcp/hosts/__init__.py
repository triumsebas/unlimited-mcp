"""Execution backends (``Host`` implementations).

Phase 1 ships :class:`LocalHost`.  :class:`SshHost` requires the optional
``ssh`` dependency group (``pip install 'unlimited-mcp[ssh]'``).
"""

from .base import Host, RunOutput
from .local import LocalHost
from .registry import HostRegistry
from .ssh import SshHost

__all__ = ["Host", "HostRegistry", "LocalHost", "RunOutput", "SshHost"]
