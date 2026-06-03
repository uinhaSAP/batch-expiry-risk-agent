"""MCP tool loader.

Owned indirection layer between agent code and the Agent Gateway.
All agent code imports get_mcp_tools from here.

Behaviour is controlled by the IBD_TESTING environment variable:

  Production (IBD_TESTING not set):
      Uses Agent Gateway client directly from the SDK to connect via mTLS.
      Credentials are loaded from the UMS volume mount (/etc/ums/credentials/credentials)
      or the AGW_CREDENTIALS_JSON environment variable.

  Local / test mode (IBD_TESTING=1):
      Reads mcp-mock.json from the directory containing this file's parent
      (i.e. <asset-root>/mcp-mock.json) and returns LangChain StructuredTool
      instances built from the mock data — no network calls.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from sap_cloud_sdk.agentgateway import create_client
from pydantic import create_model
from langchain_core.tools import StructuredTool

from util import enhance_tool_description, enhance_tool_name, call_mcp_tool_with_retry

logger = logging.getLogger(__name__)

# mcp-mock.json lives at the asset root (one level above app/)
_MOCK_FILE = Path(__file__).parent.parent / "mcp-mock.json"

# Tool cache for performance optimization
_tool_cache: Optional[tuple[list, float]] = None
_CACHE_TTL = float(os.environ.get("MCP_TOOL_CACHE_TTL", "60.0"))  # seconds


def _build_mock_tools() -> list:
    """Build LangChain StructuredTool instances from mcp-mock.json.

    Returns an empty list (without error) when mcp-mock.json is absent or
    cannot be parsed — add/fix the file to enable tool mocking.
    """
    if not _MOCK_FILE.exists():
        return []

    try:
        mock_data = json.loads(_MOCK_FILE.read_text())
    except Exception:
        logger.warning("Failed to parse mcp-mock.json at %s — returning empty tool list", _MOCK_FILE, exc_info=True)
        return []

    tools = []

    from langchain_core.tools import StructuredTool
    from pydantic import Field, create_model

    for _server_slug, server in mock_data.get("servers", {}).items():
        for tool_name, tool_def in server.get("tools", {}).items():
            description = tool_def.get("description", "")
            mock_response = tool_def.get("mock_response", {})
            input_schema = tool_def.get("input_schema", {})

            props = input_schema.get("properties", {})
            required_fields = set(input_schema.get("required", []))
            field_definitions: dict = {}
            for field_name, field_info in props.items():
                json_type = field_info.get("type", "string")
                if json_type == "integer":
                    python_type = int
                elif json_type == "number":
                    python_type = float
                elif json_type == "boolean":
                    python_type = bool
                else:
                    python_type = str

                if field_name in required_fields:
                    field_definitions[field_name] = (python_type, Field(description=field_info.get("description", "")))
                else:
                    field_definitions[field_name] = (python_type, Field(default=None, description=field_info.get("description", "")))

            args_schema = create_model(f"{tool_name}_args", **field_definitions) if field_definitions else create_model(f"{tool_name}_args")
            _response = json.dumps(mock_response)

            async def _coroutine(_resp=_response, **kwargs) -> str:
                return _resp

            tools.append(
                StructuredTool(
                    name=tool_name,
                    description=description,
                    args_schema=args_schema,
                    coroutine=_coroutine,
                )
            )

    logger.info("Loaded %d mock MCP tool(s) from %s", len(tools), _MOCK_FILE)
    return tools




def _convert_mcp_tool_to_langchain(mcp_tool: Any, agw_client: Any) -> StructuredTool:
    """
    Convert an MCP tool to a LangChain StructuredTool.

    Args:
        mcp_tool: The MCP tool to convert (MCPTool object from SDK)
        agw_client: Agent Gateway client for tool execution

    Returns:
        LangChain StructuredTool

    Raises:
        ValueError: If mcp_tool is None

    Note:
        Uses the SDK's namespaced_name property (format: 'server_name__tool_name')
        to prevent naming conflicts when multiple MCP servers provide tools
        with the same name.
    """
    if mcp_tool is None:
        raise ValueError("mcp_tool parameter cannot be None")

    async def run(**kwargs) -> str:
        """Execute the MCP tool via Agent Gateway client with retry logic."""
        return await call_mcp_tool_with_retry(agw_client, mcp_tool, **kwargs)

    # Build args schema from input_schema
    properties = mcp_tool.input_schema.get("properties", {})
    required = set(mcp_tool.input_schema.get("required", []))

    fields = {}
    for name, prop in properties.items():
        # Map JSON schema types to Python types
        prop_type = prop.get("type", "string")
        python_type = str  # Default to string
        if prop_type == "integer":
            python_type = int
        elif prop_type == "number":
            python_type = float
        elif prop_type == "boolean":
            python_type = bool

        # Required fields use ... (Ellipsis), optional use None default
        if name in required:
            fields[name] = (python_type, ...)
        else:
            fields[name] = (python_type | None, None)

    args_schema = create_model(f"{mcp_tool.name}_args", **fields) if fields else None

    # Enhance description and name with server context
    enhanced_description = enhance_tool_description(mcp_tool)
    namespaced_tool_name = enhance_tool_name(mcp_tool)

    return StructuredTool.from_function(
        coroutine=run,
        name=namespaced_tool_name,
        description=enhanced_description,
        args_schema=args_schema,
    )


async def get_mcp_tools(use_cache: bool = True) -> list:
    """Return LangChain-compatible MCP tools with optional caching.

    In local/test mode (IBD_TESTING=1): returns mock tools from mcp-mock.json.
    In production: uses Agent Gateway client directly from SDK to connect via mTLS.

    Args:
        use_cache: If True, returns cached tools if available and not expired.
                   Cache TTL is controlled by MCP_TOOL_CACHE_TTL env var (default: 60 seconds).

    Returns:
        List of LangChain StructuredTool objects
    """
    global _tool_cache

    if os.environ.get("IBD_TESTING") == "1":
        return _build_mock_tools()

    # Check cache
    if use_cache and _tool_cache is not None:
        tools, cached_at = _tool_cache
        age = time.time() - cached_at
        if age < _CACHE_TTL:
            logger.debug(f"Returning {len(tools)} cached MCP tools (age: {age:.1f}s, TTL: {_CACHE_TTL}s)")
            return tools
        else:
            logger.debug(f"Tool cache expired (age: {age:.1f}s > TTL: {_CACHE_TTL}s), refreshing...")

    try:
        # Create Agent Gateway client directly
        agw_client = create_client()
        logger.info("Agent Gateway client created successfully")

        # Get MCP tools from Agent Gateway
        mcp_tools = await agw_client.list_mcp_tools()

        if not mcp_tools:
            logger.warning("Agent Gateway returned 0 tools - MCP servers may not be available or have no tools")
        else:
            logger.info(f"Successfully retrieved {len(mcp_tools)} tool(s) from Agent Gateway: {[t.name for t in mcp_tools]}")

        # Convert to LangChain tools
        langchain_tools = [_convert_mcp_tool_to_langchain(t, agw_client) for t in mcp_tools]
        logger.info("Loaded %d MCP tool(s) from Agent Gateway", len(langchain_tools))

        # Cache the result
        if use_cache:
            _tool_cache = (langchain_tools, time.time())
            logger.debug(f"Cached {len(langchain_tools)} tools with TTL={_CACHE_TTL}s")

        return langchain_tools
    except Exception:
        logger.exception("Failed to load MCP tools from Agent Gateway")
        # If cache exists and we failed to refresh, return stale cache
        if use_cache and _tool_cache is not None:
            tools, cached_at = _tool_cache
            age = time.time() - cached_at
            logger.warning(f"Returning stale cached tools (age: {age:.1f}s) after failure")
            return tools
        return []
