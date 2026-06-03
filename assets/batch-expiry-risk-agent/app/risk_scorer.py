"""Risk scorer — Step 3 of the expiry risk scan pipeline.

Assigns a 1-100 risk score to each batch using a configurable weighted formula.
"""

import logging

from models import BatchRecord, RiskResult
from config import (
    RISK_HORIZON_DAYS,
    W_EXPIRY,
    W_EXPOSURE,
    W_VALUE,
    W_BIN,
    MIN_SCORE_THRESHOLD,
)

logger = logging.getLogger(__name__)

# Bin velocity class risk multipliers
_BIN_VELOCITY_RISK = {
    "C": 1.0,  # Slow-moving — highest risk
    "B": 0.5,
    "A": 0.0,  # Fast-moving — lowest risk
}


def _normalise(value: float, max_value: float) -> float:
    """Normalise value to [0, 1] against a maximum. Returns 0 if max_value is 0."""
    if max_value <= 0:
        return 0.0
    return min(1.0, value / max_value)


def score_batch(
    batch: BatchRecord,
    risk: RiskResult,
    total_sku_stock: float,
    max_financial_exposure: float,
    risk_horizon_days: int = RISK_HORIZON_DAYS,
) -> tuple[int, str]:
    """Compute risk score and confidence level for a batch.

    Args:
        batch: Batch record.
        risk: Calculated risk result.
        total_sku_stock: Total on-hand stock for this SKU at this plant (for exposure %).
        max_financial_exposure: Maximum unit_value × risk_qty across all at-risk batches
                                in the current run (for financial normalisation).
        risk_horizon_days: Scan horizon used for expiry normalisation.

    Returns:
        Tuple of (score: int 1-100, confidence: str "High"/"Medium"/"Low").
    """
    # Component 1: Days-to-expiry urgency
    # Sooner = higher risk: (horizon - days_to_expiry) / horizon
    expiry_component = _normalise(
        risk_horizon_days - max(0, batch.days_to_expiry),
        risk_horizon_days,
    )

    # Component 2: Exposure as % of total SKU stock
    exposure_component = _normalise(risk.risk_qty, total_sku_stock)

    # Component 3: Financial exposure
    financial_exposure = batch.unit_value * risk.risk_qty
    financial_component = _normalise(financial_exposure, max_financial_exposure)

    # Component 4: Bin velocity class
    bin_component = _BIN_VELOCITY_RISK.get(batch.bin_velocity_class.upper(), 0.5)

    # Weighted sum → scale to 1-100
    raw_score = (
        expiry_component * W_EXPIRY
        + exposure_component * W_EXPOSURE
        + financial_component * W_VALUE
        + bin_component * W_BIN
    )
    # raw_score is in [0, 100] since weights sum to 100 and each component is in [0,1]
    score = max(1, min(100, round(raw_score)))

    # Confidence
    if risk.ibp_data_stale:
        confidence = "Low"
    elif risk.ibp_demand_per_day == 0.0:
        confidence = "Medium"  # No demand data available
    else:
        confidence = "High"

    logger.debug(
        "Scored batch=%s: expiry=%.2f exposure=%.2f financial=%.2f bin=%.2f → score=%d confidence=%s",
        batch.batch_number,
        expiry_component,
        exposure_component,
        financial_component,
        bin_component,
        score,
        confidence,
    )
    return score, confidence


def score_all_batches(
    batches_with_risks: list[tuple[BatchRecord, RiskResult]],
    sku_stock_map: dict[tuple[str, str], float],
    risk_horizon_days: int = RISK_HORIZON_DAYS,
    min_score_threshold: int = MIN_SCORE_THRESHOLD,
) -> list[tuple[BatchRecord, RiskResult, int, str]]:
    """Score all batches and filter by MIN_SCORE_THRESHOLD.

    Args:
        batches_with_risks: List of (BatchRecord, RiskResult) tuples.
        sku_stock_map: Maps (material, plant) → total on-hand stock.
        risk_horizon_days: Scan horizon.
        min_score_threshold: Batches below this score are suppressed.

    Returns:
        List of (BatchRecord, RiskResult, score, confidence) sorted by score desc.
    """
    if not batches_with_risks:
        return []

    # Pre-compute max financial exposure for normalisation
    max_financial = max(
        (b.unit_value * r.risk_qty for b, r in batches_with_risks),
        default=1.0,
    )
    if max_financial <= 0:
        max_financial = 1.0

    results = []
    suppressed = 0
    for batch, risk in batches_with_risks:
        total_sku = sku_stock_map.get((batch.material, batch.plant), batch.qty_on_hand)
        score, confidence = score_batch(
            batch=batch,
            risk=risk,
            total_sku_stock=total_sku,
            max_financial_exposure=max_financial,
            risk_horizon_days=risk_horizon_days,
        )
        if score < min_score_threshold:
            suppressed += 1
            continue
        results.append((batch, risk, score, confidence))

    # Sort descending by score
    results.sort(key=lambda x: x[2], reverse=True)

    logger.info(
        "Scoring complete: %d batches scored, %d suppressed (threshold=%d), %d surfaced",
        len(batches_with_risks),
        suppressed,
        min_score_threshold,
        len(results),
    )
    return results
