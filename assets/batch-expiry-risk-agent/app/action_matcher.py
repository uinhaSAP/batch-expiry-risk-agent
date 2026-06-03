"""Action matcher — Step 4 of the expiry risk scan pipeline.

Evaluates all enabled action types in priority order for each at-risk batch
and generates draft artefacts where applicable.

Hard constraints enforced here (ConstraintViolationError raised if violated):
- NEVER recommend redistribution to temperature-incompatible bin
- NEVER recommend RTV if days_to_expiry < RTV_MIN_DAYS_REMAINING
- If HAZMAT_EXCLUDE = True and batch is hazmat: exclude Actions 1, 2, 4
- NEVER include PII of warehouse staff in any output
"""

import json
import logging
from typing import Any

from models import ActionRecommendation, BatchRecord, ConstraintViolationError, RiskResult
from config import (
    HAZMAT_EXCLUDE,
    MARKDOWN_ENABLED,
    MARKDOWN_MIN_QTY,
    MARKDOWN_TRIGGER_DAYS,
    MD_TIER_1,
    MD_TIER_2,
    MD_TIER_3,
    MIN_SHELF_LIFE_POST_TRANSFER_DAYS,
    RTV_ESCALATION_THRESHOLD,
    RTV_MIN_DAYS_REMAINING,
    TRANSFER_BUFFER_DAYS,
    CURRENCY,
)

logger = logging.getLogger(__name__)


def _find_tool(tools: list[Any], keywords: list[str]) -> Any | None:
    for kw in keywords:
        for tool in tools:
            if kw.lower() in tool.name.lower():
                return tool
    return None


def _markdown_tier(days_to_expiry: int) -> int:
    """Return the appropriate markdown percentage for days to expiry."""
    if days_to_expiry <= 7:
        return MD_TIER_3
    if days_to_expiry <= 14:
        return MD_TIER_2
    return MD_TIER_1


async def _check_redistribution(
    batch: BatchRecord,
    tools: list[Any],
) -> ActionRecommendation | None:
    """Action 1: Check if redistribution to a higher-velocity bin is feasible."""
    if HAZMAT_EXCLUDE and batch.hazmat_flag:
        return None

    if batch.days_to_expiry < MIN_SHELF_LIFE_POST_TRANSFER_DAYS:
        return None

    bin_tool = _find_tool(tools, ["storagebin", "storage_bin", "warehousebin", "fixedbin"])
    if bin_tool is None:
        return None

    try:
        response = await bin_tool.arun({"top": 100, "plant": batch.plant})
        data = json.loads(response) if isinstance(response, str) else response
        items = (
            data.get("value", []) if isinstance(data, dict) and "value" in data
            else data if isinstance(data, list) else []
        )

        for candidate in items:
            if not isinstance(candidate, dict):
                continue

            candidate_velocity = (candidate.get("VelocityClass") or candidate.get("velocity_class") or "C").upper()
            candidate_temp = (candidate.get("TemperatureCondition") or candidate.get("temperature_zone") or "AMBIENT").upper()
            candidate_bin = candidate.get("StorageBin") or candidate.get("bin") or ""

            if candidate_bin == batch.bin:
                continue  # Same bin

            # Hard constraint: temperature zone must be compatible
            if candidate_temp != batch.temperature_zone.upper():
                # Violation would occur — skip silently (do not surface incompatible recommendation)
                continue

            # Check velocity improvement
            velocity_rank = {"A": 0, "B": 1, "C": 2}
            current_rank = velocity_rank.get(batch.bin_velocity_class.upper(), 2)
            candidate_rank = velocity_rank.get(candidate_velocity, 2)

            if candidate_rank < current_rank:  # Lower rank = higher velocity
                return ActionRecommendation(
                    action_type=1,
                    action_label="Redistribution to High-Velocity Bin",
                    description=(
                        f"DRAFT — Move {batch.risk.risk_qty if hasattr(batch, 'risk') else 'at-risk qty'} {batch.unit_of_measure} "
                        f"from bin {batch.bin} (velocity {batch.bin_velocity_class}) "
                        f"to bin {candidate_bin} (velocity {candidate_velocity}). "
                        f"Remaining shelf life after transfer: {batch.days_to_expiry} days ≥ {MIN_SHELF_LIFE_POST_TRANSFER_DAYS} day minimum. "
                        f"Temperature zone compatible: {candidate_temp}."
                    ),
                    draft_artefact=(
                        f"DRAFT TRANSFER PROPOSAL — REQUIRES HUMAN APPROVAL\n"
                        f"Batch: {batch.batch_number} | Material: {batch.material}\n"
                        f"From: Plant {batch.plant} | Location {batch.storage_location} | Bin {batch.bin}\n"
                        f"To: Bin {candidate_bin} (velocity class {candidate_velocity})\n"
                        f"Quantity: [planner to confirm risk qty]\n"
                        f"SLED: {batch.sled} ({batch.days_to_expiry} days remaining)\n"
                        f"Reason: FEFO redistribution to increase pick velocity before expiry."
                    ),
                    requires_human_approval=True,
                )
    except Exception:
        logger.warning("Bin redistribution check failed for batch %s", batch.batch_number, exc_info=True)

    return None


async def _check_channel_reallocation(
    batch: BatchRecord,
    risk: RiskResult,
    tools: list[Any],
) -> ActionRecommendation | None:
    """Action 2: Check if channel reallocation to a higher-demand plant/DC is feasible."""
    if HAZMAT_EXCLUDE and batch.hazmat_flag:
        return None

    # Simplified: if IBP demand is very low at current plant, flag for reallocation
    if risk.ibp_demand_per_day > 0 and batch.days_to_expiry > TRANSFER_BUFFER_DAYS + 5:
        return ActionRecommendation(
            action_type=2,
            action_label="Channel Reallocation",
            description=(
                f"Consider transferring to a higher-velocity sales channel or DC. "
                f"Current IBP demand rate: {risk.ibp_demand_per_day:.1f} units/day at plant {batch.plant}. "
                f"Transfer must complete {TRANSFER_BUFFER_DAYS}+ days before SLED."
            ),
            draft_artefact=(
                f"DRAFT CHANNEL REALLOCATION PROPOSAL — REQUIRES HUMAN APPROVAL\n"
                f"Batch: {batch.batch_number} | Material: {batch.material} | {batch.description}\n"
                f"Source: Plant {batch.plant} | {batch.storage_location} | Bin {batch.bin}\n"
                f"Destination: [Planner to select high-demand DC/plant from IBP demand view]\n"
                f"Quantity: [Planner to confirm risk qty]\n"
                f"SLED: {batch.sled} ({batch.days_to_expiry} days) — transfer buffer: {TRANSFER_BUFFER_DAYS} days\n"
                f"IBP Reference: Consensus demand at source = {risk.ibp_demand_per_day:.2f} units/day"
            ),
            requires_human_approval=True,
        )
    return None


def _check_markdown(
    batch: BatchRecord,
    risk: RiskResult,
) -> ActionRecommendation | None:
    """Action 3: Check if markdown / price promotion is eligible."""
    if not MARKDOWN_ENABLED:
        return None
    if batch.days_to_expiry > MARKDOWN_TRIGGER_DAYS:
        return None
    if risk.risk_qty <= MARKDOWN_MIN_QTY:
        return None

    pct = _markdown_tier(batch.days_to_expiry)
    return ActionRecommendation(
        action_type=3,
        action_label=f"Markdown {pct}% Price Promotion",
        description=(
            f"SLED in {batch.days_to_expiry} days triggers {pct}% markdown tier. "
            f"Risk qty: {risk.risk_qty:.0f} {batch.unit_of_measure}. "
            f"Estimated recovery: {risk.risk_qty * batch.unit_value * (1 - pct / 100):.2f} {CURRENCY}."
        ),
        draft_artefact=(
            f"DRAFT MARKDOWN EVENT — REQUIRES PRICING TEAM APPROVAL\n"
            f"Material: {batch.material} | {batch.description}\n"
            f"Batch: {batch.batch_number} | Plant: {batch.plant}\n"
            f"SLED: {batch.sled} | Days remaining: {batch.days_to_expiry}\n"
            f"Proposed markdown: {pct}% (Tier: {'≤7d' if batch.days_to_expiry <= 7 else '≤14d' if batch.days_to_expiry <= 14 else '≤30d'})\n"
            f"Quantity: {risk.risk_qty:.0f} {batch.unit_of_measure}\n"
            f"Current unit value: {batch.unit_value:.2f} {CURRENCY}\n"
            f"Proposed sell price: {batch.unit_value * (1 - pct / 100):.2f} {CURRENCY}\n"
            f"Estimated revenue recovery: {risk.risk_qty * batch.unit_value * (1 - pct / 100):.2f} {CURRENCY}\n"
            f"Event description: Expiry markdown — clear stock before {batch.sled}."
        ),
        requires_human_approval=True,
    )


async def _check_rtv(
    batch: BatchRecord,
    risk: RiskResult,
    tools: list[Any],
) -> ActionRecommendation | None:
    """Action 4: Check if return-to-vendor is eligible."""
    if HAZMAT_EXCLUDE and batch.hazmat_flag:
        return None

    # Hard constraint: must have sufficient days remaining for vendor to process
    if batch.days_to_expiry < RTV_MIN_DAYS_REMAINING:
        # Do not raise ConstraintViolationError here — just skip; disposal is fallback
        return None

    rtv_tool = _find_tool(tools, ["returns_inspection", "returnsinspection", "return_supplier", "rtv"])
    has_agreement = False

    if rtv_tool is not None:
        try:
            response = await rtv_tool.arun({"top": 100, "material": batch.material})
            data = json.loads(response) if isinstance(response, str) else response
            items = (
                data.get("value", []) if isinstance(data, dict) and "value" in data
                else data if isinstance(data, list) else []
            )
            has_agreement = len(items) > 0
        except Exception:
            logger.warning("RTV agreement check failed for batch %s", batch.batch_number, exc_info=True)

    financial_exposure = batch.unit_value * risk.risk_qty

    if not has_agreement:
        if financial_exposure >= RTV_ESCALATION_THRESHOLD:
            return ActionRecommendation(
                action_type=4,
                action_label="RTV — Manual Negotiation Required",
                description=(
                    f"No active return agreement found. Financial exposure "
                    f"{financial_exposure:.2f} {CURRENCY} ≥ escalation threshold "
                    f"{RTV_ESCALATION_THRESHOLD:.2f} {CURRENCY} — escalate for manual RTV negotiation."
                ),
                draft_artefact=(
                    f"DRAFT RTV ESCALATION REQUEST — REQUIRES PROCUREMENT APPROVAL\n"
                    f"Material: {batch.material} | {batch.description}\n"
                    f"Batch: {batch.batch_number} | Plant: {batch.plant}\n"
                    f"Quantity: {risk.risk_qty:.0f} {batch.unit_of_measure}\n"
                    f"Financial exposure: {financial_exposure:.2f} {CURRENCY}\n"
                    f"SLED: {batch.sled} ({batch.days_to_expiry} days remaining)\n"
                    f"Action: No standard return agreement exists. Manual negotiation with vendor required.\n"
                    f"Vendor: [Procurement to identify from purchasing info record]\n"
                    f"Urgency: Must complete return authorisation within {batch.days_to_expiry - RTV_MIN_DAYS_REMAINING} days."
                ),
                requires_human_approval=True,
            )
        return None

    return ActionRecommendation(
        action_type=4,
        action_label="Return to Vendor (RTV)",
        description=(
            f"Active return agreement found. {risk.risk_qty:.0f} {batch.unit_of_measure} "
            f"eligible for return. {batch.days_to_expiry} days remaining ≥ {RTV_MIN_DAYS_REMAINING} day minimum."
        ),
        draft_artefact=(
            f"DRAFT RTV REQUEST — REQUIRES HUMAN APPROVAL\n"
            f"Vendor: [from purchasing info record for material {batch.material}]\n"
            f"PO Reference: [planner to confirm]\n"
            f"Material: {batch.material} | {batch.description}\n"
            f"Batch: {batch.batch_number} | Plant: {batch.plant} | Location: {batch.storage_location}\n"
            f"Quantity: {risk.risk_qty:.0f} {batch.unit_of_measure}\n"
            f"SLED: {batch.sled} ({batch.days_to_expiry} days remaining)\n"
            f"Reason code: EXPIRY_RISK\n"
            f"Proposed return date: [within {batch.days_to_expiry - RTV_MIN_DAYS_REMAINING} days]\n"
            f"Financial exposure avoided: {financial_exposure:.2f} {CURRENCY}"
        ),
        requires_human_approval=True,
    )


def _disposal_recommendation(
    batch: BatchRecord,
    risk: RiskResult,
    reason: str,
) -> ActionRecommendation:
    """Action 5: Quality hold / disposal recommendation (last resort)."""
    write_off_estimate = batch.unit_value * risk.risk_qty
    return ActionRecommendation(
        action_type=5,
        action_label="Quality Hold / Disposal",
        description=(
            f"{reason} "
            f"Estimated write-off: {write_off_estimate:.2f} {CURRENCY}. "
            f"QM notification required."
        ),
        draft_artefact=(
            f"DRAFT QM NOTIFICATION — REQUIRES QUALITY TEAM APPROVAL\n"
            f"Batch: {batch.batch_number} | Material: {batch.material} | {batch.description}\n"
            f"Plant: {batch.plant} | Location: {batch.storage_location} | Bin: {batch.bin}\n"
            f"Quantity at risk: {risk.risk_qty:.0f} {batch.unit_of_measure}\n"
            f"SLED: {batch.sled} ({batch.days_to_expiry} days remaining)\n"
            f"Reason: {reason}\n"
            f"Estimated write-off value: {write_off_estimate:.2f} {CURRENCY}\n"
            f"Recommended action: Place on quality hold; initiate disposal per site procedures.\n"
            f"NOTE: This is a draft — quality team must post the QM notification."
        ),
        requires_human_approval=True,
    )


async def match_actions(
    batch: BatchRecord,
    risk: RiskResult,
    tools: list[Any],
) -> list[ActionRecommendation]:
    """Evaluate all enabled action types for a batch in priority order.

    Returns a list of recommended actions (may be empty only if no action applies,
    in which case the caller should treat this as disposal-flagged).
    """
    actions: list[ActionRecommendation] = []

    # Hazmat: skip all standard actions, flag for dedicated handling
    if HAZMAT_EXCLUDE and batch.hazmat_flag:
        actions.append(ActionRecommendation(
            action_type=5,
            action_label="Hazmat — Dedicated Handling Required",
            description=(
                f"Batch flagged as hazardous material. Standard redistribution and RTV actions excluded. "
                f"Route to dedicated hazmat disposal/handling process."
            ),
            draft_artefact=None,
            requires_human_approval=True,
        ))
        return actions

    # Action 1: Redistribution
    action1 = await _check_redistribution(batch, tools)
    if action1:
        actions.append(action1)

    # Action 2: Channel reallocation
    action2 = await _check_channel_reallocation(batch, risk, tools)
    if action2:
        actions.append(action2)

    # Action 3: Markdown
    action3 = _check_markdown(batch, risk)
    if action3:
        actions.append(action3)

    # Action 4: RTV
    action4 = await _check_rtv(batch, risk, tools)
    if action4:
        actions.append(action4)

    # Action 5: Disposal — last resort if no other action found, or if days_to_expiry < RTV_MIN_DAYS_REMAINING
    if not actions:
        reason = (
            f"No other action feasible. "
            if batch.days_to_expiry >= RTV_MIN_DAYS_REMAINING
            else f"Insufficient time for RTV (days_to_expiry={batch.days_to_expiry} < minimum={RTV_MIN_DAYS_REMAINING}). "
        )
        actions.append(_disposal_recommendation(batch, risk, reason))

    return actions
