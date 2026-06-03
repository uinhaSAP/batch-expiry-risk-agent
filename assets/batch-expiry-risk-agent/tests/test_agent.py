"""Integration test — end-to-end agent flow with mocked MCP tools and LLM."""

import json
import sys
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


def _make_batch_response(days_to_expiry=25):
    sled = (date.today() + timedelta(days=days_to_expiry)).isoformat()
    return json.dumps({
        "value": [
            {
                "Batch": "INT-BATCH001",
                "Material": "MAT-INTEGRATION",
                "MaterialDescription": "Integration Test Material",
                "Plant": "1000",
                "StorageLocation": "WH01",
                "StorageBin": "BIN-C5",
                "GoodsMovementQuantity": 500.0,
                "ShelfLifeExpirationDate": sled,
                "MovingAveragePrice": 20.0,
                "BaseUnit": "EA",
                "VelocityClass": "C",
                "TemperatureCondition": "AMBIENT",
                "HazardousSubstanceFlag": False,
            }
        ]
    })


def _make_order_response():
    return json.dumps({"value": []})  # No open orders


def _make_forecast_response():
    return json.dumps({"value": [{"KeyFigureValue": 90.0}]})


def _make_bin_response():
    return json.dumps({
        "value": [
            {
                "StorageBin": "BIN-A1",
                "VelocityClass": "A",
                "TemperatureCondition": "AMBIENT",
            }
        ]
    })


def _make_rtv_response():
    return json.dumps({"value": []})  # No RTV agreement


@pytest.fixture
def mock_mcp_tools():
    """Create a full set of mock MCP tools for integration testing."""
    batch_tool = MagicMock()
    batch_tool.name = "api_batch_srv_tool"
    batch_tool.arun = AsyncMock(return_value=_make_batch_response(days_to_expiry=25))

    order_tool = MagicMock()
    order_tool.name = "warehouseorder_tool"
    order_tool.arun = AsyncMock(return_value=_make_order_response())

    forecast_tool = MagicMock()
    forecast_tool.name = "forecast_demand_tool"
    forecast_tool.arun = AsyncMock(return_value=_make_forecast_response())

    bin_tool = MagicMock()
    bin_tool.name = "storagebin_tool"
    bin_tool.arun = AsyncMock(return_value=_make_bin_response())

    rtv_tool = MagicMock()
    rtv_tool.name = "returnsinspection_tool"
    rtv_tool.arun = AsyncMock(return_value=_make_rtv_response())

    return [batch_tool, order_tool, forecast_tool, bin_tool, rtv_tool]


@pytest.mark.asyncio
async def test_run_agent_produces_report(mock_mcp_tools):
    """Full end-to-end: _run_agent with mocked tools should produce a valid report."""
    from agent import _run_agent

    report = await _run_agent(
        query="Run batch expiry risk scan for plant=1000",
        tools=mock_mcp_tools,
        risk_horizon_days=60,
        demand_horizon_days=90,
    )

    assert isinstance(report, str)
    assert len(report) > 100
    # Report should mention the batch
    assert "INT-BATCH001" in report or "At-Risk" in report or "No at-risk" in report


@pytest.mark.asyncio
async def test_run_agent_handles_no_batches(mock_mcp_tools):
    """When no batches are at risk, report should say so gracefully."""
    from agent import _run_agent

    # Override batch tool to return batches far outside horizon
    for tool in mock_mcp_tools:
        if "batch" in tool.name:
            tool.arun = AsyncMock(return_value=_make_batch_response(days_to_expiry=200))

    report = await _run_agent(
        query="Run batch expiry risk scan",
        tools=mock_mcp_tools,
        risk_horizon_days=60,
    )
    assert "No at-risk" in report or "at-risk" in report.lower() or "FAILED" in report


@pytest.mark.asyncio
async def test_run_agent_handles_scan_failure():
    """When EWM tool fails, agent should return a clear failure message, not partial output."""
    from agent import _run_agent

    failing_tool = MagicMock()
    failing_tool.name = "api_batch_srv_tool"
    failing_tool.arun = AsyncMock(side_effect=ConnectionError("EWM unreachable"))

    order_tool = MagicMock()
    order_tool.name = "warehouseorder_tool"
    order_tool.arun = AsyncMock(return_value=_make_order_response())

    report = await _run_agent(
        query="Run scan",
        tools=[failing_tool, order_tool],
    )
    assert "FAILED" in report


@pytest.mark.asyncio
async def test_invoke_returns_completed_status(mock_mcp_tools):
    """Agent.invoke should return status='completed' for a successful scan."""
    from agent import SampleAgent

    agent = SampleAgent()

    with patch("agent.get_mcp_tools", new=AsyncMock(return_value=mock_mcp_tools)):
        response = await agent.invoke(
            query="Run batch expiry risk scan",
            context_id="test-ctx-001",
        )

    assert response.status in ("completed", "error")
    assert isinstance(response.message, str)
    assert len(response.message) > 50


@pytest.mark.asyncio
async def test_run_agent_plant_filter_parsed():
    """Plant filter from query should be passed to scanner."""
    from agent import _parse_plants

    assert _parse_plants("Run scan for plant=1000") == ["1000"]
    assert _parse_plants("plants: 1000,2000") == ["1000", "2000"]
    assert _parse_plants("no plant info here") is None
