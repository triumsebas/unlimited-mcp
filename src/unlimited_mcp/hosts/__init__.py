"""Execution backends (``Host`` implementations).

Phase 1 ships :class:`LocalHost` only.  The SSH backend is added in phase 3.
"""

from .base import Host, RunOutput
from .local import LocalHost

__all__ = ["Host", "LocalHost", "RunOutput"]
