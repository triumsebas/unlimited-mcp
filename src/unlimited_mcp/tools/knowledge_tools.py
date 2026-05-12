"""Knowledge lookup and registration tool functions.

lookup_agent_cli   — query the merged catalog for a CLI entry
register_agent_knowledge — write a new CLI entry to knowledge.local.yaml
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from unlimited_mcp.config.knowledge import KnowledgeStore
from unlimited_mcp.config.schema import CliKnowledge, Knowledge


def lookup_agent_cli(name: str, *, knowledge: Knowledge) -> dict[str, Any]:
    """Look up a CLI agent in the merged knowledge catalog.

    Returns the CliKnowledge entry as a plain dict, or an error dict if
    not found.  The orchestrator can use this to discover install hints,
    command templates, and available params before calling add_agent.
    """
    entry = knowledge.clis.get(name)
    if entry is None:
        known = sorted(knowledge.clis.keys())
        return {
            "ok": False,
            "error": {
                "code": "UNKNOWN_CLI",
                "message": f"CLI {name!r} not found in knowledge catalog.",
                "hint": f"Known CLIs: {known}. Use register_agent_knowledge to add a new one.",
            },
        }
    return {
        "ok": True,
        "name": name,
        "cli": entry.model_dump(),
    }


def register_agent_knowledge(
    name: str,
    command_template: str,
    *,
    docs_url: str | None = None,
    install_hint: str | None = None,
    params: dict[str, Any] | None = None,
    verified: bool = False,
    knowledge_local_path: Path,
) -> dict[str, Any]:
    """Write a CLI entry to knowledge.local.yaml.

    The entry is immediately available via the merged KnowledgeStore on the
    next reload (mtime-triggered). This is how the orchestrator teaches the
    server about a new CLI without restarting.

    Parameters
    ----------
    name:
        The CLI binary name (e.g. ``"goose"``).
    command_template:
        How to invoke it (e.g. ``"goose run {prompt!q}"``).
    docs_url:
        Optional URL for documentation.
    install_hint:
        Optional install instruction surfaced by ``doctor`` and ``UNKNOWN_CLI``.
    params:
        Optional dict of param specs (same shape as knowledge.yaml ``params``).
    verified:
        Whether this template has been tested locally.
    knowledge_local_path:
        Path to ``knowledge.local.yaml`` (injected by server).
    """
    # Validate the entry shape before touching any file.
    entry_dict: dict[str, Any] = {
        "command_template": command_template,
        "docs_url": docs_url,
        "install_hint": install_hint,
        "params": params or {},
        "verified": verified,
    }
    CliKnowledge.model_validate(entry_dict)

    _write_to_local(name, entry_dict, knowledge_local_path)
    return {
        "ok": True,
        "message": (
            f"CLI {name!r} written to knowledge.local.yaml. "
            "Use add_agent to create a runnable agent from it."
        ),
    }


def _write_to_local(name: str, entry: dict[str, Any], path: Path) -> None:
    """Merge *entry* under ``clis.<name>`` in the local knowledge YAML."""
    ryaml = YAML(typ="rt")
    ryaml.indent(mapping=2, sequence=4, offset=2)

    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            doc = ryaml.load(fh)
        if doc is None:
            doc = CommentedMap()
    else:
        doc = CommentedMap()

    clis = doc.setdefault("clis", CommentedMap())
    e = CommentedMap()
    e["command_template"] = entry["command_template"]
    if entry.get("docs_url"):
        e["docs_url"] = entry["docs_url"]
    if entry.get("install_hint"):
        e["install_hint"] = entry["install_hint"]
    if entry.get("params"):
        p = CommentedMap()
        for k, v in entry["params"].items():
            p[k] = v
        e["params"] = p
    e["verified"] = entry.get("verified", False)
    clis[name] = e

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        ryaml.dump(doc, fh)
