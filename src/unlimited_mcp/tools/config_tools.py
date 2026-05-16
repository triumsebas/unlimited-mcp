"""Config and safety management tool functions.

Covers:
  - list_capabilities  — what agents, providers and tools are registered
  - add_provider / add_agent / configure_agent / remove_entry
  - add_host / add_queue / ssh_trust_host
  - list_safety_policy / add_allowed_root / remove_allowed_root
  - add_deny_path / remove_deny_path

All mutations go through ConfigStore.update() so comments and formatting
in config.yaml are preserved.  Functions return plain dicts so FastMCP
serialises them as JSON objects without forcing the JobResult shape on
non-job operations.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from unlimited_mcp.config.knowledge import KnowledgeStore
from unlimited_mcp.config.loader import ConfigStore
from unlimited_mcp.config.schema import AgentConfig, Config, Knowledge, ProviderConfig, QueueConfig, SshHostConfig


# ---------------------------------------------------------------------------
# list_capabilities
# ---------------------------------------------------------------------------


def list_capabilities(*, config: Config, knowledge: Knowledge) -> dict[str, Any]:
    """Return a summary of configured agents, providers, and known tools.

    This is the first call an orchestrator should make after connecting
    to understand what workers and models are available.
    """
    agents = {}
    for name, a in config.agents.items():
        agents[name] = {
            "cli": a.cli,
            "cost_tier": a.cost_tier,
            "speed_tier": a.speed_tier,
            "tags": a.tags,
            "suitable_for": a.suitable_for,
            "workspace": a.workspace,
        }

    providers = {}
    for name, p in config.providers.items():
        providers[name] = {
            "type": p.type,
            "model": p.model,
            "cost_tier": p.cost_tier,
            "speed_tier": p.speed_tier,
            "tags": p.tags,
        }

    tools = {}
    for name, t in knowledge.tools.items():
        tools[name] = {"safety_class": t.safety_class}

    hosts = {}
    for name, h in config.hosts.items():
        hosts[name] = {
            "type": h.type,
            "host": getattr(h, "host", None),
            "user": getattr(h, "user", None),
            "port": getattr(h, "port", None),
        }

    queues = {}
    for name, q in config.queues.items():
        queues[name] = {"type": q.type, "slots": q.slots, "host": q.host}

    return {
        "agents": agents,
        "providers": providers,
        "tools": tools,
        "hosts": hosts,
        "queues": queues,
        "allowed_roots": config.allowed_roots,
        "deny_paths": config.deny_paths,
        "safety": {
            "allow_shell_like_argv": config.safety.allow_shell_like_argv,
            "default_safety_policy": config.safety.default_safety_policy,
            "confirm_token_ttl_seconds": config.safety.confirm_token_ttl_seconds,
        },
    }


# ---------------------------------------------------------------------------
# Provider management
# ---------------------------------------------------------------------------


def add_provider(
    name: str,
    provider_type: str,
    model: str,
    *,
    base_url: str | None = None,
    api_key_env: str | None = None,
    tags: list[str] | None = None,
    config_store: ConfigStore,
) -> dict[str, Any]:
    """Add or replace a provider entry in config.yaml."""
    tags = tags or []
    # Validate via pydantic before touching the file.
    ProviderConfig.model_validate(
        {
            "type": provider_type,
            "model": model,
            "base_url": base_url,
            "api_key_env": api_key_env,
            "tags": tags,
        }
    )

    def _mutate(doc: CommentedMap) -> None:
        providers = doc.setdefault("providers", CommentedMap())
        entry: CommentedMap = CommentedMap()
        entry["type"] = provider_type
        entry["model"] = model
        if base_url:
            entry["base_url"] = base_url
        if api_key_env:
            entry["api_key_env"] = api_key_env
        if tags:
            entry["tags"] = list(tags)
        providers[name] = entry

    config_store.update(_mutate)
    return {"ok": True, "message": f"Provider {name!r} added (type={provider_type}, model={model})."}


# ---------------------------------------------------------------------------
# Agent management
# ---------------------------------------------------------------------------


def add_agent(
    name: str,
    cli: str,
    *,
    tags: list[str] | None = None,
    suitable_for: list[str] | None = None,
    workspace: str | None = None,
    params: dict[str, Any] | None = None,
    config_store: ConfigStore,
) -> dict[str, Any]:
    """Add or replace an agent entry in config.yaml."""
    tags = tags or []
    suitable_for = suitable_for or []
    params = params or {}
    AgentConfig.model_validate(
        {
            "cli": cli,
            "tags": tags,
            "suitable_for": suitable_for,
            "workspace": workspace,
            "params": params,
        }
    )

    def _mutate(doc: CommentedMap) -> None:
        agents = doc.setdefault("agents", CommentedMap())
        entry: CommentedMap = CommentedMap()
        entry["cli"] = cli
        if tags:
            entry["tags"] = list(tags)
        if suitable_for:
            entry["suitable_for"] = list(suitable_for)
        if workspace:
            entry["workspace"] = workspace
        if params:
            p = CommentedMap()
            for k, v in params.items():
                p[k] = v
            entry["params"] = p
        agents[name] = entry

    config_store.update(_mutate)
    return {"ok": True, "message": f"Agent {name!r} added (cli={cli!r})."}


def configure_agent(
    name: str,
    set: dict[str, Any] | None = None,  # noqa: A002
    unset: list[str] | None = None,
    *,
    config_store: ConfigStore,
) -> dict[str, Any]:
    """Update params (or top-level fields) on an existing agent entry.

    *set* is a mapping of field_name → value to apply. *unset* is a list of
    param keys to remove from ``params``.  Both can be used in the same call.
    """
    set = set or {}
    unset = unset or []

    def _mutate(doc: CommentedMap) -> None:
        agents = doc.get("agents") or CommentedMap()
        if name not in agents:
            raise KeyError(f"Agent {name!r} not found in config.yaml. Add it with add_agent first.")
        entry = agents[name]
        # Top-level fields that can be set directly.
        top_level = {
            "cli", "cost_tier", "speed_tier", "tags", "suitable_for",
            "workspace", "queue", "exec_host", "env_extra", "supports_clarify",
        }
        for k, v in set.items():
            if k in top_level:
                entry[k] = v
            else:
                params = entry.setdefault("params", CommentedMap())
                params[k] = v
        # Unset only applies to params sub-keys.
        if unset and "params" in entry:
            for k in unset:
                entry["params"].pop(k, None)

    config_store.update(_mutate)
    return {"ok": True, "message": f"Agent {name!r} updated."}


def configure_safety(
    *,
    allow_shell_like_argv: bool | None = None,
    default_safety_policy: str | None = None,
    confirm_token_ttl_seconds: int | None = None,
    log_full_shell_scripts: bool | None = None,
    clarify_max_rounds: int | None = None,
    clarify_max_seconds: int | None = None,
    config_store: ConfigStore,
) -> dict[str, Any]:
    """Update global safety and clarify settings in config.yaml.

    All parameters are optional — only the ones provided are changed.

    allow_shell_like_argv:      Allow argv starting with bash/sh/python -c etc.
                                Default False (blocked for safety).
    default_safety_policy:      'read_only' | 'standard' | 'permissive'.
    confirm_token_ttl_seconds:  How long a confirm_token stays valid (default 300).
    log_full_shell_scripts:     Log full shell script content in audit log.
    clarify_max_rounds:         Cap on clarify_rounds regardless of caller (default 5).
    clarify_max_seconds:        Total Q&A wait budget in seconds (default 300).
    """
    valid_policies = {"read_only", "standard", "permissive"}
    if default_safety_policy is not None and default_safety_policy not in valid_policies:
        return {
            "ok": False,
            "message": f"Invalid default_safety_policy {default_safety_policy!r}. "
                       f"Valid: {sorted(valid_policies)}",
        }

    def _mutate(doc: CommentedMap) -> None:
        if any(v is not None for v in [
            allow_shell_like_argv, default_safety_policy,
            confirm_token_ttl_seconds, log_full_shell_scripts,
        ]):
            safety = doc.setdefault("safety", CommentedMap())
            if allow_shell_like_argv is not None:
                safety["allow_shell_like_argv"] = allow_shell_like_argv
            if default_safety_policy is not None:
                safety["default_safety_policy"] = default_safety_policy
            if confirm_token_ttl_seconds is not None:
                safety["confirm_token_ttl_seconds"] = confirm_token_ttl_seconds
            if log_full_shell_scripts is not None:
                safety["log_full_shell_scripts"] = log_full_shell_scripts

        if clarify_max_rounds is not None or clarify_max_seconds is not None:
            clarify = doc.setdefault("clarify", CommentedMap())
            if clarify_max_rounds is not None:
                clarify["max_rounds"] = clarify_max_rounds
            if clarify_max_seconds is not None:
                clarify["max_total_seconds"] = clarify_max_seconds

    config_store.update(_mutate)
    changed = {k: v for k, v in {
        "allow_shell_like_argv": allow_shell_like_argv,
        "default_safety_policy": default_safety_policy,
        "confirm_token_ttl_seconds": confirm_token_ttl_seconds,
        "log_full_shell_scripts": log_full_shell_scripts,
        "clarify_max_rounds": clarify_max_rounds,
        "clarify_max_seconds": clarify_max_seconds,
    }.items() if v is not None}
    return {"ok": True, "message": f"Updated: {changed}"}


def remove_entry(
    section: str,
    name: str,
    *,
    config_store: ConfigStore,
) -> dict[str, Any]:
    """Remove a named entry from *section* (``"agents"`` or ``"providers"``)."""
    if section not in ("agents", "providers", "hosts", "queues"):
        return {
            "ok": False,
            "message": f"Unknown section {section!r}. Valid: agents, providers, hosts, queues.",
        }

    def _mutate(doc: CommentedMap) -> None:
        group = doc.get(section)
        if group is None or name not in group:
            raise KeyError(f"{section}.{name} not found.")
        del group[name]

    try:
        config_store.update(_mutate)
    except KeyError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"Removed {section}.{name}."}


# ---------------------------------------------------------------------------
# Host management
# ---------------------------------------------------------------------------


def add_host(
    name: str,
    host: str,
    user: str,
    *,
    port: int = 22,
    key_file: str | None = None,
    key_passphrase_env: str | None = None,
    key_passphrase_keyring: str | None = None,
    config_store: ConfigStore,
) -> dict[str, Any]:
    """Add or replace an SSH host entry in config.yaml.

    The host is immediately available for run_command(exec_host=name) and
    delegate_to_agent(exec_host=name) — no restart needed.
    Call ssh_trust_host(host, port) first if the host is not yet in
    ~/.ssh/known_hosts.
    """
    SshHostConfig.model_validate(
        {
            "type": "ssh",
            "user": user,
            "host": host,
            "port": port,
            "key_file": key_file,
            "key_passphrase_env": key_passphrase_env,
            "key_passphrase_keyring": key_passphrase_keyring,
        }
    )

    def _mutate(doc: CommentedMap) -> None:
        hosts = doc.setdefault("hosts", CommentedMap())
        entry: CommentedMap = CommentedMap()
        entry["type"] = "ssh"
        entry["user"] = user
        entry["host"] = host
        if port != 22:
            entry["port"] = port
        if key_file:
            entry["key_file"] = key_file
        if key_passphrase_env:
            entry["key_passphrase_env"] = key_passphrase_env
        if key_passphrase_keyring:
            entry["key_passphrase_keyring"] = key_passphrase_keyring
        hosts[name] = entry

    config_store.update(_mutate)
    return {
        "ok": True,
        "message": (
            f"Host {name!r} added (ssh {user}@{host}:{port}). "
            "Test with: run_command(['hostname'], exec_host='{name}')"
        ).replace("'{name}'", f"'{name}'"),
    }


def add_queue(
    name: str,
    *,
    queue_type: str = "remote_ts",
    slots: int = 1,
    host: str | None = None,
    socket: str | None = None,
    config_store: ConfigStore,
) -> dict[str, Any]:
    """Add or replace a queue entry in config.yaml.

    queue_type: 'remote_ts' (task-spooler on a remote SSH host) or
                'local_ts'  (task-spooler on this machine).
    host:       SSH host name (key in hosts:) — required for remote_ts.
    slots:      Maximum simultaneous jobs (ts -S <n>).

    A server restart is required for new queues to become active
    (existing queues are wired at startup). Call restart_server() after
    adding a queue.
    """
    QueueConfig.model_validate(
        {"type": queue_type, "slots": slots, "host": host, "socket": socket}
    )

    def _mutate(doc: CommentedMap) -> None:
        queues = doc.setdefault("queues", CommentedMap())
        entry: CommentedMap = CommentedMap()
        entry["type"] = queue_type
        entry["slots"] = slots
        if host:
            entry["host"] = host
        if socket:
            entry["socket"] = socket
        queues[name] = entry

    config_store.update(_mutate)
    restart_hint = " Call restart_server() to activate it." if queue_type == "remote_ts" else ""
    return {
        "ok": True,
        "message": (
            f"Queue {name!r} added (type={queue_type}, slots={slots}"
            + (f", host={host!r}" if host else "")
            + f").{restart_hint}"
        ),
    }


def ssh_trust_host(host: str, port: int = 22) -> dict[str, Any]:
    """Add the SSH host key of *host* to ~/.ssh/known_hosts via ssh-keyscan.

    This is required once per host before the MCP can connect.
    WARNING: fingerprint is not manually verified — only use on trusted networks.
    After this call, test the connection with run_command(['hostname'], exec_host=<name>).
    """
    known_hosts_path = Path.home() / ".ssh" / "known_hosts"
    known_hosts_path.parent.mkdir(mode=0o700, exist_ok=True)

    try:
        result = subprocess.run(
            ["ssh-keyscan", "-H", "-p", str(port), host],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return {"ok": False, "message": "ssh-keyscan not found — install OpenSSH client."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"ssh-keyscan timed out connecting to {host}:{port}."}

    if not result.stdout.strip():
        stderr = result.stderr.strip()[:300]
        return {
            "ok": False,
            "message": f"ssh-keyscan returned no keys for {host}:{port}. {stderr}",
        }

    with open(known_hosts_path, "a") as f:
        f.write(result.stdout)

    lines = [l for l in result.stdout.strip().splitlines() if l]
    return {
        "ok": True,
        "message": (
            f"Added {len(lines)} host key(s) for {host}:{port} to {known_hosts_path}. "
            "WARNING: fingerprint was not manually verified."
        ),
        "keys_added": len(lines),
    }


# ---------------------------------------------------------------------------
# Safety policy
# ---------------------------------------------------------------------------


def list_safety_policy(*, config: Config) -> dict[str, Any]:
    """Return the current safety configuration, allowed_roots and deny_paths."""
    return {
        "safety": {
            "allow_shell_like_argv": config.safety.allow_shell_like_argv,
            "default_safety_policy": config.safety.default_safety_policy,
            "confirm_token_ttl_seconds": config.safety.confirm_token_ttl_seconds,
            "log_full_shell_scripts": config.safety.log_full_shell_scripts,
        },
        "allowed_roots": config.allowed_roots,
        "deny_paths": config.deny_paths,
    }


def add_allowed_root(path: str, *, config_store: ConfigStore) -> dict[str, Any]:
    """Append *path* to allowed_roots (no-op if already present)."""
    resolved = str(Path(path).expanduser())

    def _mutate(doc: CommentedMap) -> None:
        roots = doc.setdefault("allowed_roots", CommentedSeq())
        if resolved not in list(roots):
            roots.append(resolved)

    config_store.update(_mutate)
    return {"ok": True, "message": f"Added {resolved!r} to allowed_roots."}


def remove_allowed_root(path: str, *, config_store: ConfigStore) -> dict[str, Any]:
    """Remove *path* from allowed_roots."""
    resolved = str(Path(path).expanduser())

    def _mutate(doc: CommentedMap) -> None:
        roots = doc.get("allowed_roots")
        if roots is None:
            return
        items = list(roots)
        if resolved in items:
            items.remove(resolved)
            doc["allowed_roots"] = CommentedSeq(items)

    config_store.update(_mutate)
    return {"ok": True, "message": f"Removed {resolved!r} from allowed_roots."}


def add_deny_path(path: str, *, config_store: ConfigStore) -> dict[str, Any]:
    """Append *path* to deny_paths (no-op if already present)."""
    resolved = str(Path(path).expanduser())

    def _mutate(doc: CommentedMap) -> None:
        denies = doc.setdefault("deny_paths", CommentedSeq())
        if resolved not in list(denies):
            denies.append(resolved)

    config_store.update(_mutate)
    return {"ok": True, "message": f"Added {resolved!r} to deny_paths."}


def remove_deny_path(path: str, *, config_store: ConfigStore) -> dict[str, Any]:
    """Remove *path* from deny_paths."""
    resolved = str(Path(path).expanduser())

    def _mutate(doc: CommentedMap) -> None:
        denies = doc.get("deny_paths")
        if denies is None:
            return
        items = list(denies)
        if resolved in items:
            items.remove(resolved)
            doc["deny_paths"] = CommentedSeq(items)

    config_store.update(_mutate)
    return {"ok": True, "message": f"Removed {resolved!r} from deny_paths."}
