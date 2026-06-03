"""Configurable parameters for the Batch Expiry Risk Management Agent.

All values are plain Python constants — adjust at deployment without code changes.
These must NEVER be decorated with @agent_config (that decorator is only for temperature).
"""

# --- Scan horizons ---
RISK_HORIZON_DAYS: int = 60        # Days ahead to scan for expiring batches
DEMAND_HORIZON_DAYS: int = 90      # IBP forecast window for consumption projection

# --- Residual quantity thresholds (batch exclusion from scan) ---
RESIDUAL_QTY_THRESHOLD_PCT: float = 0.10  # 10% of batch qty
RESIDUAL_QTY_ABSOLUTE: float = 50.0       # Minimum absolute residual units

# --- Risk quantity filter ---
MIN_RISK_QTY: float = 0.0          # Minimum net risk qty to include (0 = flag all)

# --- Risk score ---
MIN_SCORE_THRESHOLD: int = 20      # Minimum score to surface in report

# --- Score weights (must sum to 100) ---
W_EXPIRY: int = 40    # Days-to-expiry urgency
W_EXPOSURE: int = 30  # Risk qty as % of total SKU stock
W_VALUE: int = 20     # Financial exposure (unit_value × risk_qty)
W_BIN: int = 10       # Bin velocity class (C = highest risk)

# --- Redistribution action ---
MIN_SHELF_LIFE_POST_TRANSFER_DAYS: int = 14

# --- Channel reallocation action ---
TRANSFER_BUFFER_DAYS: int = 5

# --- Markdown action ---
MARKDOWN_ENABLED: bool = True
MARKDOWN_TRIGGER_DAYS: int = 30
MARKDOWN_MIN_QTY: float = 1.0
MD_TIER_1: int = 15   # ≤30 days
MD_TIER_2: int = 30   # ≤14 days
MD_TIER_3: int = 50   # ≤7 days

# --- Return to vendor action ---
RTV_MIN_DAYS_REMAINING: int = 21
RTV_ESCALATION_THRESHOLD: float = 5000.0

# --- Data freshness ---
IBP_DATA_FRESHNESS_HOURS: int = 24

# --- Hazmat ---
HAZMAT_EXCLUDE: bool = True

# --- Report ---
CURRENCY: str = "USD"
