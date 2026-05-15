"""query_logs implementation: filter JSONL log files without exposing raw paths.

Reads ``logs/server.jsonl`` (operational) and ``audit/errors.jsonl`` (errors),
filters by ``since``, ``level``, ``tool``, ``job_id``, and ``source``,
returns the last *limit* matching entries.

``since`` accepts:
  - Relative strings: "1h", "2h", "30m", "1d", "7d"
  - ISO-8601 datetimes: "2025-05-15T10:00:00Z"
  - ``None`` for no time filter (returns newest *limit* lines)
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _parse_since(since: str) -> datetime:
    """Convert a relative duration string or ISO datetime to a UTC datetime."""
    relative = re.fullmatch(r"(\d+)(m|h|d)", since.strip())
    if relative:
        value, unit = int(relative.group(1)), relative.group(2)
        delta = {"m": timedelta(minutes=value), "h": timedelta(hours=value), "d": timedelta(days=value)}[unit]
        return datetime.now(UTC) - delta
    return datetime.fromisoformat(since.replace("Z", "+00:00"))


def _matches(entry: dict[str, Any], *, since_dt: datetime | None, level: str | None, tool: str | None, job_id: str | None) -> bool:
    if since_dt is not None:
        ts_raw = entry.get("timestamp") or entry.get("ts") or ""
        if ts_raw:
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if ts < since_dt:
                    return False
            except ValueError:
                pass

    if level and entry.get("level", "").lower() != level.lower():
        return False
    if tool and entry.get("tool") != tool:
        return False
    if job_id and entry.get("job_id") != job_id:
        return False
    return True


def _read_jsonl(path: Path, *, since_dt: datetime | None, level: str | None, tool: str | None, job_id: str | None) -> list[dict[str, Any]]:
    """Read matching entries from a single JSONL file (newest last)."""
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if _matches(entry, since_dt=since_dt, level=level, tool=tool, job_id=job_id):
                entries.append(entry)
    except OSError:
        pass
    return entries


def query_logs(
    logs_dir: Path,
    audit_dir: Path,
    *,
    since: str | None = None,
    level: str | None = None,
    tool: str | None = None,
    job_id: str | None = None,
    source: str = "server",
    limit: int = 50,
) -> dict[str, Any]:
    """Return filtered log entries from the MCP operational and error logs.

    Parameters
    ----------
    logs_dir:   Path to ``~/.local/state/unlimited-mcp/logs/``.
    audit_dir:  Path to ``~/.local/state/unlimited-mcp/audit/``.
    since:      Relative duration ("1h", "30m", "2d") or ISO-8601 datetime.
    level:      Filter by log level ("info", "warning", "error").
    tool:       Filter by MCP tool name (e.g. "run_command").
    job_id:     Filter by job identifier.
    source:     "server" | "errors" | "all". Selects which log file(s) to read.
    limit:      Maximum number of entries to return (newest wins).
    """
    since_dt: datetime | None = None
    if since:
        try:
            since_dt = _parse_since(since)
        except (ValueError, KeyError):
            return {
                "ok": False,
                "error": f"Cannot parse since={since!r}. Use '1h', '30m', '2d', or an ISO-8601 datetime.",
                "entries": [],
            }

    sources_read: list[str] = []
    all_entries: list[dict[str, Any]] = []

    if source in ("server", "all"):
        server_log = logs_dir / "server.jsonl"
        entries = _read_jsonl(server_log, since_dt=since_dt, level=level, tool=tool, job_id=job_id)
        if entries:
            for e in entries:
                e.setdefault("_source", "server")
        all_entries.extend(entries)
        sources_read.append(str(server_log))

    if source in ("errors", "all"):
        errors_log = audit_dir / "errors.jsonl"
        entries = _read_jsonl(errors_log, since_dt=since_dt, level=level, tool=tool, job_id=job_id)
        if entries:
            for e in entries:
                e.setdefault("_source", "errors")
        all_entries.extend(entries)
        sources_read.append(str(errors_log))

    # Sort by timestamp ascending, then take the last `limit` (newest)
    def _ts_key(e: dict[str, Any]) -> str:
        return e.get("timestamp") or e.get("ts") or ""

    all_entries.sort(key=_ts_key)
    truncated = len(all_entries) > limit
    returned = all_entries[-limit:]

    return {
        "ok": True,
        "total_matched": len(all_entries),
        "returned": len(returned),
        "truncated": truncated,
        "sources_read": sources_read,
        "entries": returned,
    }
