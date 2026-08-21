"""Isabelle backend implemented through the Isabelle-MCP PIDE client."""

from .backend import IsabelleBackend
from .theory import discover_theorem_name

__all__ = ["IsabelleBackend", "discover_theorem_name"]
