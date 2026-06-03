# Agent Guidelines

Technical constraints and patterns for building Pro-Code AI Agents. Follow these throughout specification execution.

## Tech Stack

- Python 3.13
- Agent framework defined in the `sap-agent-bootstrap` skill
- Agent2Agent (A2A) protocol
- Local execution only (in-memory storage, no deployment)

## Project Structure

- Asset root: `assets/<asset-name>/`
- Required structure: `asset.yaml`, `app/`
- Full layout from project root: `solution.yaml`, `assets/<asset-name>/asset.yaml`, `assets/<asset-name>/app/`
- `asset.yaml` must use `buildPath: .` and `/.well-known/agent.json` for all health probes
- Follow the `sap-agent-bootstrap` skill for project scaffolding — invoke directly from `assets/<asset-name>/`, use copy commands

## Key Constraints

- When working with LangChain or LangGraph, you MUST NEVER use the `create_react_agent` function (`from langgraph.prebuilt import create_react_agent`) as it has been deprecated in LangChain 1.0. Instead, you should use the `from langchain.agents import create_agent` function.
- **NEVER call SAP APIs directly** (no `requests`, `httpx`, or hand-rolled OData clients). All SAP API consumption MUST go through MCP servers. The agent consumes them as tools, never as raw HTTP calls (regardless of whether it's an existing MCP Server or a new MCP Server created by the `mcp-translation-file` skill).
- Only use public APIs; mock any private systems (like S/4HANA) with minimal mock data
- AI Core is available at **runtime** via LiteLLM (environment variables provided at deployment) but is **NOT available during tests** — all LLM calls must be mocked
- No Git operations, no authentication, no documentation/READMEs
- Update `requirements.txt` for any new dependencies
- Never modify `sys.path`
- Map SAP Joule Studio/Skills concepts to standard agent tools
- No `.env` files (environment variables supplied at runtime)

## Code Quality

- All Python code must compile with valid imports
- No `src.` import patterns
- All function parameters must be used in function body

## Agent Decorators

- The bootstrap template already includes decorator scaffolding — no separate skill invocation needed
- **NEVER add new decorated functions to `app/agent.py`** — the three from the bootstrap template (`@agent_model`, `@agent_config` for temperature, `@prompt_section`) are the complete and final set. `@agent_config` is not a general-purpose decorator; it exposes parameters to the SAP platform UI and is intentionally limited to temperature. All other values (thresholds, limits, counts, etc.) must be plain Python constants.
- Never mark decorator tasks complete until `sap_cloud_sdk.agent_decorators` imports exist in `app/agent.py`

## Agent Instrumentation

- ALL business logic steps MUST be instrumented with proper logging and OpenTelemetry spans
- Use milestones from the PRD's "Milestones" section (if available) or derive from the project input for business step instrumentation
- Each milestone must emit structured log statements on achievement and miss
- Log pattern: `[MILESTONE_ID].[achieved|missed]: [description]`
- Add OpenTelemetry custom spans for each business step using `tracer.start_as_current_span` — use the **decorator form** (`@tracer.start_as_current_span("name")`) on regular async methods, or the **context manager form** (`with tracer.start_as_current_span("name"):`) inside non-generator async functions
- **NEVER use `with tracer.start_as_current_span(...)` as a context manager inside an async generator** (i.e. any method containing `yield`). Doing so causes `ValueError: Token was created in a different Context` when the generator is closed via `GeneratorExit`. For `stream()`, extract all business logic into a plain async helper method (e.g. `_run_agent()`) and instrument that method instead, then call it from `stream()` and yield the result outside any span context.
- Ensure `auto_instrument()` is called at top of `main.py` before any AI framework imports

## MCP Tool Integration

All SAP API integrations MUST use this pattern. If the PRD or specification references any SAP API (OData, REST, events), MCP wiring is mandatory, not optional.

MCP tool names are prefixed with an MCP server identifier at runtime (e.g. `mcp_myserver__get_items`). **Never hard-code tool names in code.** Retrieve tools dynamically via `get_mcp_tools()` and let the agent resolve them by capability, not by name.

When writing system instructions for the agent, explicitly instruct the agent not to hallucinate data. The system prompt MUST always instruct the agent to set `top` (or equivalent page-size parameter) to a maximum of 100 on every tool call that accepts it — regardless of whether the user requested a limit — to prevent context overflow. The agent must inform the user when this limit is applied.

### Canonical Pattern

```python
from mcp_tools import get_mcp_tools

async def _load_tools() -> list:
    return await get_mcp_tools()
```

`mcp_tools.py` is the owned indirection layer produced by the bootstrap — import from there, never directly from `sap_cloud_sdk.agentgateway`. This is the target the test fixture patches.

Call `_load_tools()` lazily (not in `__init__`) — it makes network calls. Wire the result into the agent graph:

```python
class MyAgent:
    def __init__(self):
        self._tools = None

    async def _get_tools(self) -> list:
        if self._tools is None:
            self._tools = await _load_tools()
        return self._tools

    async def stream(self, query, context_id, ext_impl=None):
        tools = await self._get_tools()
        graph = self._build_graph(tools, system_prompt=get_system_prompt())
        ...
```

### Local Testing (IBD_TESTING)

**Do NOT branch on `IBD_TESTING` in application code.** The `conftest.py` monkey-patches `mcp_tools.get_mcp_tools` before any agent code runs. Agent code stays identical in production and tests.

The patch returns `StructuredTool` instances built from `mcp-mock.json`. Generate `mcp-mock.json` with the `mcp-mock-config` skill before running tests.

## Testing

Working directory for all test operations: `assets/<asset-name>/` (asset root).

### Setup

1. Install test dependencies: `pip install -r requirements-test.txt`

### Boilerplate Files

- `conftest.py` — shared fixtures, custom markers, writes `test_report.json` on full runs
- `pytest.ini` — configures test discovery (`prebuilt_tests/`, `tests/`), default flags, markers
- `requirements-test.txt` — test dependencies
- `prebuilt_tests/` — pre-built structure and server tests; NEVER modify these

### Writing Tests

- All generated tests go in `assets/<asset-name>/tests/` (NOT inside `app/`)
- Unit tests: exactly one per tool; run each immediately after writing
- Integration test: one end-to-end test exercising the full agent graph
- **AI Core / LLM calls MUST be mocked in all tests.** AI Core credentials are NOT available in the test environment. Patch the LLM (e.g. `ChatLiteLLM`) to return canned responses. Never make real network calls to AI Core during tests.
- Mock all external systems (S/4HANA, MCP servers, AI Core) — tests must run offline

### Running Tests

- ALWAYS invoke as just `pytest` from asset root — no paths, no `--cov`, no `--json-report`, no extra flags
- `pytest.ini` configures everything; extra CLI flags conflict with ini settings
- Only exception: targeting a single test: `pytest path/to/test_file.py::test_name`
- Coverage must be ≥ 70%; if below, add targeted tests until threshold met
- Final `pytest` run (no args) MUST produce `test_report.json` — this only happens on full runs without arguments

## Validation Checklist

Run these verifications before marking implementation complete:

```bash
# Instrumentation
grep -r "M[0-9]\.achieved" assets/<asset-name>/app/     # must return results

# Decorators
grep -r "sap_cloud_sdk.agent_decorators" assets/<asset-name>/app/  # must return results
grep -c "^@agent_model\|^@agent_config\|^@prompt_section" assets/<asset-name>/app/agent.py  # must return 3 (one @agent_model, one @agent_config for temperature, one @prompt_section)

# Test report
ls assets/<asset-name>/test_report.json                  # must exist
```

## MCP Translation Files, MCP Servers & Mock Tools (Post-Implementation)

After all asset spec TODO items are complete, run the applicable path(s):

### Skill Availability — Graceful Degradation

The `mcp-translation-file` skill depends on the `generate_mcp_translation` tool which is only available in the Joule studio runtime. **Before invoking Path A**, check whether the skill is available:

- If `mcp-translation-file` **is available** (i.e. the skill exists) → proceed with Path A normally.
- If `mcp-translation-file` **is available but doesn't pass the Gate 0** (i.e. the `generate_mcp_translation` tool is not available) **OR** if `mcp-translation-file` **is NOT available** (i.e. the skill does not exist) → skip Path A entirely (no translation files, no MCP server assets). Log: `[MCP-SKILL] mcp-translation-file unavailable — skipping MCP server asset generation. Agent will use existing MCP servers only.` Continue the process normally with Path B (if applicable) or proceed directly to testing. The solution will not include MCP server assets for APIs that lack pre-existing MCP servers.

This does NOT affect Path B (existing MCP servers with known ORD IDs) — those always work regardless of skill availability.

### Path A — API spec files (OData/REST, no existing MCP server)

Run when `specification/<asset-name>/api-specs/` contains API spec files (e.g. `supplier-invoices.json`).

1. **MCP Translation Files:** Invoke the `mcp-translation-file` skill. Do NOT manually create translation files.
2. **MCP Server Assets:** Invoke the `setup-solution` skill to create MCP server assets for any translation files generated in step 1. Do NOT manually create any MCP server assets.

Remember the names and ORD IDs of the generated MCP servers for later reference in the agent's `asset.yaml` required dependencies.

### Path B — MCP spec files (existing MCP server with known ORD ID)

Run when `specification/<asset-name>/mcp-specs/` contains `mcp-spec-*.json` files captured during API discovery (step 2a).

No translation or MCP server asset creation needed — the MCP server already exists externally.

Remember the names and ORD IDs of the existing MCP servers for later reference in the agent's `asset.yaml` required dependencies.

### MCP Server Dependencies in asset.yaml

For **every** MCP server the agent uses, add a corresponding entry to the agent's `asset.yaml` under `requires`:

```yaml
requires:
  - name: <mcp-server-name>
    kind: mcp-server
    ordId: <ord-id>
```

This applies to both internally created MCP server assets (Path A) and externally existing MCP servers (Path B).

### Mock MCP Configuration

After **both** `mcp-translation-file` and `setup-solution` have completed (Path A), or after confirming existing MCP specs (Path B), invoke the `mcp-mock-config` skill to generate `mcp-mock.json`. This must be the **last** MCP-related skill invoked — never before `mcp-translation-file` or `setup-solution`.

The required chain is: `mcp-translation-file` → `setup-solution` → `mcp-mock-config`.

## Agent Evaluation (Post-Implementation)

Run after all tests pass and the agent is working locally.

### Step 1: Generate tool schema

Invoke the `sap-aeval-generate-tool-schema` skill from the asset root (`assets/<asset-name>/`). It scans the `app/` directory, extracts all tool function signatures and docstrings, and writes `tools.json` to the current working directory.

### Step 2: Generate eval test cases

Invoke the `sap-aeval-generate-testcase` skill from the same asset root, passing:

- The PRD file path (e.g. `specification/<asset-name>/specification.md`)
- `tools.json` generated in Step 1

The skill writes eval criteria to `aeval/eval.yaml` and YAML test cases to `aeval/testcases/`. Review the generated test cases and replace any placeholder values (e.g. `<param_name:example>`) with realistic data before running evaluations.
