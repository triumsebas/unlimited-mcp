# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Secret-redaction filter applied to argv tails, stdout/stderr, log fields.

Pattern: replace any occurrence of the runtime *value* of an env var that
looks like a secret (or that the operator declared as secret) with
``***REDACTED***``. We deliberately do **not** scan for secret-shaped
tokens (regex on key shapes) because false positives are worse than false
negatives in audit output — the operator who has the env var also has the
audit log, so the env-var-name approach is sound.

Auto-detected suffixes: ``_KEY``, ``_TOKEN``, ``_SECRET``, ``_PASSWORD``,
``_PASSWD``, ``_API_KEY``. Operators can pass extra names via
``env_var_names``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

REDACTED = "***REDACTED***"

#: Env var name suffixes that trigger automatic redaction of their value.
DEFAULT_SUFFIXES: tuple[str, ...] = (
    "_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_PASSWD",
    "_API_KEY",
)

#: Values shorter than this are treated as placeholders, not secrets, to
#: avoid e.g. wiping every "1" from output when ``DEBUG_KEY=1``.
MIN_SECRET_LEN = 6


class Redactor:
    """Scrub secret env-var values from text, argv lists, or bytes."""

    def __init__(
        self,
        env_var_names: Iterable[str] | None = None,
        environ: Mapping[str, str] | None = None,
        *,
        suffixes: Iterable[str] = DEFAULT_SUFFIXES,
        min_secret_len: int = MIN_SECRET_LEN,
    ) -> None:
        env: Mapping[str, str] = environ if environ is not None else os.environ
        names: set[str] = set()
        for name in env:
            if any(name.endswith(s) for s in suffixes):
                names.add(name)
        if env_var_names:
            names.update(env_var_names)

        secrets: list[str] = []
        for name in names:
            val = env.get(name)
            if val and len(val) >= min_secret_len:
                secrets.append(val)
        # Longer secrets first so they win over shorter ones that happen
        # to be substrings (rare but possible).
        secrets.sort(key=len, reverse=True)
        self._secrets: tuple[str, ...] = tuple(secrets)

    @property
    def secret_count(self) -> int:
        """Number of distinct secret values being scrubbed (for diagnostics)."""
        return len(self._secrets)

    def redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, REDACTED)
        return text

    def redact_argv(self, argv: list[str]) -> list[str]:
        return [self.redact(a) for a in argv]

    def redact_bytes(self, data: bytes) -> bytes:
        """Best-effort redaction on bytes. Non-UTF-8 input is returned
        unchanged because we cannot safely substring-match without
        decoding, and we'd rather leak than corrupt downstream parsers."""
        try:
            decoded = data.decode("utf-8")
        except UnicodeDecodeError:
            return data
        return self.redact(decoded).encode("utf-8")
