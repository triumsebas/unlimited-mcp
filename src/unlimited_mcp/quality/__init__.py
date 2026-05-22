# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Post-job quality gate: lint + type-check a coding worker's changed files."""

from .gate import run_quality_gate

__all__ = ["run_quality_gate"]
