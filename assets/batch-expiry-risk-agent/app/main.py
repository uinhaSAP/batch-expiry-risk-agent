# CRITICAL: Initialize telemetry BEFORE importing AI frameworks
from sap_cloud_sdk.aicore import set_aicore_config
from sap_cloud_sdk.core.telemetry import auto_instrument

set_aicore_config()
auto_instrument()

import logging
import os

import click
import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from agent_executor import AgentExecutor
from opentelemetry.instrumentation.starlette import StarletteInstrumentor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5000"))


@click.command()
@click.option("--host", default=HOST)
@click.option("--port", default=PORT)
def main(host: str, port: int):
    skill = AgentSkill(
        id="batch-expiry-risk-agent",
        name="batch-expiry-risk-agent",
        description="Proactive batch expiry risk management agent for SAP EWM + SAP IBP — identifies at-risk batches before SLED/BBD, scores financial exposure, and recommends prioritised actions (bin redistribution, channel reallocation, markdown, return-to-vendor, disposal) to prevent inventory write-offs. Read-only against all SAP systems — never creates, posts, or confirms any SAP document.",
        tags=["batch", "expiry", "risk", "ewm", "ibp", "inventory"],
        examples=["Run the daily batch expiry risk scan for plant 1000", "Which batches are expiring in the next 30 days with high financial exposure?", "Show me all batches eligible for return-to-vendor action"],
    )
    agent_card = AgentCard(
        name="batch-expiry-risk-agent",
        description="Proactive batch expiry risk management agent for SAP EWM + SAP IBP — identifies at-risk batches before SLED/BBD, scores financial exposure, and recommends prioritised actions (bin redistribution, channel reallocation, markdown, return-to-vendor, disposal) to prevent inventory write-offs. Read-only against all SAP systems — never creates, posts, or confirms any SAP document.",
        url=os.environ.get("AGENT_PUBLIC_URL", f"http://{host}:{port}/"),
        version="1.0.0",
        default_input_modes=["text", "text/plain"],
        default_output_modes=["text", "text/plain"],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        skills=[skill],
    )
    server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=DefaultRequestHandler(
            agent_executor=AgentExecutor(),
            task_store=InMemoryTaskStore(),
        ),
    )
    app = server.build()
    StarletteInstrumentor().instrument_app(app)

    logger.info(f"Starting A2A server at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
