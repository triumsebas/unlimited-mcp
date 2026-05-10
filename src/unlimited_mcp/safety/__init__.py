"""Safety pipeline: classification, allowed-roots, confirmation, redaction.

Public surface:

* :class:`SafetyChecker` — top-level orchestration used by tools.
* :class:`SafetyDecision` — what the pipeline returns to the caller.
* :class:`ConfirmationStore` — single-use tokens with TTL.
* :class:`Redactor` — secret scrubbing for stdout/stderr/argv.
* :func:`classify_argv` — argv → :data:`SafetyClass`.
* :func:`find_path_args`, :func:`is_within_allowed_roots` — path
  containment helpers.
"""

from .allowed_roots import find_path_args, is_within_allowed_roots
from .argv_check import SafetyChecker, SafetyDecision
from .classes import classify_argv
from .confirmation import ConfirmationStore
from .redactor import Redactor

__all__ = [
    "ConfirmationStore",
    "Redactor",
    "SafetyChecker",
    "SafetyDecision",
    "classify_argv",
    "find_path_args",
    "is_within_allowed_roots",
]
