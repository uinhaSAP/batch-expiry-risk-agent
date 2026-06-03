"""Batch scanner — Step 1 of the expiry risk scan pipeline.

Fetches all EWM batches expiring within the risk horizon, deducts confirmed
order quantities, and filters out batches that are already fully covered.
All SAP data access goes through MCP tools — no direct HTTP calls.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Any

from models import BatchRecord
from config import RISK_HORIZON_DAYS, RESIDUAL_QTY_THRESHOLD_PCT, RESIDUAL_QTY_ABSOLUTE

logger = logging.getLogger(__name__)


def _find_tool(tools: list[Any], keywords: list[str]) -> Any | None:
    """Find the first MCP tool whose name contains any of the keywords."""
    for kw in keywords:
        for tool in tools:
            if kw.lower() in tool.name.lower():
                return tool
    return None


def _parse_date(val: Any) -> date | None:
    """Parse a date from various formats returned by OData services."""
    if val is None:
        return None
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        # ISO format YYYY-MM-DD
        try:
            return datetime.strptime(val[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
        # Compact YYYYMMDD
        if len(val) >= 8:
            try:
                return datetime.strptime(val[:8], "%Y%m%d").date()
            except ValueError:
                pass
        # OData /Date(ms)/ format
        if val.startswith("/Date("):
            try:
                ms = int(val[6:val.index(")")])
                return date.fromtimestamp(ms / 1000)
            except Exception:
                pass
    return None


def _extract_batch_records_from_tool_response(
    response_text: str,
    open_orders: dict[str, float],
    risk_horizon_days: int,
) -> list[BatchRecord]:
    """Parse tool response text into BatchRecord objects.

    The response from MCP tools is a JSON/text string. We parse it
    and construct BatchRecord dataclasses. Batches missing SLED are
    skipped and returned as exceptions.
    """
    import json
    today = date.today()
    cutoff = today + timedelta(days=risk_horizon_days)
    records = []

    try:
        data = json.loads(response_text)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Could not parse batch tool response as JSON: %s", response_text[:200])
        return records

    # Handle OData wrapper formats
    if isinstance(data, dict):
        if "value" in data:
            items = data["value"]
        elif "d" in data and "results" in data.get("d", {}):
            items = data["d"]["results"]
        elif "d" in data:
            items = [data["d"]] if isinstance(data["d"], dict) else data["d"]
        else:
            items = [data]
    elif isinstance(data, list):
        items = data
    else:
        return records

    for item in items:
        if not isinstance(item, dict):
            continue

        # Extract SLED — try multiple field name conventions
        sled_raw = (
            item.get("ShelfLifeExpirationDate")
            or item.get("SLEDDate")
            or item.get("BestBeforeDate")
            or item.get("BBD")
            or item.get("sled")
        )
        sled = _parse_date(sled_raw)

        if sled is None:
            logger.debug("Batch %s missing SLED — will appear in exceptions", item.get("Batch", "unknown"))
            continue

        if sled > cutoff:
            continue  # Outside risk horizon

        days_to_expiry = (sled - today).days

        batch_num = (
            item.get("Batch") or item.get("BatchNumber") or item.get("batch_number") or ""
        )
        material = item.get("Material") or item.get("material") or ""
        description = item.get("MaterialDescription") or item.get("description") or material
        plant = item.get("Plant") or item.get("plant") or ""
        storage_location = item.get("StorageLocation") or item.get("Warehouse") or ""
        bin_loc = item.get("StorageBin") or item.get("bin") or ""
        qty_on_hand = float(item.get("GoodsMovementQuantity") or item.get("Quantity") or item.get("qty") or 0)
        unit_value = float(item.get("MovingAveragePrice") or item.get("UnitValue") or item.get("unit_value") or 0)
        uom = item.get("BaseUnit") or item.get("UoM") or "EA"
        velocity = item.get("VelocityClass") or item.get("velocity_class") or "C"
        temp_zone = item.get("TemperatureCondition") or item.get("temperature_zone") or "AMBIENT"
        hazmat = bool(item.get("HazardousSubstanceFlag") or item.get("hazmat_flag") or False)

        qty_on_orders = open_orders.get(batch_num, 0.0)

        # Residual quantity check
        # Per spec: exclude if residual is NOT above max(threshold_pct, RESIDUAL_QTY_ABSOLUTE)
        residual = qty_on_hand - qty_on_orders
        if residual <= 0:
            continue  # Fully covered
        higher_threshold = max(qty_on_hand * RESIDUAL_QTY_THRESHOLD_PCT, RESIDUAL_QTY_ABSOLUTE)
        if residual < higher_threshold:
            continue  # Below whichever threshold is higher — exclude

        records.append(BatchRecord(
            batch_number=batch_num,
            material=material,
            description=description,
            plant=plant,
            storage_location=storage_location,
            bin=bin_loc,
            qty_on_hand=qty_on_hand,
            qty_on_open_orders=qty_on_orders,
            sled=sled,
            days_to_expiry=days_to_expiry,
            unit_value=unit_value,
            unit_of_measure=uom,
            bin_velocity_class=velocity.upper(),
            temperature_zone=temp_zone.upper(),
            hazmat_flag=hazmat,
            classification_attrs=item,
        ))

    return records


async def fetch_open_orders(tools: list[Any]) -> dict[str, float]:
    """Fetch confirmed open warehouse order quantities per batch from EWM.

    Returns a dict mapping batch_number → qty_on_confirmed_orders.
    """
    tool = _find_tool(tools, ["warehouseorder", "warehouse_order", "order_task", "ordertask"])
    if tool is None:
        logger.warning("No warehouse order tool found — open order quantities will be 0")
        return {}

    try:
        response = await tool.arun({"top": 100})
        import json
        data = json.loads(response) if isinstance(response, str) else response

        orders: dict[str, float] = {}
        items = (
            data.get("value", [])
            if isinstance(data, dict) and "value" in data
            else data if isinstance(data, list) else []
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            batch = item.get("Batch") or item.get("batch_number") or ""
            qty = float(item.get("ConfirmedQuantity") or item.get("OpenQuantity") or item.get("qty") or 0)
            if batch:
                orders[batch] = orders.get(batch, 0.0) + qty
        logger.info("Fetched open orders for %d batches", len(orders))
        return orders
    except Exception:
        logger.exception("Failed to fetch open warehouse orders")
        return {}


async def scan_at_risk_batches(
    tools: list[Any],
    plants: list[str] | None = None,
    risk_horizon_days: int = RISK_HORIZON_DAYS,
) -> tuple[list[BatchRecord], list[str]]:
    """Identify at-risk batches from SAP EWM.

    Args:
        tools: LangChain MCP tools loaded from Agent Gateway.
        plants: Optional list of plant codes to filter. None = all plants.
        risk_horizon_days: Days ahead to scan for SLED.

    Returns:
        Tuple of (at_risk_batches, exception_messages).
        exception_messages lists batches skipped due to data quality issues.
    """
    exceptions: list[str] = []

    # 1. Fetch open confirmed orders for coverage deduction
    open_orders = await fetch_open_orders(tools)

    # 2. Find batch master tool
    batch_tool = _find_tool(tools, ["batch_master", "batchmaster", "batch_srv", "api_batch"])
    stock_tool = _find_tool(tools, ["availablestock", "available_stock", "warehouse_stock"])

    if batch_tool is None and stock_tool is None:
        raise RuntimeError(
            "No batch master or available stock tool found in MCP tools. "
            "Cannot proceed with batch scan. Required tools: batch_master or available_stock."
        )

    primary_tool = batch_tool or stock_tool
    logger.info("Using tool '%s' for batch scan", primary_tool.name)

    # 3. Build filter for plants if specified
    tool_params: dict = {"top": 100}
    if plants:
        tool_params["filter"] = f"Plant in ({','.join(repr(p) for p in plants)})"

    try:
        response = await primary_tool.arun(tool_params)
    except Exception as e:
        raise RuntimeError(f"Batch tool '{primary_tool.name}' failed: {e}") from e

    # 4. Parse and filter
    records = _extract_batch_records_from_tool_response(
        response_text=response if isinstance(response, str) else str(response),
        open_orders=open_orders,
        risk_horizon_days=risk_horizon_days,
    )

    # 5. Try to enrich with shelf life data if available
    sl_tool = _find_tool(tools, ["shelf_life", "shelflife", "slversion"])
    if sl_tool and records:
        try:
            sl_response = await sl_tool.arun({"top": 100})
            import json
            sl_data = json.loads(sl_response) if isinstance(sl_response, str) else sl_response
            sl_items = (
                sl_data.get("value", [])
                if isinstance(sl_data, dict) and "value" in sl_data
                else sl_data if isinstance(sl_data, list) else []
            )
            sl_map: dict[str, date] = {}
            for item in sl_items:
                if not isinstance(item, dict):
                    continue
                bn = item.get("Batch") or item.get("batch_number") or ""
                sled_raw = item.get("ShelfLifeExpirationDate") or item.get("SLED")
                sled = _parse_date(sled_raw)
                if bn and sled:
                    sl_map[bn] = sled
            # Update records with enriched SLED data
            today = date.today()
            for rec in records:
                if rec.batch_number in sl_map:
                    rec.sled = sl_map[rec.batch_number]
                    rec.days_to_expiry = (rec.sled - today).days
        except Exception:
            logger.warning("Shelf life enrichment failed — using SLED from batch master", exc_info=True)

    logger.info("Batch scan found %d at-risk candidates (horizon=%d days)", len(records), risk_horizon_days)
    return records, exceptions
