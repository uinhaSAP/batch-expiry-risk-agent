"""Unit tests for risk_calculator.py."""

import json
import sys
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from risk_calculator import calculate_net_risk, _fetch_ibp_demand
from models import BatchRecord


def _make_batch(
    qty_on_hand=200.0,
    qty_on_open_orders=50.0,
    days_to_expiry=30,
    material="MAT001",
    plant="1000",
):
    sled = date.today() + timedelta(days=days_to_expiry)
    return BatchRecord(
        batch_number="B001",
        material=material,
        description="Test Material",
        plant=plant,
        storage_location="WH01",
        bin="BIN-01",
        qty_on_hand=qty_on_hand,
        qty_on_open_orders=qty_on_open_orders,
        sled=sled,
        days_to_expiry=days_to_expiry,
        unit_value=25.0,
        unit_of_measure="EA",
        bin_velocity_class="C",
        temperature_zone="AMBIENT",
        hazmat_flag=False,
    )


@pytest.mark.asyncio
async def test_net_risk_qty_basic():
    batch = _make_batch(qty_on_hand=200.0, qty_on_open_orders=50.0, days_to_expiry=30)
    # IBP: 300 units / 90 days = 3.33/day × 30 days = 100 projected consumption
    # net_risk_qty = 200 - 50 = 150
    # risk_qty = max(0, 150 - 100) = 50

    mock_tool = MagicMock()
    mock_tool.name = "forecast_demand_tool"
    mock_tool.arun = AsyncMock(return_value=json.dumps({
        "value": [{"KeyFigureValue": 300.0}]
    }))

    result = await calculate_net_risk(batch, tools=[mock_tool], demand_horizon_days=90)
    assert result.net_risk_qty == 150.0
    assert abs(result.projected_consumption - 100.0) < 0.01
    assert abs(result.risk_qty - 50.0) < 0.01
    assert not result.ibp_data_stale


@pytest.mark.asyncio
async def test_risk_qty_never_negative():
    batch = _make_batch(qty_on_hand=100.0, qty_on_open_orders=50.0, days_to_expiry=30)
    # IBP: 3000 units — demand far exceeds stock
    mock_tool = MagicMock()
    mock_tool.name = "forecast_demand"
    mock_tool.arun = AsyncMock(return_value=json.dumps({
        "value": [{"KeyFigureValue": 3000.0}]
    }))
    result = await calculate_net_risk(batch, tools=[mock_tool], demand_horizon_days=90)
    assert result.risk_qty == 0.0


@pytest.mark.asyncio
async def test_ibp_data_stale_detection():
    from datetime import datetime, timezone, timedelta
    # Provide a timestamp more than 24h old
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    batch = _make_batch()
    mock_tool = MagicMock()
    mock_tool.name = "ibp_forecast_tool"
    mock_tool.arun = AsyncMock(return_value=json.dumps({
        "value": [{"KeyFigureValue": 100.0, "LastUpdatedAt": old_ts}]
    }))
    result = await calculate_net_risk(batch, tools=[mock_tool], demand_horizon_days=90, ibp_freshness_hours=24)
    assert result.ibp_data_stale is True


@pytest.mark.asyncio
async def test_no_forecast_tool():
    batch = _make_batch()
    result = await calculate_net_risk(batch, tools=[], demand_horizon_days=90)
    assert result.ibp_demand_per_day == 0.0
    assert result.projected_consumption == 0.0
    # risk_qty = net_risk_qty - 0 = all net qty
    assert result.risk_qty == result.net_risk_qty


@pytest.mark.asyncio
async def test_projected_consumption_uses_days_to_expiry():
    batch = _make_batch(days_to_expiry=10)
    mock_tool = MagicMock()
    mock_tool.name = "demand_forecast"
    # 90 units / 90 days = 1/day; 1 × 10 days = 10 projected
    mock_tool.arun = AsyncMock(return_value=json.dumps({
        "value": [{"KeyFigureValue": 90.0}]
    }))
    result = await calculate_net_risk(batch, tools=[mock_tool], demand_horizon_days=90)
    assert abs(result.ibp_demand_per_day - 1.0) < 0.01
    assert abs(result.projected_consumption - 10.0) < 0.01
