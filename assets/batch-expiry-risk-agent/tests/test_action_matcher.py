"""Unit tests for action_matcher.py."""

import json
import sys
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from action_matcher import match_actions, _check_markdown, _markdown_tier
from models import BatchRecord, RiskResult
import config


def _make_batch(
    days_to_expiry=30,
    unit_value=100.0,
    bin_class="C",
    hazmat=False,
    temperature_zone="AMBIENT",
    qty_on_hand=200.0,
):
    sled = date.today() + timedelta(days=days_to_expiry)
    return BatchRecord(
        batch_number="B001",
        material="MAT001",
        description="Test Material",
        plant="1000",
        storage_location="WH01",
        bin="BIN-C1",
        qty_on_hand=qty_on_hand,
        qty_on_open_orders=50.0,
        sled=sled,
        days_to_expiry=days_to_expiry,
        unit_value=unit_value,
        unit_of_measure="EA",
        bin_velocity_class=bin_class,
        temperature_zone=temperature_zone,
        hazmat_flag=hazmat,
    )


def _make_risk(risk_qty=100.0, ibp_demand_per_day=1.0):
    return RiskResult(
        batch_number="B001",
        net_risk_qty=risk_qty,
        projected_consumption=0.0,
        risk_qty=risk_qty,
        ibp_demand_per_day=ibp_demand_per_day,
        ibp_data_stale=False,
        ibp_data_age_hours=0.0,
    )


def test_markdown_tier_30_days():
    assert _markdown_tier(30) == config.MD_TIER_1


def test_markdown_tier_14_days():
    assert _markdown_tier(14) == config.MD_TIER_2


def test_markdown_tier_7_days():
    assert _markdown_tier(7) == config.MD_TIER_3


def test_markdown_tier_1_day():
    assert _markdown_tier(1) == config.MD_TIER_3


def test_check_markdown_eligible():
    batch = _make_batch(days_to_expiry=20)
    risk = _make_risk(risk_qty=50.0)
    result = _check_markdown(batch, risk)
    assert result is not None
    assert result.action_type == 3
    assert "markdown" in result.action_label.lower() or "Markdown" in result.action_label


def test_check_markdown_not_eligible_too_far():
    batch = _make_batch(days_to_expiry=45)
    risk = _make_risk(risk_qty=50.0)
    result = _check_markdown(batch, risk)
    assert result is None


def test_check_markdown_not_eligible_min_qty():
    batch = _make_batch(days_to_expiry=10)
    risk = _make_risk(risk_qty=0.5)  # Below MARKDOWN_MIN_QTY=1
    result = _check_markdown(batch, risk)
    assert result is None


@pytest.mark.asyncio
async def test_match_actions_hazmat_excluded():
    batch = _make_batch(hazmat=True)
    risk = _make_risk()
    actions = await match_actions(batch, risk, tools=[])
    assert len(actions) == 1
    assert actions[0].action_type == 5
    assert "hazmat" in actions[0].action_label.lower() or "Hazmat" in actions[0].action_label


@pytest.mark.asyncio
async def test_match_actions_rtv_not_eligible_too_soon():
    batch = _make_batch(days_to_expiry=10)  # < RTV_MIN_DAYS_REMAINING=21
    risk = _make_risk(risk_qty=100.0)
    # With days_to_expiry=10, RTV not eligible; markdown should appear (≤30 days)
    actions = await match_actions(batch, risk, tools=[])
    action_types = [a.action_type for a in actions]
    assert 4 not in action_types  # RTV must not appear


@pytest.mark.asyncio
async def test_match_actions_disposal_last_resort_no_tools():
    batch = _make_batch(days_to_expiry=5)  # Very urgent — only disposal
    risk = _make_risk(risk_qty=100.0)
    # Only markdown and disposal could apply; no bin tools
    actions = await match_actions(batch, risk, tools=[])
    action_types = [a.action_type for a in actions]
    assert len(actions) > 0


@pytest.mark.asyncio
async def test_match_actions_priority_order():
    """Actions should always be returned with lower action_type numbers first."""
    batch = _make_batch(days_to_expiry=25)
    risk = _make_risk(risk_qty=100.0, ibp_demand_per_day=2.0)
    actions = await match_actions(batch, risk, tools=[])
    if len(actions) > 1:
        for i in range(len(actions) - 1):
            assert actions[i].action_type <= actions[i + 1].action_type


@pytest.mark.asyncio
async def test_match_actions_rtv_eligible_with_agreement():
    batch = _make_batch(days_to_expiry=30, unit_value=100.0)
    risk = _make_risk(risk_qty=100.0)

    mock_rtv_tool = MagicMock()
    mock_rtv_tool.name = "returnsinspection_tool"
    mock_rtv_tool.arun = AsyncMock(return_value=json.dumps({
        "value": [{"ReturnAgreementId": "RA001", "Material": "MAT001"}]
    }))

    actions = await match_actions(batch, risk, tools=[mock_rtv_tool])
    action_types = [a.action_type for a in actions]
    assert 4 in action_types


@pytest.mark.asyncio
async def test_redistribution_temperature_incompatible_skipped():
    """Bins with incompatible temperature zone must never be recommended."""
    batch = _make_batch(temperature_zone="COLD", bin_class="C")
    risk = _make_risk()

    mock_bin_tool = MagicMock()
    mock_bin_tool.name = "storagebin_tool"
    # Candidate bin has AMBIENT zone — incompatible with COLD batch
    mock_bin_tool.arun = AsyncMock(return_value=json.dumps({
        "value": [{
            "StorageBin": "BIN-A2",
            "VelocityClass": "A",
            "TemperatureCondition": "AMBIENT",
        }]
    }))

    actions = await match_actions(batch, risk, tools=[mock_bin_tool])
    action_types = [a.action_type for a in actions]
    assert 1 not in action_types  # Redistribution must not appear


@pytest.mark.asyncio
async def test_rtv_escalation_no_agreement_high_value():
    batch = _make_batch(days_to_expiry=30, unit_value=100.0)
    risk = _make_risk(risk_qty=60.0)  # 6000 USD > threshold 5000

    mock_rtv_tool = MagicMock()
    mock_rtv_tool.name = "returnsinspection_tool"
    mock_rtv_tool.arun = AsyncMock(return_value=json.dumps({"value": []}))  # No agreement

    actions = await match_actions(batch, risk, tools=[mock_rtv_tool])
    action_types = [a.action_type for a in actions]
    # Should still recommend action type 4 (escalation) for high value
    assert 4 in action_types
    escalation_action = next(a for a in actions if a.action_type == 4)
    assert "escalation" in escalation_action.action_label.lower() or "Negotiation" in escalation_action.action_label
