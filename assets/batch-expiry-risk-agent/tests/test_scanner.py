"""Unit tests for scanner.py."""

import json
import sys
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from scanner import (
    _extract_batch_records_from_tool_response,
    _parse_date,
    fetch_open_orders,
    scan_at_risk_batches,
)
from models import BatchRecord


def _make_batch_item(
    batch_num="BATCH001",
    material="MAT001",
    sled_days=30,
    qty=100.0,
    plant="1000",
):
    sled = (date.today() + timedelta(days=sled_days)).isoformat()
    return {
        "Batch": batch_num,
        "Material": material,
        "MaterialDescription": "Test Material",
        "Plant": plant,
        "StorageLocation": "WH01",
        "StorageBin": "BIN-A1",
        "GoodsMovementQuantity": qty,
        "ShelfLifeExpirationDate": sled,
        "MovingAveragePrice": 10.0,
        "BaseUnit": "EA",
        "VelocityClass": "C",
        "TemperatureCondition": "AMBIENT",
        "HazardousSubstanceFlag": False,
    }


def test_parse_date_iso():
    d = _parse_date("2025-06-15")
    assert d == date(2025, 6, 15)


def test_parse_date_yyyymmdd():
    d = _parse_date("20250615")
    assert d == date(2025, 6, 15)


def test_parse_date_none():
    assert _parse_date(None) is None


def test_parse_date_date_object():
    today = date.today()
    assert _parse_date(today) == today


def test_extract_batch_records_within_horizon():
    item = _make_batch_item(sled_days=30)
    response = json.dumps({"value": [item]})
    records = _extract_batch_records_from_tool_response(response, {}, risk_horizon_days=60)
    assert len(records) == 1
    assert records[0].batch_number == "BATCH001"
    assert records[0].days_to_expiry == 30


def test_extract_batch_records_outside_horizon():
    item = _make_batch_item(sled_days=90)
    response = json.dumps({"value": [item]})
    records = _extract_batch_records_from_tool_response(response, {}, risk_horizon_days=60)
    assert len(records) == 0


def test_extract_batch_records_missing_sled():
    item = _make_batch_item()
    del item["ShelfLifeExpirationDate"]
    response = json.dumps({"value": [item]})
    records = _extract_batch_records_from_tool_response(response, {}, risk_horizon_days=60)
    assert len(records) == 0


def test_extract_batch_records_order_coverage_deduction():
    item = _make_batch_item(qty=100.0)
    # Fully covered by open orders
    response = json.dumps({"value": [item]})
    records = _extract_batch_records_from_tool_response(
        response, {"BATCH001": 100.0}, risk_horizon_days=60
    )
    assert len(records) == 0


def test_extract_batch_records_residual_above_threshold():
    item = _make_batch_item(qty=100.0)
    # 20 units remaining — above both thresholds (10% = 10, absolute = 50)
    # 20 > 10% but 20 < 50 absolute — excluded
    response = json.dumps({"value": [item]})
    records = _extract_batch_records_from_tool_response(
        response, {"BATCH001": 80.0}, risk_horizon_days=60
    )
    assert len(records) == 0  # 20 < RESIDUAL_QTY_ABSOLUTE=50


def test_extract_batch_records_large_residual():
    item = _make_batch_item(qty=1000.0)
    # 800 remaining — above both thresholds
    response = json.dumps({"value": [item]})
    records = _extract_batch_records_from_tool_response(
        response, {"BATCH001": 200.0}, risk_horizon_days=60
    )
    assert len(records) == 1
    assert records[0].qty_on_hand == 1000.0
    assert records[0].qty_on_open_orders == 200.0


def test_extract_batch_records_odata_d_format():
    item = _make_batch_item()
    response = json.dumps({"d": {"results": [item]}})
    records = _extract_batch_records_from_tool_response(response, {}, risk_horizon_days=60)
    assert len(records) == 1


@pytest.mark.asyncio
async def test_fetch_open_orders_no_tool():
    tools = []
    result = await fetch_open_orders(tools)
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_open_orders_with_tool():
    mock_tool = MagicMock()
    mock_tool.name = "warehouseorder_tool"
    mock_tool.arun = AsyncMock(return_value=json.dumps({
        "value": [
            {"Batch": "B001", "ConfirmedQuantity": 50.0},
            {"Batch": "B001", "ConfirmedQuantity": 25.0},
            {"Batch": "B002", "ConfirmedQuantity": 10.0},
        ]
    }))
    result = await fetch_open_orders([mock_tool])
    assert result["B001"] == 75.0
    assert result["B002"] == 10.0


@pytest.mark.asyncio
async def test_scan_at_risk_batches_no_tools():
    with pytest.raises(RuntimeError):
        await scan_at_risk_batches(tools=[])


@pytest.mark.asyncio
async def test_scan_at_risk_batches_with_tools():
    item = _make_batch_item(sled_days=20)
    mock_batch_tool = MagicMock()
    mock_batch_tool.name = "api_batch_srv"
    mock_batch_tool.arun = AsyncMock(return_value=json.dumps({"value": [item]}))

    mock_order_tool = MagicMock()
    mock_order_tool.name = "warehouseorder_tool"
    mock_order_tool.arun = AsyncMock(return_value=json.dumps({"value": []}))

    records, exceptions = await scan_at_risk_batches(
        tools=[mock_batch_tool, mock_order_tool],
        risk_horizon_days=60,
    )
    assert len(records) >= 1
    assert records[0].batch_number == "BATCH001"
