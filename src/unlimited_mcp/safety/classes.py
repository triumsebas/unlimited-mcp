"""Static classification of an argv into a :data:`SafetyClass`.

Looks up ``argv[0]`` (basename) in :class:`Knowledge.tools`. Each tool
declares a base class plus zero or more flag patterns that escalate or
demote the class. An *escalation* always wins over a *demotion*, so an
operator who lists both ``delete`` (escalates to dangerous) and ``get``
(demotes to read) does not accidentally lose the dangerous gate by
running ``kubectl delete -o yaml``.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from unlimited_mcp.config.schema import Knowledge
from unlimited_mcp.jobs.result import SafetyClass

#: Total order on classes. Higher number = more dangerous.
_CLASS_RANK: dict[SafetyClass, int] = {
    "read": 0,
    "unknown": 1,
    "mutating": 2,
    "dangerous": 3,
}


def _higher(a: SafetyClass, b: SafetyClass) -> SafetyClass:
    return a if _CLASS_RANK[a] >= _CLASS_RANK[b] else b


def _lower(a: SafetyClass, b: SafetyClass) -> SafetyClass:
    return a if _CLASS_RANK[a] <= _CLASS_RANK[b] else b


def _argv_basename(argv: list[str]) -> str:
    if not argv:
        return ""
    return PurePosixPath(argv[0]).name


def classify_argv(argv: list[str], knowledge: Knowledge) -> SafetyClass:
    """Return the safety class of an argv, given the knowledge catalog.

    Unknown commands return ``"unknown"`` — the safety pipeline treats
    that as one notch below ``mutating`` for risk roll-up but never
    auto-permits, so a user whose tool isn't catalogued just sees more
    audit-log entries, not a wide-open shell.
    """
    if not argv:
        return "unknown"
    cli = _argv_basename(argv)
    tool = knowledge.tools.get(cli)
    if tool is None:
        return "unknown"

    base: SafetyClass = tool.safety_class
    rest = argv[1:]
    rest_set = set(rest)

    escalation: SafetyClass | None = None
    demotion: SafetyClass | None = None

    for pattern in tool.flag_patterns:
        if any(m in rest_set for m in pattern.match):
            if pattern.escalates_to is not None:
                escalation = (
                    pattern.escalates_to
                    if escalation is None
                    else _higher(escalation, pattern.escalates_to)
                )
            elif pattern.demotes_to is not None:
                demotion = (
                    pattern.demotes_to if demotion is None else _lower(demotion, pattern.demotes_to)
                )

    if escalation is not None:
        return _higher(base, escalation)
    if demotion is not None:
        return _lower(base, demotion)
    return base
