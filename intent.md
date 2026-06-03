# Batch Expiry Risk Management Agent

Proactive AI agent for SAP EWM + SAP IBP that prevents inventory write-offs by identifying at-risk batches early and recommending prioritised actions before expiry occurs.

## Business challenge

Warehouse managers and supply chain planners face inventory write-offs caused by batches reaching their shelf life expiry date (SLED/BBD) before they can be consumed or actioned. SAP EWM holds the batch and bin data; SAP IBP holds the demand forecast — but no standard tool automatically cross-references them daily, scores financial risk, and surfaces prioritised recommendations (redistribution, channel reallocation, markdown, return-to-vendor, disposal) before expiry occurs. The agent must act as a recommendation engine only — a human must approve and trigger any SAP document.

## Key Milestones

1. **Batch scan complete** — all EWM batches with SLED within RISK_HORIZON_DAYS fetched; open confirmed orders deducted from on-hand quantities.
2. **Net risk quantities calculated** — IBP consensus demand forecast applied per SKU/location; projected consumption before expiry computed; risk_qty derived for each batch.
3. **Risk scoring complete** — each at-risk batch scored 1–100 using weighted formula (expiry urgency, exposure %, financial value, bin velocity); batches below MIN_SCORE_THRESHOLD suppressed.
4. **Action matching complete** — each scored batch evaluated against all enabled action types (redistribution, channel reallocation, markdown, RTV, disposal) in priority order; draft artefacts generated where applicable.
5. **Report delivered** — structured operational report (per-batch blocks + summary table + exceptions) dispatched to planner/warehouse manager; data quality flags raised for master data gaps.

## Business Architecture (RBA)

### End-to-End Process

Plan to Fulfill (generic)

### Process Hierarchy

```
Plan to Fulfill (generic)
└── Plan to Optimize Fulfillment (generic)
    └── Plan demand (generic) [BPS-338]
        └── Develop baseline demand forecast
└── Manage Fulfillment (generic)
    └── Manage supply chain data and operations (generic) [BPS-342]
        └── Manage inventory and warehouse operations
└── Deliver Product to Fulfill (generic)
    └── Manage warehouse and inventory (generic) [BPS-348]
        └── Perform quality inspection
└── Make to Inspect (generic)
    └── Operate manufacturing (generic) [BPS-347]
        └── Perform quality inspection
```

### Summary

Batch expiry risk management sits at the intersection of demand planning (IBP forecast vs. SLED risk quantity) and warehouse/inventory operations (EWM bin management, FEFO stock movements), with quality hold/disposal as the last-resort path — all within the Plan to Fulfill E2E process.

## Fit Gap Analysis

| Requirement (business) | Standard asset(s) found | API ORD ID | MCP Server ORD ID | Gap? | Notes / assumptions |
|---|---|---|---|---|---|
| Read batch master records with SLED/BBD, qty, plant, storage location | SAP S/4HANA Cloud — Batch Number Management (SC5116 / SC3336) | `sap.s4:apiResource:API_BATCH_SRV:v1` | — | No | OData API available; no MCP server found — direct API integration required |
| Read shelf life / BBD data per batch | SAP S/4HANA — Shelf Life Data | `sap.s4:apiResource:CT_RIMS_SLVERSION_0001:v1` | — | No | Dedicated Shelf Life Data OData API available |
| Read EWM warehouse available stock per bin | SAP S/4HANA — Internal/Outbound Warehouse Management (SC5130 / SC841) | `sap.s4:apiResource:WAREHOUSEAVAILABLESTOCK_0001:v1` | — | No | Warehouse Available Stock Read API available |
| Read EWM storage bin configuration (velocity class, temp zone) | SAP S/4HANA — Internal Warehouse Management | `sap.s4:apiResource:WAREHOUSESTORAGEBIN_0001:v1` | — | No | Warehouse Storage Bin Read API available |
| Read open confirmed warehouse orders/tasks (coverage check) | SAP S/4HANA — Outbound Warehouse Management | `sap.s4:apiResource:WAREHOUSEORDER_0001:v1` | — | No | Warehouse Order and Task API available |
| Read IBP consensus demand forecast per SKU/location | SAP IBP — Consensus Demand Management (SC2988), Demand Forecasting (SC2989) | — (IBP OData / External Forecasting) | — | Maybe | IBP External Forecasting EDMX available; no ORD ID; integration via IBP OData or BTP event mesh required |
| Check vendor return agreements (RTV eligibility) per material/vendor | SAP S/4HANA — Returns Inspection | `sap.s4:apiResource:CE_RETURNSINSPECTION_0001:v1` | — | Maybe | Returns Inspection API available; RTV agreement lookup (LFA1/purchasing info records) may need supplemental MM/purchasing API |
| Score batches by financial risk and prioritise (multi-factor weighted formula) | None — no standard product performs this cross-source scoring | — | — | **Yes** | Custom AI agent logic required; no SAP standard capability covers expiry risk scoring across EWM + IBP |
| Match batches to action types and generate draft artefacts (RTV text, markdown event, transfer proposal) | None — no standard product generates these recommendations | — | — | **Yes** | Core AI agent capability; recommendation engine with configurable parameters is fully custom |
| Draft QM notification with write-off estimate for disposal | SAP S/4HANA — Quality Inspection, Non-Conformance Management | `sap.s4:apiResource:API_INSPECTIONLOT_SRV:v1` | — | No | Read-only context; agent drafts QM notification text — human must post |
| Deliver structured operational report to planners | SAP Analytics Cloud (optional) | — | — | Maybe | Report output can be email/Teams notification or SAC embedded; custom formatting required |

### Key findings

- SAP S/4HANA (Cloud Public/Private) covers all EWM read operations via OData APIs — batch master, shelf life, available stock, bin config, warehouse orders — but **no MCP servers** are available; all integrations require direct OData API calls.
- SAP IBP covers consensus demand management and forecasting natively (SC2988/SC2989); integration requires IBP OData or the External Forecasting API; ORD ID not yet registered.
- The **core intelligence gap** — multi-factor expiry risk scoring, action matching, and draft artefact generation — has no standard SAP coverage and must be built as a custom AI agent.
- All 5 action types (redistribution, channel reallocation, markdown, RTV, disposal) require agent-side logic; SAP APIs provide the read context only.
- The agent is explicitly read-only at the SAP layer; no write/post operations are in scope.

## Recommendations

### Batch Expiry Risk Management AI Agent

#### Executive Summary

Python AI agent on BTP that reads EWM + IBP data, scores expiry risk, and delivers prioritised action recommendations to planners daily.

#### Recommended Solution

Build a Python-based AI agent (A2A protocol) deployed on SAP BTP that:
1. Runs on a configurable cron schedule (default: daily 02:00 warehouse local time) or on-demand invocation.
2. Reads batch master, shelf life, available stock, bin configuration, and open warehouse orders from SAP S/4HANA EWM via OData APIs.
3. Reads IBP consensus demand forecast via the IBP OData / External Forecasting API.
4. Executes the 3-step scanning logic (identify → calculate net risk qty → score and prioritise) with all configurable parameters externalised.
5. Matches each at-risk batch to enabled action types in priority order and generates draft artefacts (RTV request text, markdown event description, transfer order proposal).
6. Outputs a structured operational report (per-batch blocks + summary action table + exceptions/data quality flags) delivered via email/notification.
7. Enforces all hard constraints: no SAP document creation, temperature-zone compatibility checks, RTV lead time guards, hazmat exclusions, IBP data freshness checks.

SAP products in scope: SAP S/4HANA (EWM), SAP IBP, SAP BTP (AI Core / agent runtime).

#### Recommended solution category

AI Agent

#### Intent fit
95%
