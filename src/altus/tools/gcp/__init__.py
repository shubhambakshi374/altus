"""GCP tools.

Split by what the operation needs from the user, the way ``tools/k8s``,
``tools/aws`` and ``tools/azure`` are: reads run freely, changes are gated, and
whole classes can be switched off in ``[cloud.gcp]`` so the model is never told
they exist.
"""

from __future__ import annotations

from typing import Any

from altus.tools.base import BaseTool
from altus.tools.gcp.base import GcpMutatingTool, GcpTool
from altus.tools.gcp.insight import (
    GcpCostTool,
    GcpInventoryTool,
    GcpQuotasTool,
    GcpTopologyTool,
)
from altus.tools.gcp.reads import (
    GcpApisTool,
    GcpAssetsTool,
    GcpCallTool,
    GcpCanITool,
    GcpExplainTool,
    GcpProjectsTool,
    GcpWhoamiTool,
)


def gcp_tools(settings: Any = None) -> list[BaseTool]:
    """The tool set, minus whatever `[cloud.gcp]` switches off."""
    tools: list[BaseTool] = [
        GcpWhoamiTool(),
        GcpProjectsTool(),
        GcpApisTool(),
        GcpExplainTool(),
        GcpCallTool(),
        GcpCanITool(),
        GcpAssetsTool(),
        GcpInventoryTool(),
        GcpTopologyTool(),
        GcpCostTool(),
        GcpQuotasTool(),
    ]
    return tools


__all__ = ["GcpMutatingTool", "GcpTool", "gcp_tools"]
