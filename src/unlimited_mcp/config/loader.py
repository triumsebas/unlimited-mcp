"""Live-reload config loader with atomic writes that preserve YAML comments.

Reads use ``pyyaml`` (faster, simpler). Writes use ``ruamel.yaml`` so any
comments and formatting the user authored survive ``configure_agent`` and
similar mutations. The store re-parses only when the file's mtime changes,
so calling :meth:`ConfigStore.get` per MCP tool invocation is cheap.

Validation is enforced through the :class:`Config` pydantic schema. If the
user's file is malformed, :meth:`get` raises ``ValidationError`` — service
layers translate that into a structured ``CONFIG_INVALID`` error at the
tool boundary; the store itself never returns a half-valid object.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .schema import Config


class ConfigStore:
    """Live-reload, atomic-write config store keyed by file mtime."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = Lock()
        self._cached: Config | None = None
        self._cached_mtime_ns: int | None = None
        self._yaml = YAML(typ="rt")  # round-trip mode preserves comments
        self._yaml.indent(mapping=2, sequence=4, offset=2)

    # ---------------- read -------------------------------------------------

    def get(self) -> Config:
        """Return the current Config. Re-parses only when the file changed."""
        with self._lock:
            mtime_ns = self._current_mtime_ns()
            if self._cached is not None and mtime_ns == self._cached_mtime_ns:
                return self._cached
            cfg = self._load()
            self._cached = cfg
            self._cached_mtime_ns = mtime_ns
            return cfg

    def reload(self) -> Config:
        """Force a re-read regardless of mtime."""
        with self._lock:
            self._cached = None
            self._cached_mtime_ns = None
        return self.get()

    # ---------------- write ------------------------------------------------

    def update(self, mutator: Callable[[CommentedMap], None]) -> Config:
        """Apply a mutation to the YAML document, validate, atomic-write.

        ``mutator`` receives the parsed ruamel ``CommentedMap`` (or an empty
        one if the file does not exist) and mutates it in place. After the
        call, the result is validated through the :class:`Config` schema.
        On validation failure, nothing is written and ``ValidationError``
        is raised. On success, the file is atomically replaced and the
        cache invalidated.
        """
        with self._lock:
            doc = self._load_ruamel()
            mutator(doc)
            # Validate the mutated document before persisting.
            Config.model_validate(self._ruamel_to_plain(doc))
            self._atomic_write(doc)
            self._cached = None
            self._cached_mtime_ns = None
        return self.get()

    # ---------------- internals --------------------------------------------

    def _current_mtime_ns(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def _load(self) -> Config:
        """Read with pyyaml, validate via pydantic. Missing file → defaults."""
        if not self.path.exists():
            return Config()
        text = self.path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{self.path}: top-level YAML must be a mapping")
        return Config.model_validate(data)

    def _load_ruamel(self) -> CommentedMap:
        """Read with ruamel for round-trip writes. Missing file → empty doc."""
        if not self.path.exists():
            return CommentedMap()
        with self.path.open("r", encoding="utf-8") as fh:
            doc = self._yaml.load(fh)
        if doc is None:
            return CommentedMap()
        if not isinstance(doc, CommentedMap):
            raise ValueError(f"{self.path}: top-level YAML must be a mapping")
        return doc

    def _ruamel_to_plain(self, doc: CommentedMap) -> dict[str, Any]:
        """Round-trip ruamel → plain dict via YAML so pydantic validation
        sees primitive types, not ruamel's CommentedMap/CommentedSeq
        subclasses (which can confuse some downstream isinstance checks)."""
        buf = io.StringIO()
        self._yaml.dump(doc, buf)
        plain = yaml.safe_load(buf.getvalue()) or {}
        if not isinstance(plain, dict):
            raise ValueError("mutator produced a non-mapping top-level YAML")
        return plain

    def _atomic_write(self, doc: CommentedMap) -> None:
        """Write via tempfile + os.replace so the target file is never seen
        in a partial state (no torn reads under concurrent get() calls)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                self._yaml.dump(doc, fh)
            os.replace(tmp_path, self.path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
