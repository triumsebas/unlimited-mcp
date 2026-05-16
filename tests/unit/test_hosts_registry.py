"""Unit tests for hosts/registry.py."""

from __future__ import annotations

from unlimited_mcp.config.schema import Config, LocalHostConfig, SshHostConfig
from unlimited_mcp.hosts import HostRegistry, LocalHost, SshHost


def _cfg(**hosts: object) -> Config:
    return Config(hosts=hosts)  # type: ignore[arg-type]


def test_local_always_resolves() -> None:
    registry = HostRegistry(_cfg())
    host = registry.get("local")
    assert isinstance(host, LocalHost)


def test_local_cached() -> None:
    registry = HostRegistry(_cfg())
    assert registry.get("local") is registry.get("local")


def test_unknown_host_raises() -> None:
    registry = HostRegistry(_cfg())
    try:
        registry.get("missing")
        assert False, "expected KeyError"
    except KeyError as exc:
        assert "missing" in str(exc)


def test_ssh_host_resolved() -> None:
    ssh_cfg = SshHostConfig(type="ssh", user="ubuntu", host="10.0.0.1")
    registry = HostRegistry(_cfg(gpu=ssh_cfg))
    host = registry.get("gpu")
    assert isinstance(host, SshHost)
    assert host.name == "ssh:ubuntu@10.0.0.1:22"


def test_ssh_host_cached() -> None:
    ssh_cfg = SshHostConfig(type="ssh", user="ubuntu", host="10.0.0.1")
    registry = HostRegistry(_cfg(gpu=ssh_cfg))
    assert registry.get("gpu") is registry.get("gpu")


def test_local_host_config_resolves_to_local_host() -> None:
    local_cfg = LocalHostConfig(type="local")
    registry = HostRegistry(_cfg(my_local=local_cfg))
    host = registry.get("my_local")
    assert isinstance(host, LocalHost)


def test_close_all_clears_cache() -> None:
    ssh_cfg = SshHostConfig(type="ssh", user="ubuntu", host="10.0.0.1")
    registry = HostRegistry(_cfg(gpu=ssh_cfg))
    registry.get("local")
    registry.get("gpu")
    registry.close_all()
    assert registry._cache == {}
