# Product Requirements Document (PRD)

**Title:** Batch Expiry Risk Management Agent  
**Date:** 2026-06-03  
**Owner:** Supply Chain / Warehouse Operations  
**Solution Category:** AI Agent

---

## Product Purpose & Value Proposition

**Elevator Pitch:**  
Warehouse managers lose revenue to inventory write-offs because no standard SAP tool cross-references EWM batch expiry dates against IBP demand forecasts daily, scores financial risk, and surfaces prioritised actions before it is too late. This AI agent does exactly that — automatically, every morning.

**Business Need:**  
Batches in SAP EWM expire without being actioned because the data needed to identify, score, and act on the risk is spread across EWM (batch master, bins, open orders) and IBP (demand forecast). Manual monitoring is slow, inconsistent, and understates financial exposure. The agent closes this gap by running on a configurable schedule, computing net risk quantities, scoring each batch against a weighted financial risk formula, and recommending concrete actions — all without creating a single SAP document.

**Expected Value:**  
- Reduction in inventory write-offs from expiry events.
- Faster planner response: structured prioritised report replaces manual spreadsheet analysis.
- Consistent action coverage: all 5 action types (redistribution, markdown, RTV, quality hold, channel reallocation) evaluated for every at-risk batch every run.

**Product Objectives:**
1. Identify 100% of batches expiring within the configured risk horizon before expiry.
2. Surface only actionable recommendations (score ≥ MIN_SCORE_THRESHOLD) to keep the report noise-free.
3. Generate ready-to-approve draft artefacts (RTV request, markdown event, transfer proposal) that reduce planner effort to a review-and-approve action.

---

## Requirements

### Must-Have Requirements

**R01**: Scheduled and On-Demand Batch Scan
- **User Story**: As a warehouse manager, I need the agent to run automatically each morning (and on-demand) so that I have a fresh risk report before the warehouse shift starts.
- **Acceptance Criteria**: Given the configured cron schedule (default: daily 02:00 warehouse local time), when the trigger fires, then the agent fetches fresh EWM and IBP data and produces a complete report. On-demand invocation by a planner also produces a complete report.
- **Priority Rank**: 1

**R02**: At-Risk Batch Identification (SLED Scan)
- **User Story**: As a planner, I need all batches with SLED within the configurable risk horizon to be identified, with confirmed order coverage deducted, so I only see genuine risk.
- **Acceptance Criteria**: Given EWM batch records and open confirmed orders, when the scan runs, then batches with SLED ≤ RISK_HORIZON_DAYS are identified; batches with residual qty below both thresholds after confirmed orders are excluded.
- **Priority Rank**: 2

**R03**: Net Risk Quantity and Risk Score Calculation
- **User Story**: As a planner, I need each at-risk batch scored by financial exposure and expiry urgency so I can focus effort on the highest-risk batches first.
- **Acceptance Criteria**: Given batch on-hand qty, open order qty, IBP consensus demand, and MAP unit value, when scores are computed, then risk_qty and a 1–100 score (weighted: expiry 40%, exposure 30%, value 20%, bin velocity 10%) are produced; batches below MIN_SCORE_THRESHOLD are suppressed.
- **Priority Rank**: 3

**R04**: Prioritised Action Matching with Draft Artefacts
- **User Story**: As a planner, I need each at-risk batch matched to the best feasible action (redistribution, channel reallocation, markdown, RTV, disposal) with a draft artefact so I can approve and act without further research.
- **Acceptance Criteria**: Given a scored batch, when action matching runs, then actions are evaluated in priority order (1→5); the first eligible action type is recommended; a draft artefact (RTV request text, markdown event description, or transfer order proposal) is produced where applicable; all hard constraints (temperature zone compatibility, RTV lead time, hazmat exclusion) are enforced.
- **Priority Rank**: 4

**R05**: Structured Operational Report
- **User Story**: As a warehouse manager, I need a clean, structured daily report with per-batch blocks, a summary table, and a data quality exception list so I can review and assign actions without additional formatting work.
- **Acceptance Criteria**: Report contains: run timestamp, plant(s) covered, total batches scanned, total at-risk, total financial exposure; per-batch block (batch #, material, plant/bin, qty at risk, SLED, days to expiry, risk score, recommended action(s), confidence, draft artefact); summary action table; exceptions list. All monetary values in configured CURRENCY; all quantities in SAP base UoM.
- **Priority Rank**: 5

**R06**: Data Freshness and Failure Guardrails
- **User Story**: As a planner, I need the agent to halt and flag clearly if any required data source is unavailable or stale, rather than silently producing a partial report.
- **Acceptance Criteria**: If IBP data age exceeds IBP_DATA_FRESHNESS_HOURS, all confidence ratings are downgraded to Low and the report is prominently flagged. If any required EWM data source cannot be fetched, the run halts with a clear failure report. No partial report is produced silently.
- **Priority Rank**: 6

---

## Solution Architecture

**Architecture Overview:**  
Python AI agent deployed on SAP BTP (AI Core / agent runtime). The agent is triggered by cron schedule or on-demand invocation. It reads from SAP S/4HANA EWM via OData APIs and SAP IBP via the IBP OData/External Forecasting API. All SAP system interactions are read-only. Output is a structured report delivered via notification channel (email / Teams).

**Key Components:**
- **Agent runtime**: Python A2A agent on SAP BTP AI Core — hosts scanning logic, scoring engine, action matcher, report generator.
- **EWM connector**: OData calls to SAP S/4HANA for batch master, shelf life, available stock, bin config, warehouse orders/tasks.
- **IBP connector**: OData calls to SAP IBP for consensus demand forecast per SKU/location.
- **Config store**: Externalised parameter store (RISK_HORIZON_DAYS, weights, thresholds, action toggles, etc.).
- **Report dispatcher**: Sends structured report to configured notification channel.

**Integration Points:**
- SAP S/4HANA EWM — `API_BATCH_SRV` (batch master), `CT_RIMS_SLVERSION_0001` (shelf life), `WAREHOUSEAVAILABLESTOCK_0001` (stock), `WAREHOUSESTORAGEBIN_0001` (bin config), `WAREHOUSEORDER_0001` (open orders), `CE_RETURNSINSPECTION_0001` (returns inspection); read-only, per-run.
- SAP IBP — External Forecasting / Consensus Demand OData; read-only, per-run; freshness check enforced.
- Notification channel — email or Teams webhook; write; per-run.

### Agent Extensibility & Instrumentation

**Agent Extensibility:**
- All configurable parameters (scan horizons, score weights, thresholds, action toggles, markdown tiers) are externalised in a config store and can be adjusted without code changes.
- Action type handlers (R01–R05 action types) are modular — new action types can be added as independent handlers without touching core scoring logic.
- Notification channel is pluggable — additional output targets (SAC, Slack, ServiceNow) can be added via connector extension points.

**Business Step Instrumentation:**
- All 5 milestones (see Milestones section) must emit structured log statements using the pattern `[MILESTONE_ID].[achieved|missed]: [description]`.
- Logs must include run_id, plant(s), timestamp, and counts where applicable to enable monitoring of agent behaviour in production.

### Automation & Agent Behaviour

**Automation Level:** Autonomous agent (read + recommend); human-in-the-loop for all SAP document actions.

**Actions performed without human approval:**
- Fetch EWM batch, stock, bin, and order data.
- Fetch IBP demand forecast.
- Compute risk quantities and scores.
- Match actions and generate draft artefact text.
- Deliver report to notification channel.

**Actions that require human review or approval:**
- Any warehouse movement, transfer order, or delivery (never created by the agent).
- Any pricing / markdown event (draft only — pricing team must activate).
- Any RTV request submission to vendor (draft only — planner must approve and send).
- Any QM notification posting (draft only — quality team must post).

**Model or engine used:** Rule-based scoring engine with configurable weights; LLM (SAP Generative AI Hub) used only for draft artefact text generation (RTV request wording, markdown event description).

**Knowledge & data sources accessed:**
- SAP S/4HANA EWM: batch master, shelf life, available stock, bin configuration, warehouse orders (read-only).
- SAP IBP: consensus demand forecast, promotion flags (read-only).
- Vendor master / purchasing info records: RTV agreement lookup (read-only).

**Tools / connectors invoked:**
- `get_batch_master`: reads MCHA/MCHB-equivalent via `API_BATCH_SRV` — read-only.
- `get_shelf_life_data`: reads SLED/BBD via `CT_RIMS_SLVERSION_0001` — read-only.
- `get_warehouse_stock`: reads available stock per bin via `WAREHOUSEAVAILABLESTOCK_0001` — read-only.
- `get_bin_config`: reads bin velocity class and temperature zone via `WAREHOUSESTORAGEBIN_0001` — read-only.
- `get_open_orders`: reads confirmed warehouse orders/tasks via `WAREHOUSEORDER_0001` — read-only.
- `get_ibp_forecast`: reads consensus demand forecast via IBP External Forecasting OData — read-only.
- `get_vendor_return_agreements`: reads RTV eligibility via `CE_RETURNSINSPECTION_0001` / purchasing info records — read-only.
- `generate_draft_artefact`: LLM call to SAP Generative AI Hub to produce RTV request text, markdown event description, or transfer proposal — no SAP write side-effect.
- `dispatch_report`: sends structured report to notification channel — write (external only).

**Guardrails & fail-safes:**
- NEVER create, post, or confirm any SAP document.
- NEVER recommend bin redistribution to a temperature-incompatible zone.
- NEVER recommend RTV if days_to_expiry < RTV_MIN_DAYS_REMAINING — flag for disposal instead.
- NEVER surface batches below MIN_SCORE_THRESHOLD.
- NEVER use IBP data older than IBP_DATA_FRESHNESS_HOURS — halt and flag if stale.
- NEVER include PII of warehouse staff in any output.
- If HAZMAT_EXCLUDE = true, exclude hazmat batches from all standard recommendations — flag for dedicated hazmat handling.
- If any required data source cannot be fetched, halt and report failure clearly; do not produce a partial report silently.

### Configuration & Data

**Configuration Scope:**  
All parameters from the configurable parameters reference table (RISK_HORIZON_DAYS, DEMAND_HORIZON_DAYS, score weights, thresholds, action toggles, markdown tiers, RTV_MIN_DAYS_REMAINING, CURRENCY, PLANTS, etc.) are externalised and set per deployment without code changes.

**Organisational & Master Data:**
- Plant codes and storage types in scope must be configured at deployment.
- Batch classification attributes (temperature class, hazmat flag) must be maintained in SAP batch master.
- Vendor return agreements must be current in purchasing info records for RTV action to function.

---

## Milestones

### M1: Batch Scan Complete

- **Description**: All EWM batches with SLED within RISK_HORIZON_DAYS have been fetched and open confirmed order quantities deducted.
- **Achieved when**: Batch scan loop completes with at least one batch record processed; no data fetch errors.
- **Log on achievement**: `M1.achieved: batch_scan_complete | run_id={run_id} | plants={plants} | batches_fetched={count} | at_risk_candidates={count}`
- **Log on miss**: `M1.missed: batch_scan_failed | run_id={run_id} | reason={error_detail}`

### M2: Net Risk Quantities Calculated

- **Description**: IBP demand forecast applied per SKU/location; projected consumption before expiry computed; risk_qty derived for every at-risk candidate batch.
- **Achieved when**: risk_qty and projected_consumption_before_expiry computed for all candidate batches; IBP data freshness check passed.
- **Log on achievement**: `M2.achieved: risk_qty_calculated | run_id={run_id} | ibp_data_age_hours={age} | batches_with_risk_qty={count}`
- **Log on miss**: `M2.missed: risk_qty_calculation_failed | run_id={run_id} | reason={error_detail} | ibp_stale={true|false}`

### M3: Risk Scoring Complete

- **Description**: Each at-risk batch scored 1–100 using the weighted formula; batches below MIN_SCORE_THRESHOLD suppressed.
- **Achieved when**: All candidate batches have a risk score assigned; final at-risk list filtered to score ≥ MIN_SCORE_THRESHOLD.
- **Log on achievement**: `M3.achieved: scoring_complete | run_id={run_id} | scored={count} | suppressed_below_threshold={count} | final_at_risk={count}`
- **Log on miss**: `M3.missed: scoring_failed | run_id={run_id} | reason={error_detail}`

### M4: Action Matching Complete

- **Description**: Each scored batch evaluated against all enabled action types in priority order; draft artefacts generated where applicable.
- **Achieved when**: Every batch in the final at-risk list has at least one recommended action (or a disposal flag); all draft artefacts generated without error.
- **Log on achievement**: `M4.achieved: action_matching_complete | run_id={run_id} | batches_matched={count} | drafts_generated={count} | disposal_flagged={count}`
- **Log on miss**: `M4.missed: action_matching_failed | run_id={run_id} | reason={error_detail}`

### M5: Report Delivered

- **Description**: Structured operational report (per-batch blocks + summary table + exceptions) dispatched to configured notification channel.
- **Achieved when**: Report successfully delivered to all configured recipients; delivery confirmation received.
- **Log on achievement**: `M5.achieved: report_delivered | run_id={run_id} | channel={channel} | total_exposure_usd={value} | at_risk_batches={count}`
- **Log on miss**: `M5.missed: report_delivery_failed | run_id={run_id} | channel={channel} | reason={error_detail}`
