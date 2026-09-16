"""MCP: the servers Altus ships kitted out, and how dangerous their tools are.

Headless, like ``altus.cloud`` --- no Textual, and the ``mcp`` client SDK is
imported only inside :mod:`altus.mcp.session`, so classification works with the
extra uninstalled.

This is the first surface Altus cannot measure offline. botocore, the ARM
provider manifests and the GCP discovery documents all ship on disk; an MCP
server's tool list lives behind an authenticated connection to a product that
ships on its own schedule. So the classifier's input is a *curated manifest
that drifts*, and the job of this package is to make that drift loud rather
than silent.
"""

from __future__ import annotations

from altus.mcp.catalog import CATALOG, ServerSpec, server_spec
from altus.mcp.classify import ToolInfo, classify, manifest, target_for

__all__ = [
    "CATALOG",
    "ServerSpec",
    "ToolInfo",
    "classify",
    "manifest",
    "server_spec",
    "target_for",
]
