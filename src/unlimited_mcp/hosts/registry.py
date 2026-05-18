# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""HostRegistry — maps config host names to Host singletons.

Keeps one connection pool per SSH host across all calls in the server's
lifetime.  ``"local"`` always resolves to a fresh :class:`LocalHost`.

Usage::

    registry = HostRegistry(cfg_store, redactor=redactor)
    host = registry.get("gpu_server")   # returns SshHost, cached
    host = registry.get("local")        # always LocalHost
"""

from __future__ import annotations

from unlimited_mcp.safety.redactor import Redactor

from .base import Host
from .local import LocalHost
from .ssh import SshHost

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unlimited_mcp.config.loader import ConfigStore
    from unlimited_mcp.config.schema import Config


class HostRegistry:
    """Map config host names to live ``Host`` instances (cached per name).

    Parameters
    ----------
    config:
        Config snapshot or live store.  A store is re-read on each ``get``
        call so host additions via MCP tools are visible without a restart.
    redactor:
        Forwarded to every ``SshHost`` for secret scrubbing.
    """

    def __init__(
        self,
        config: Config | ConfigStore,
        redactor: Redactor | None = None,
    ) -> None:
        self._config = config
        self._redactor = redactor
        self._cache: dict[str, Host] = {}

    def get(self, name: str) -> Host:
        """Return the ``Host`` for *name*, creating it on first access.

        Raises
        ------
        KeyError
            If *name* is not ``"local"`` and not in ``config.hosts``.
        RuntimeError
            If the ``ssh`` optional dependency is not installed.
        """
        if name in self._cache:
            return self._cache[name]

        if name == "local":
            host: Host = LocalHost(redactor=self._redactor)
            self._cache[name] = host
            return host

        cfg = self._config.get() if hasattr(self._config, "get") else self._config  # type: ignore[union-attr]
        host_cfg = cfg.hosts.get(name)
        if host_cfg is None:
            known = list(cfg.hosts.keys()) or ["(none configured)"]
            raise KeyError(
                f"Host {name!r} not found in config.hosts. "
                f"Known hosts: {known}"
            )

        if host_cfg.type == "local":
            host = LocalHost(redactor=self._redactor)
        elif host_cfg.type == "ssh":
            host = SshHost(host_cfg, redactor=self._redactor)
        else:
            raise ValueError(f"Unknown host type {host_cfg.type!r} for host {name!r}")

        self._cache[name] = host
        return host

    def close_all(self) -> None:
        """Close all SSH connections held by cached hosts."""
        for host in self._cache.values():
            if isinstance(host, SshHost):
                host.close()
        self._cache.clear()
