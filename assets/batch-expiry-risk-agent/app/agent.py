import logging
import time
import uuid
from dataclasses import dataclass
from typing import AsyncGenerator, Literal, Sequence

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langchain_litellm import ChatLiteLLM
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry import trace
from sap_cloud_sdk.agent_decorators import agent_config, agent_model, prompt_section

from mcp_tools import get_mcp_tools
from models import FullBatchReport, ScoredBatch
from scanner import scan_at_risk_batches
from risk_calculator import calculate_net_risk
from risk_scorer import score_all_batches
from action_matcher import match_actions
from report_generator import generate_report
from config import (
    RISK_HORIZON_DAYS,
    DEMAND_HORIZON_DAYS,
    MIN_RISK_QTY,
    MIN_SCORE_THRESHOLD,
    IBP_DATA_FRESHNESS_HOURS,
    CURRENCY,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

THREAD_TTL_SECONDS = 3600


@agent_model(
    key="config.model",
    label="LLM Model",
    description="The language model powering this agent",
)
def get_model_name() -> str:
    return "sap/anthropic--claude-4.5-sonnet"


@agent_config(
    key="config.temperature",
    label="LLM Temperature",
    description="Controls randomness of responses (0.0 = deterministic, 1.0 = creative)",
)
def get_temperature() -> float:
    return 0.0


@prompt_section(
    key="prompts.system",
    label="System Prompt",
    description="The full system prompt defining the agent's role and behavior",
    validation={"format": "markdown", "max_length": 5000},
)
def get_system_prompt() -> str:
    return """You are a proactive batch expiry risk management agent operating within SAP EWM and SAP IBP. Your sole purpose is to prevent inventory write-offs by identifying at-risk batches early and recommending concrete, prioritised actions before expiry occurs. You are a recommendation engine, NOT an execution engine — you surface risk and propose actions; a human must approve and trigger any warehouse movements, vendor communications, or markdown events. NEVER create, post, or confirm any SAP document. NEVER recommend redistribution to a temperature-incompatible bin. NEVER recommend RTV if days_to_expiry is below the minimum days remaining threshold. NEVER include personally identifiable information of warehouse staff in any output. Always set top to a maximum of 100 on any tool call that accepts a page-size parameter to prevent context overflow, and inform the user when this limit is applied. Do not hallucinate batch data, stock quantities, or demand figures — use only data returned by MCP tools."""


@dataclass
class AgentResponse:
    status: Literal["input_required", "completed", "error"]
    message: str


def _parse_plants(query: str) -> list[str] | None:
    """Extract plant filter from query string. Returns None for all plants."""
    import re
    match = re.search(r"plant[s]?\s*[=:]\s*([A-Z0-9,\s]+)", query, re.IGNORECASE)
    if match:
        plants = [p.strip() for p in match.group(1).split(",") if p.strip()]
        return plants if plants else None
    return None


async def _run_agent(
    query: str,
    tools: list,
    risk_horizon_days: int = RISK_HORIZON_DAYS,
    demand_horizon_days: int = DEMAND_HORIZON_DAYS,
) -> str:
    """Core business logic for the batch expiry risk scan.

    Extracted from stream() into a plain async helper to allow safe
    OpenTelemetry instrumentation without wrapping yield statements.
    All milestone logging uses pattern [MX.achieved|missed]: description.
    """
    run_id = str(uuid.uuid4())[:8]
    plants = _parse_plants(query)
    ibp_data_stale_global = False

    # ── M1: Batch scan ───────────────────────────────────────────────────────
    with tracer.start_as_current_span("batch-scan") as span:
        try:
            batches, exceptions = await scan_at_risk_batches(
                tools=tools,
                plants=plants,
                risk_horizon_days=risk_horizon_days,
            )
            span.set_attribute("batches_fetched", len(batches))
            span.set_attribute("at_risk_candidates", len(batches))
            logger.info(
                "M1.achieved: batch_scan_complete | run_id=%s | plants=%s | "
                "batches_fetched=%d | at_risk_candidates=%d",
                run_id, plants or "ALL", len(batches), len(batches),
            )
        except Exception as exc:
            logger.error("M1.missed: batch_scan_failed | run_id=%s | reason=%s", run_id, str(exc))
            return (
                f"# Batch Expiry Risk Scan — FAILED\n\n"
                f"**Run ID:** {run_id}\n\n"
                f"**Error:** Could not fetch batch data from SAP EWM: {exc}\n\n"
                f"The scan has been halted. No partial report produced. "
                f"Check SAP EWM connectivity and MCP tool availability."
            )

    total_batches_scanned = len(batches)

    # ── M2: Net risk quantities ───────────────────────────────────────────────
    batch_risks = []
    with tracer.start_as_current_span("risk-qty-calculation") as span:
        try:
            for batch in batches:
                risk = await calculate_net_risk(
                    batch=batch,
                    tools=tools,
                    demand_horizon_days=demand_horizon_days,
                    ibp_freshness_hours=IBP_DATA_FRESHNESS_HOURS,
                )
                if risk.ibp_data_stale:
                    ibp_data_stale_global = True
                if risk.risk_qty > MIN_RISK_QTY:
                    batch_risks.append((batch, risk))

            max_age = max((r.ibp_data_age_hours for _, r in batch_risks), default=0.0)
            span.set_attribute("ibp_data_age_hours", max_age)
            span.set_attribute("batches_with_risk_qty", len(batch_risks))
            logger.info(
                "M2.achieved: risk_qty_calculated | run_id=%s | ibp_data_age_hours=%.1f | "
                "batches_with_risk_qty=%d | ibp_stale=%s",
                run_id, max_age, len(batch_risks), ibp_data_stale_global,
            )
        except Exception as exc:
            logger.error(
                "M2.missed: risk_qty_calculation_failed | run_id=%s | reason=%s | ibp_stale=%s",
                run_id, str(exc), ibp_data_stale_global,
            )
            return (
                f"# Batch Expiry Risk Scan — FAILED\n\n"
                f"**Run ID:** {run_id}\n\n"
                f"**Error:** Risk quantity calculation failed: {exc}\n\n"
                f"Scan halted at Step 2."
            )

    # Build SKU stock map for scorer
    sku_stock_map: dict[tuple[str, str], float] = {}
    for batch, _ in batch_risks:
        key = (batch.material, batch.plant)
        sku_stock_map[key] = sku_stock_map.get(key, 0.0) + batch.qty_on_hand

    # ── M3: Risk scoring ──────────────────────────────────────────────────────
    scored = []
    suppressed = 0
    with tracer.start_as_current_span("risk-scoring") as span:
        try:
            scored_tuples = score_all_batches(
                batches_with_risks=batch_risks,
                sku_stock_map=sku_stock_map,
                risk_horizon_days=risk_horizon_days,
                min_score_threshold=MIN_SCORE_THRESHOLD,
            )
            suppressed = len(batch_risks) - len(scored_tuples)
            for batch, risk, score, confidence in scored_tuples:
                if ibp_data_stale_global:
                    confidence = "Low"
                total_exposure = batch.unit_value * risk.risk_qty
                scored.append(ScoredBatch(
                    batch=batch,
                    risk=risk,
                    score=score,
                    confidence=confidence,
                    total_exposure=total_exposure,
                    total_sku_stock=sku_stock_map.get((batch.material, batch.plant), batch.qty_on_hand),
                ))
            span.set_attribute("scored", len(scored))
            span.set_attribute("suppressed_below_threshold", suppressed)
            logger.info(
                "M3.achieved: scoring_complete | run_id=%s | scored=%d | "
                "suppressed_below_threshold=%d | final_at_risk=%d",
                run_id, len(batch_risks), suppressed, len(scored),
            )
        except Exception as exc:
            logger.error("M3.missed: scoring_failed | run_id=%s | reason=%s", run_id, str(exc))
            return (
                f"# Batch Expiry Risk Scan — FAILED\n\n"
                f"**Run ID:** {run_id}\n\n"
                f"**Error:** Risk scoring failed: {exc}"
            )

    # ── M4: Action matching ───────────────────────────────────────────────────
    batch_reports: list[FullBatchReport] = []
    drafts_generated = 0
    disposal_flagged = 0
    with tracer.start_as_current_span("action-matching") as span:
        try:
            for sb in scored:
                actions = await match_actions(
                    batch=sb.batch,
                    risk=sb.risk,
                    tools=tools,
                )
                for a in actions:
                    if a.draft_artefact:
                        drafts_generated += 1
                    if a.action_type == 5:
                        disposal_flagged += 1
                batch_reports.append(FullBatchReport(scored_batch=sb, actions=actions))

            span.set_attribute("batches_matched", len(batch_reports))
            span.set_attribute("drafts_generated", drafts_generated)
            span.set_attribute("disposal_flagged", disposal_flagged)
            logger.info(
                "M4.achieved: action_matching_complete | run_id=%s | batches_matched=%d | "
                "drafts_generated=%d | disposal_flagged=%d",
                run_id, len(batch_reports), drafts_generated, disposal_flagged,
            )
        except Exception as exc:
            logger.error("M4.missed: action_matching_failed | run_id=%s | reason=%s", run_id, str(exc))
            return (
                f"# Batch Expiry Risk Scan — FAILED\n\n"
                f"**Run ID:** {run_id}\n\n"
                f"**Error:** Action matching failed: {exc}"
            )

    # ── M5: Report ────────────────────────────────────────────────────────────
    with tracer.start_as_current_span("report-delivery") as span:
        try:
            report = generate_report(
                run_id=run_id,
                plants=plants or [],
                batch_reports=batch_reports,
                exceptions=exceptions,
                total_batches_scanned=total_batches_scanned,
                ibp_data_stale=ibp_data_stale_global,
                risk_horizon_days=risk_horizon_days,
            )
            total_exposure = sum(
                br.scored_batch.batch.unit_value * br.scored_batch.risk.risk_qty
                for br in batch_reports
            )
            span.set_attribute("at_risk_batches", len(batch_reports))
            span.set_attribute("total_exposure", total_exposure)
            logger.info(
                "M5.achieved: report_delivered | run_id=%s | channel=agent_response | "
                "total_exposure_%s=%.2f | at_risk_batches=%d",
                run_id, CURRENCY, total_exposure, len(batch_reports),
            )
            return report
        except Exception as exc:
            logger.error(
                "M5.missed: report_delivery_failed | run_id=%s | channel=agent_response | reason=%s",
                run_id, str(exc),
            )
            return (
                f"# Batch Expiry Risk Scan — FAILED\n\n"
                f"**Run ID:** {run_id}\n\n"
                f"**Error:** Report generation failed: {exc}"
            )


class SampleAgent:
    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(self):
        self.llm = ChatLiteLLM(model=get_model_name(), temperature=get_temperature())
        self._checkpointer = InMemorySaver()
        self._last_active: dict[str, float] = {}
        self._tools = None
        self._summarization_middleware = SummarizationMiddleware(
            model=self.llm,
            trigger=("tokens", 100_000),
        )

    def _touch(self, thread_id: str) -> None:
        now = time.monotonic()
        expired = [tid for tid, ts in list(self._last_active.items()) if now - ts > THREAD_TTL_SECONDS]
        for tid in expired:
            self._checkpointer.delete_thread(tid)
            del self._last_active[tid]
            logger.info("Evicted inactive thread: %s", tid)
        self._last_active[thread_id] = now

    async def _get_tools(self) -> list:
        """Lazy MCP tool loading — network calls, must not be called in __init__."""
        if self._tools is None:
            self._tools = await get_mcp_tools()
        return self._tools

    async def stream(
        self,
        query: str,
        context_id: str,
        tools: Sequence[BaseTool] | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Stream agent responses. Business logic delegated to _run_agent()."""
        self._touch(context_id)
        yield {
            "is_task_complete": False,
            "require_user_input": False,
            "content": "Running batch expiry risk scan...",
        }

        try:
            mcp_tools = await self._get_tools()
            active_tools = list(tools) + mcp_tools if tools else mcp_tools
            response = await _run_agent(query=query, tools=active_tools)
            yield {
                "is_task_complete": True,
                "require_user_input": False,
                "content": response,
            }
        except Exception as e:
            logger.exception("Agent stream() failed")
            yield {
                "is_task_complete": True,
                "require_user_input": False,
                "content": f"I encountered an error while processing your request: {str(e)}. Please try again.",
            }

    async def invoke(
        self,
        query: str,
        context_id: str,
        tools: Sequence[BaseTool] | None = None,
    ) -> AgentResponse:
        last: dict = {}
        async for chunk in self.stream(query, context_id, tools=tools):
            last = chunk
        if last.get("is_task_complete"):
            return AgentResponse(status="completed", message=last["content"])
        if last.get("require_user_input"):
            return AgentResponse(status="input_required", message=last["content"])
        return AgentResponse(status="error", message=last.get("content", "Unknown error"))
