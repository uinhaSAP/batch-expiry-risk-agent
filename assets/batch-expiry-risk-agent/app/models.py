"""Data models for the Batch Expiry Risk Management Agent."""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class BatchRecord:
    """EWM batch master record with shelf life and stock information."""
    batch_number: str
    material: str
    description: str
    plant: str
    storage_location: str
    bin: str
    qty_on_hand: float
    qty_on_open_orders: float
    sled: date                          # Shelf life expiry date / best-before date
    days_to_expiry: int
    unit_value: float                   # Moving average price per base UoM
    unit_of_measure: str
    bin_velocity_class: str             # "A", "B", or "C"
    temperature_zone: str
    hazmat_flag: bool
    classification_attrs: dict = field(default_factory=dict)


@dataclass
class RiskResult:
    """Calculated risk quantities and forecast data for a batch."""
    batch_number: str
    net_risk_qty: float                 # qty_on_hand - qty_on_open_orders
    projected_consumption: float        # IBP-based consumption before expiry
    risk_qty: float                     # max(0, net_risk_qty - projected_consumption)
    ibp_demand_per_day: float
    ibp_data_stale: bool
    ibp_data_age_hours: float           # Age of IBP data at time of fetch


@dataclass
class ScoredBatch:
    """Batch with risk score and associated calculations."""
    batch: BatchRecord
    risk: RiskResult
    score: int                          # 1-100
    confidence: str                     # "High", "Medium", "Low"
    total_exposure: float               # unit_value × risk_qty in CURRENCY
    total_sku_stock: float              # Total on-hand stock for this SKU at this plant


@dataclass
class ActionRecommendation:
    """A single recommended action for an at-risk batch."""
    action_type: int                    # 1-5
    action_label: str
    description: str
    draft_artefact: Optional[str] = None     # Draft text for RTV/markdown/transfer
    requires_human_approval: bool = True


@dataclass
class FullBatchReport:
    """Complete report entry for one at-risk batch."""
    scored_batch: ScoredBatch
    actions: list[ActionRecommendation]


class ConstraintViolationError(Exception):
    """Raised when a hard constraint is violated during action matching."""
    pass
