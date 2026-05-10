"""KnowledgeStore: merge of repo ``knowledge.yaml`` with user ``knowledge.local.yaml``.

The repo file is the shared catalog (committed, English, verified). The
local file is gitignored and contains the user's runtime additions or
overrides — populated by the ``register_agent_knowledge`` MCP tool.

Merge precedence is **entry-level replacement**: an entry in
``knowledge.local.yaml`` fully replaces the same-named entry from the repo
catalog within each top-level mergeable group (``clis``, ``tools``,
``providers`` ...). We deliberately do *not* deep-merge inside an entry —
declaring the whole CLI locally is simpler to reason about than a partial
overlay, and matches the mental model "the local file is what you're
trying out before contributing back".
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from threading import Lock
from typing import Any

import yaml

from .schema import Knowledge

#: Top-level Knowledge keys that hold a ``dict[name, entry]`` and merge
#: at the entry level. Anything else is treated as a scalar override.
_MERGEABLE_GROUPS: tuple[str, ...] = (
    "orchestrators",
    "clis",
    "providers",
    "runners",
    "tools",
    "shell_like_argv",
    "workspace_presets",
    "host_templates",
    "probes",
)


class KnowledgeStore:
    """Two-file knowledge loader with live reload by mtime.

    Reads only — writes happen through the ``register_agent_knowledge`` MCP
    tool which manipulates the local file directly. The store re-reads only
    when either file's mtime changes, so calling :meth:`get` per MCP tool
    invocation is cheap.
    """

    def __init__(self, repo_path: Path, local_path: Path) -> None:
        self.repo_path = Path(repo_path)
        self.local_path = Path(local_path)
        self._lock = Lock()
        self._cached: Knowledge | None = None
        self._cached_repo_mtime_ns: int | None = None
        self._cached_local_mtime_ns: int | None = None

    # ---------------- public API -----------------------------------------

    def get(self) -> Knowledge:
        """Return the merged Knowledge. Re-parses only if either mtime changed."""
        with self._lock:
            repo_mt = _mtime_ns(self.repo_path)
            local_mt = _mtime_ns(self.local_path)
            if (
                self._cached is not None
                and repo_mt == self._cached_repo_mtime_ns
                and local_mt == self._cached_local_mtime_ns
            ):
                return self._cached
            merged = self._load_and_merge()
            self._cached = merged
            self._cached_repo_mtime_ns = repo_mt
            self._cached_local_mtime_ns = local_mt
            return merged

    def get_repo(self) -> Knowledge:
        """Return only the repo-level catalog (no local overrides). Useful
        for ``unlimited-mcp doctor`` and contribution review."""
        return _load_one(self.repo_path)

    def get_local(self) -> Knowledge:
        """Return only the local overrides — what the user has learned but
        not yet upstreamed."""
        return _load_one(self.local_path)

    def reload(self) -> Knowledge:
        """Force a re-merge regardless of mtime."""
        with self._lock:
            self._cached = None
            self._cached_repo_mtime_ns = None
            self._cached_local_mtime_ns = None
        return self.get()

    # ---------------- internals ------------------------------------------

    def _load_and_merge(self) -> Knowledge:
        repo_data = _load_raw(self.repo_path)
        local_data = _load_raw(self.local_path)
        merged = _merge(repo_data, local_data)
        return Knowledge.model_validate(merged)


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _load_raw(path: Path) -> dict[str, Any]:
    """Load a YAML file as a plain mapping. Missing file → empty dict."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return data


def _load_one(path: Path) -> Knowledge:
    return Knowledge.model_validate(_load_raw(path))


def _merge(repo: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    """Entry-level replacement merge.

    For each known mergeable group, the repo dict and local dict are merged
    such that local entries fully replace same-named repo entries. Other
    top-level keys (e.g. ``schema_version``) are taken from local if
    present, otherwise from repo.
    """
    merged: dict[str, Any] = deepcopy(repo)

    for key, val in local.items():
        if key in _MERGEABLE_GROUPS and isinstance(val, dict):
            base = merged.setdefault(key, {})
            if not isinstance(base, dict):
                # Pathological: repo has the group as a non-mapping. Local
                # is the source of truth in that case — fully replace.
                merged[key] = deepcopy(val)
                continue
            for entry_name, entry in val.items():
                base[entry_name] = deepcopy(entry)
        else:
            merged[key] = deepcopy(val)
    return merged
