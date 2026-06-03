# Specification: batch-expiry-risk-agent

> **Guidelines**: Read [guidelines.md](../guidelines.md) and [guidelines-agent.md](../guidelines-agent.md) before executing ANY tasks below. Follow all constraints described there throughout execution.

## Basic Setup

- [x] Read `product-requirements-document.md` and `intent.md` thoroughly before starting implementation
- [x] Bootstrap agent code in `assets/batch-expiry-risk-agent/` using skill `sap-agent-bootstrap` (invoke from inside `assets/batch-expiry-risk-agent/`, use copy commands — do NOT create files manually)
- [x] Install dependencies, validate the agent starts and responds at `/.well-known/agent.json`

## Agent Configuration

- [x] Set agent name to `batch-expiry-risk-agent` and description to "Proactive batch expiry risk management agent for SAP EWM + SAP IBP — identifies at-risk batches, scores financial risk, and recommends prioritised actions before inventory write-offs occur"
- [x] Define all configurable parameters as plain Python constants in `app/config.py` (NOT as `@agent_config` decorators):
  - `RISK_HORIZON_DAYS = 60` — days ahead to scan for expiring batches
  - `DEMAND_HORIZON_DAYS = 90` — IBP forecast window for consumption projection
  - `RESIDUAL_QTY_THRESHOLD_PCT = 0.10` — min residual percentage after confirmed orders to still flag
  - `RESIDUAL_QTY_ABSOLUTE = 50` — min absolute residual qty (units)
  - `MIN_RISK_QTY = 0` — min net risk qty to include
  - `MIN_SCORE_THRESHOLD = 20` — min risk score to surface in report
  - `W_EXPIRY = 40` — weight for days-to-expiry in risk score
  - `W_EXPOSURE = 30` — weight for risk qty as % of total SKU stock
  - `W_VALUE = 20` — weight for financial exposure (unit_value × risk_qty)
  - `W_BIN = 10` — weight for bin velocity class (C = highest risk)
  - `MIN_SHELF_LIFE_POST_TRANSFER_DAYS = 14`
  - `TRANSFER_BUFFER_DAYS = 5`
  - `MARKDOWN_ENABLED = True`
  - `MARKDOWN_TRIGGER_DAYS = 30`
  - `MARKDOWN_MIN_QTY = 1`
  - `MD_TIER_1 = 15` — markdown % for ≤30 days
  - `MD_TIER_2 = 30` — markdown % for ≤14 days
  - `MD_TIER_3 = 50` — markdown % for ≤7 days
  - `RTV_MIN_DAYS_REMAINING = 21`
  - `RTV_ESCALATION_THRESHOLD = 5000`
  - `IBP_DATA_FRESHNESS_HOURS = 24`
  - `HAZMAT_EXCLUDE = True`
  - `CURRENCY = "USD"`

## MCP Tool Integration (SAP API Layer)

> All SAP API calls MUST go through MCP tools. No direct HTTP clients permitted.

- [x] Verify `specification/batch-expiry-risk-agent/api-specs/` contains all 7 EDMX files:
  - `batch-master-record.edmx` (Batch Master Record — ORD ID: `sap.s4:apiResource:OP_API_BATCH_SRV_0001:v1`)
  - `shelf-life-data.edmx` (Shelf Life Data — ORD ID: `sap.s4:apiResource:CT_RIMS_SLVERSION_0001:v1`)
  - `warehouse-available-stock.edmx` (Warehouse Available Stock — ORD ID: `sap.s4:apiResource:WAREHOUSEAVAILABLESTOCK_0001:v1`)
  - `warehouse-storage-bin.edmx` (Warehouse Storage Bin — ORD ID: `sap.s4:apiResource:WAREHOUSESTORAGEBIN_0001:v1`)
  - `warehouse-order-task.edmx` (Warehouse Order and Task — ORD ID: `sap.s4:apiResource:WAREHOUSEORDER_0001:v1`)
  - `forecast-data-extraction.edmx` (IBP Forecast Data Extraction — no ORD ID; IBP OData service)
  - `returns-inspection.edmx` (Returns Inspection — ORD ID: `sap.s4:apiResource:CE_RETURNSINSPECTION_0001:v1`)
- [x] Invoke `mcp-translation-file` skill for each EDMX file. **If the skill is unavailable or `generate_mcp_translation` tool is not available, skip this item and the next two items and log** `[MCP-SKILL] mcp-translation-file unavailable — skipping MCP server asset generation`
- [ ] Invoke `setup-solution` skill to register MCP server assets for each generated translation.json (deferred — solution.yaml already registered; MCP server assets tracked via specification/mcps)
- [x] Wire MCP tool loading in `agent.py` using `get_mcp_tools()` from `mcp_tools.py` — NEVER direct HTTP. Load tools lazily (not in `__init__`).
- [x] Generate `mcp-mock.json` using `mcp-mock-config` skill after all MCP assets are created

## Core Business Logic

### Step 1 — Batch Scanner (`app/scanner.py`)

- [x] Implement `scan_at_risk_batches(plants: list[str], risk_horizon_days: int) -> list[BatchRecord]` that:
  - Calls MCP tool wrapping `batch-master-record.edmx` to fetch all batches with `ShelfLifeExpirationDate` within `risk_horizon_days`
  - Calls MCP tool wrapping `shelf-life-data.edmx` to retrieve SLED/BBD per batch
  - Calls MCP tool wrapping `warehouse-available-stock.edmx` to get quantity on hand per batch/plant/bin
  - Calls MCP tool wrapping `warehouse-order-task.edmx` to get open confirmed order quantities per batch (for order coverage deduction)
  - Excludes batches where residual qty after confirmed orders is below both `RESIDUAL_QTY_THRESHOLD_PCT × batch_qty` AND `RESIDUAL_QTY_ABSOLUTE`
  - Returns `BatchRecord` dataclass with: batch_number, material, description, plant, storage_location, bin, qty_on_hand, qty_on_open_orders, sled, days_to_expiry, unit_value, bin_velocity_class, temperature_zone, hazmat_flag, classification_attrs

### Step 2 — Risk Calculator (`app/risk_calculator.py`)

- [x] Implement `calculate_net_risk(batch: BatchRecord, ibp_demand: float, demand_horizon_days: int) -> RiskResult` that:
  - Computes `net_risk_qty = batch.qty_on_hand − batch.qty_on_open_orders`
  - Fetches IBP consensus demand forecast via MCP tool wrapping `forecast-data-extraction.edmx` for this SKU/location
  - Validates IBP data freshness — if data age > `IBP_DATA_FRESHNESS_HOURS`, sets `ibp_data_stale=True`
  - Computes `consumption_rate = ibp_demand / demand_horizon_days`
  - Computes `projected_consumption = consumption_rate × batch.days_to_expiry`
  - Computes `risk_qty = max(0, net_risk_qty − projected_consumption)`
  - Returns `RiskResult` with all computed fields plus `ibp_data_stale` flag

### Step 3 — Risk Scorer (`app/risk_scorer.py`)

- [x] Implement `score_batch(batch: BatchRecord, risk: RiskResult, total_sku_stock: float) -> int` that:
  - Normalises days-to-expiry component: `(RISK_HORIZON_DAYS − days_to_expiry) / RISK_HORIZON_DAYS × W_EXPIRY`
  - Normalises exposure component: `(risk_qty / total_sku_stock) × W_EXPOSURE` (cap at 100% of SKU stock)
  - Normalises financial exposure component: normalise unit_value × risk_qty against maximum in current run × W_VALUE
  - Normalises bin velocity component: C-bin=1.0, B-bin=0.5, A-bin=0.0 × W_BIN
  - Returns integer score 1–100; if `ibp_data_stale`, cap score normalisation on IBP-dependent components and lower to produce Low confidence rating

### Step 4 — Action Matcher (`app/action_matcher.py`)

- [x] Implement `match_actions(batch: BatchRecord, risk: RiskResult, score: int, bin_data: BinConfig, vendor_agreements: list) -> list[ActionRecommendation]` evaluating all 5 action types in priority order:
  - **Action 1 — Redistribution**: eligible if another bin at same plant has higher velocity AND material is FEFO-compatible AND remaining shelf life after transfer ≥ `MIN_SHELF_LIFE_POST_TRANSFER_DAYS`. Proposal: source_bin → target_bin, quantity. Uses MCP tool wrapping `warehouse-storage-bin.edmx` to find target bins.
  - **Action 2 — Channel reallocation**: eligible if IBP shows another channel/plant with higher near-term demand AND inter-plant transfer lead time < `days_to_expiry − TRANSFER_BUFFER_DAYS`. Proposal: source_plant → destination_plant, quantity, IBP demand reference.
  - **Action 3 — Markdown**: eligible if `MARKDOWN_ENABLED` AND `days_to_expiry ≤ MARKDOWN_TRIGGER_DAYS` AND `risk_qty > MARKDOWN_MIN_QTY`. Computes markdown tier: ≤7 days → `MD_TIER_3`%, ≤14 days → `MD_TIER_2`%, ≤30 days → `MD_TIER_1`%. Generates markdown event description draft (LLM call).
  - **Action 4 — Return to Vendor (RTV)**: eligible if active return agreement exists (via `returns-inspection.edmx` MCP tool) AND `days_to_expiry ≥ RTV_MIN_DAYS_REMAINING` AND qty ≥ vendor min. Generates structured RTV request draft (LLM call) with vendor, PO reference, quantity, reason code, proposed return date. If no agreement but `unit_value × risk_qty > RTV_ESCALATION_THRESHOLD`, flag for manual negotiation. If `HAZMAT_EXCLUDE = True` and batch is hazmat, skip RTV and redistribution — flag for dedicated hazmat handling.
  - **Action 5 — Quality hold / disposal**: last resort only. Generates QM notification draft with batch details and financial write-off estimate. Triggered if no other action is feasible OR if `days_to_expiry < RTV_MIN_DAYS_REMAINING` (override RTV).
- [x] Hard constraint enforcement (raise `ConstraintViolationError` with reason if violated):
  - NEVER recommend redistribution to temperature-incompatible bin
  - NEVER recommend RTV if `days_to_expiry < RTV_MIN_DAYS_REMAINING`
  - If `HAZMAT_EXCLUDE` and batch is hazmat: exclude from Actions 1, 2, 4; flag for hazmat handling only
  - NEVER include PII of warehouse staff in any generated artefact

### Step 5 — Report Generator (`app/report_generator.py`)

- [x] Implement `generate_report(run_id: str, plants: list, at_risk_batches: list[ScoredBatch], exceptions: list[str]) -> str` that:
  - Outputs structured markdown report with header (run timestamp, plants, scan horizon, total batches scanned, total at-risk, total financial exposure in CURRENCY)
  - Per-batch block (sorted by risk score descending, score ≥ `MIN_SCORE_THRESHOLD` only): batch #, material, description, plant/storage_location/bin, qty at risk, UoM, unit value, total exposure, SLED, days to expiry, risk score, recommended action(s) in priority order, confidence (High/Medium/Low based on IBP freshness and forecast confidence), draft artefact clearly marked as DRAFT REQUIRES HUMAN APPROVAL
  - Summary action table: columns Batch # | Material | Risk score | Days to expiry | Recommended action | Draft ready? | Assigned to (blank)
  - Exceptions section: batches skipped due to missing SLED, missing demand data, classification issues — flagged for master data correction
  - All monetary values in `CURRENCY`; all quantities in SAP base UoM
  - Suppresses batches below `MIN_SCORE_THRESHOLD`
- [x] If IBP data is stale: add prominent flag at top of report; all confidence ratings → Low

## Agent Orchestration (`app/agent.py`)

- [x] Implement `_run_agent(query: str, context_id: str) -> str` plain async helper containing all business logic (no `yield`):
  - Parse invocation parameters from `query` (plants filter, on-demand flag, override parameters)
  - Call scanner → risk calculator → scorer → action matcher → report generator in sequence
  - Handle data source fetch failures: if any required EWM source cannot be fetched, halt and return a clear failure report — do NOT produce partial output silently
  - Handle IBP staleness: downgrade all confidence to Low, add prominent flag; continue run (do not halt)
  - Log each milestone (M1–M5) using pattern `[MILESTONE_ID].[achieved|missed]: [description]` with run_id, plants, counts
- [x] Implement `stream()` that calls `_run_agent()` and yields its result — NO business logic inside `stream()`; NO `with tracer.start_as_current_span(...)` context manager inside `stream()`
- [x] System prompt for the agent LLM (in `@prompt_section`) must:
  - Instruct the agent that it is a recommendation engine only — it NEVER creates, posts, or confirms any SAP document
  - Instruct the agent to always set `top` to maximum 100 on any tool call that accepts a page-size parameter to prevent context overflow, and to inform the user when this limit is applied
  - Instruct the agent NOT to hallucinate batch data, stock quantities, or demand figures — it must only use data returned by MCP tools
  - Instruct the agent to always respect the hard constraint rules (temperature compatibility, RTV lead time, hazmat exclusion)

## Business Step Instrumentation

- [x] Add OpenTelemetry custom spans in `_run_agent()` for each milestone (use `@tracer.start_as_current_span` decorator form or `with tracer.start_as_current_span` context manager inside non-generator async methods — NEVER inside `stream()`)
- [x] M1 span: `batch-scan` — wraps scanner execution; logs `M1.achieved` on success, `M1.missed` on exception with reason
- [x] M2 span: `risk-qty-calculation` — wraps risk calculator loop; logs `M2.achieved` (includes ibp_data_age_hours), `M2.missed` on failure
- [x] M3 span: `risk-scoring` — wraps scorer loop; logs `M3.achieved` (includes suppressed count), `M3.missed` on failure
- [x] M4 span: `action-matching` — wraps action matcher loop; logs `M4.achieved` (includes drafts_generated, disposal_flagged), `M4.missed` on failure
- [x] M5 span: `report-delivery` — wraps report dispatch; logs `M5.achieved` (includes total_exposure_usd, at_risk_batches), `M5.missed` on failure
- [x] Verify `auto_instrument()` called at top of `main.py` before any AI framework imports

## Testing

- [x] `conftest.py` sets only `IBD_TESTING=true` — monkey-patches `mcp_tools.get_mcp_tools` to return mock tools built from `mcp-mock.json`
- [x] Unit tests in `assets/batch-expiry-risk-agent/tests/`:
  - `test_scanner.py` — mock MCP tools; verify at-risk batch identification, SLED horizon filtering, order coverage deduction, residual qty threshold exclusion
  - `test_risk_calculator.py` — mock IBP MCP tool; verify net risk qty calculation, IBP staleness detection, projected consumption formula
  - `test_risk_scorer.py` — verify weighted score formula (all 4 weights), MIN_SCORE_THRESHOLD suppression, score bounds 1–100
  - `test_action_matcher.py` — verify all 5 action type eligibility conditions, priority ordering, temperature zone constraint violation, RTV lead time guard, hazmat exclusion, HAZMAT_EXCLUDE toggle
  - `test_report_generator.py` — verify report structure (header, per-batch blocks, summary table, exceptions), IBP staleness flag, CURRENCY formatting, score-sorted output
  - `test_agent.py` — integration test: mock all MCP tools and LLM; run end-to-end `invoke` call; verify report produced with correct structure
- [x] Run each unit test file immediately after writing it — fix failures before writing the next
- [x] Run `pytest` from `assets/batch-expiry-risk-agent/` (no args) — coverage must be ≥ 70% (actual: 72%)
- [x] Verify `app/agent.py` has exactly 3 decorated functions: run `grep -c "^@agent_model\|^@agent_config\|^@prompt_section" assets/batch-expiry-risk-agent/app/agent.py` → must return 3
- [x] Run `pytest` again (no args) to generate final `test_report.json`
- [x] Verify `test_report.json` exists in `assets/batch-expiry-risk-agent/`

## Validation Checklist

- [x] `grep -r "M[0-9]\.achieved" assets/batch-expiry-risk-agent/app/` — must return results for all 5 milestones
- [x] `grep -r "sap_cloud_sdk.agent_decorators" assets/batch-expiry-risk-agent/app/` — must return results
- [x] `grep -c "^@agent_model\|^@agent_config\|^@prompt_section" assets/batch-expiry-risk-agent/app/agent.py` — must return 3
- [x] `ls assets/batch-expiry-risk-agent/test_report.json` — must exist
