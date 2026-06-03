"""Risk calculator — Step 2 of the expiry risk scan pipeline.

Calculates net risk quantities and IBP-projected consumption for each batch.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from models import BatchRecord, RiskResult
from config import DEMAND_HORIZON_DAYS, IBP_DATA_FRESHNESS_HOURS

logger = logging.getLogger(__name__)


def _find_tool(tools: list[Any], keywords: list[str]) -> Any | None:
    for kw in keywords:
        for tool in tools:
            if kw.lower() in tool.name.lower():
                return tool
    return None


async def _fetch_ibp_demand(
    tools: list[Any],
    material: str,
    plant: str,
    demand_horizon_days: int,
) -> tuple[float, float]:
    """Fetch IBP consensus demand forecast for a SKU/location.

    Returns:
        Tuple of (total_demand_for_horizon, data_age_hours).
        data_age_hours is 0 if freshness metadata unavailable.
    """
    forecast_tool = _find_tool(
        tools,
        ["forecast", "demand", "ibp", "keyfigure", "key_figure", "consensus"]
    )
    if forecast_tool is None:
        logger.warning(
            "No IBP forecast tool found for material=%s plant=%s — demand will be 0", material, plant
        )
        return 0.0, 0.0

    try:
        params: dict = {
            "top": 100,
            "material": material,
            "plant": plant,
        }
        response = await forecast_tool.arun(params)
        data = json.loads(response) if isinstance(response, str) else response

        # Extract total demand
        items = (
            data.get("value", [])
            if isinstance(data, dict) and "value" in data
            else data if isinstance(data, list)
            else []
        )
        total_demand = 0.0
        data_age_hours = 0.0

        for item in items:
            if not isinstance(item, dict):
                continue
            qty = float(
                item.get("KeyFigureValue")
                or item.get("ForecastQuantity")
                or item.get("ConsensusQty")
                or item.get("demand")
                or 0
            )
            total_demand += qty

            # Try to determine data freshness from metadata
            last_updated_raw = item.get("LastUpdatedAt") or item.get("UpdatedAt") or item.get("Timestamp")
            if last_updated_raw and data_age_hours == 0.0:
                try:
                    last_updated = datetime.fromisoformat(str(last_updated_raw).replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    data_age_hours = (now - last_updated).total_seconds() / 3600
                except Exception:
                    pass

        return total_demand, data_age_hours

    except Exception:
        logger.exception(
            "IBP demand fetch failed for material=%s plant=%s", material, plant
        )
        return 0.0, 0.0


async def calculate_net_risk(
    batch: BatchRecord,
    tools: list[Any],
    demand_horizon_days: int = DEMAND_HORIZON_DAYS,
    ibp_freshness_hours: int = IBP_DATA_FRESHNESS_HOURS,
) -> RiskResult:
    """Calculate net risk quantity and consumption projection for a batch.

    Args:
        batch: The batch record to evaluate.
        tools: Available MCP tools.
        demand_horizon_days: IBP forecast window (days).
        ibp_freshness_hours: Maximum acceptable IBP data age.

    Returns:
        RiskResult with all computed fields.
    """
    net_risk_qty = max(0.0, batch.qty_on_hand - batch.qty_on_open_orders)

    total_demand, data_age_hours = await _fetch_ibp_demand(
        tools, batch.material, batch.plant, demand_horizon_days
    )

    ibp_data_stale = (
        data_age_hours > ibp_freshness_hours if data_age_hours > 0 else False
    )
    if ibp_data_stale:
        logger.warning(
            "IBP data stale for batch=%s material=%s: age=%.1f h > threshold=%d h",
            batch.batch_number,
            batch.material,
            data_age_hours,
            ibp_freshness_hours,
        )

    # Consumption rate = total forecast / horizon
    demand_per_day = total_demand / demand_horizon_days if demand_horizon_days > 0 else 0.0
    projected_consumption = demand_per_day * max(0, batch.days_to_expiry)

    risk_qty = max(0.0, net_risk_qty - projected_consumption)

    return RiskResult(
        batch_number=batch.batch_number,
        net_risk_qty=net_risk_qty,
        projected_consumption=projected_consumption,
        risk_qty=risk_qty,
        ibp_demand_per_day=demand_per_day,
        ibp_data_stale=ibp_data_stale,
        ibp_data_age_hours=data_age_hours,
    )
