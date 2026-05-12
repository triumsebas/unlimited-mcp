"""Config and safety management tool functions.

Covers:
  - list_capabilities  — what agents, providers and tools are registered
  - add_provider / add_agent / configure_agent / remove_entry
  - list_safety_policy / add_allowed_root / remove_allowed_root
  - add_deny_path / remove_deny_path

All mutations go through ConfigStore.update() so comments and formatting
in config.yaml are preserved.  Functions return plain dicts so FastMCP
serialises them as JSON objects without forcing the JobResult shape on
non-job operations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from unlimited_mcp.config.knowledge import KnowledgeStore
from unlimited_mcp.config.loader import ConfigStore
from unlimited_mcp.config.schema import AgentConfig, Config, Knowledge, ProviderConfig


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

    return {
        "agents": agents,
        "providers": providers,
        "tools": tools,
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
        top_level = {"cli", "cost_tier", "speed_tier", "tags", "suitable_for", "workspace", "queue"}
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
