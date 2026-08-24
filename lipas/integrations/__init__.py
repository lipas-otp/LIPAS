"""Capability clients and experimental compatibility adapters.

``MCPClient``/``MCPHttpClient`` are first-party transport boundaries. The
LangGraph, AutoGen, OpenClaw, and MCP server bridges remain compatibility
surfaces and do not create a second execution authority.
"""
from .langgraph import LangGraphActionNode, LangGraphToolAdapter
from .langgraph import LangGraphHandoffNode
from .mcp import MCPActionServer, MCPClient, MCPClientError, MCPHttpClient
from .openclaw import OpenClawActionBackend
from .autogen import AutoGenHandoffHandler, AutoGenToolAdapter

__all__ = [
    "LangGraphActionNode",
    "LangGraphHandoffNode",
    "LangGraphToolAdapter",
    "AutoGenHandoffHandler",
    "AutoGenToolAdapter",
    "MCPActionServer",
    "MCPClient",
    "MCPClientError",
    "MCPHttpClient",
    "OpenClawActionBackend",
]
