# Batch Expiry Risk Agent

Proactive batch expiry risk management agent for SAP EWM + SAP IBP — identifies at-risk batches before SLED/BBD, scores financial exposure, and recommends prioritised actions (bin redistribution, channel reallocation, markdown, return-to-vendor, disposal) to prevent inventory write-offs. Read-only against all SAP systems — never creates, posts, or confirms any SAP document.

## Overview

Uses A2A Protocol, LangGraph, LiteLLM, and SAP Cloud SDK.

## Structure

- `app/main.py` - A2A server entry
- `app/agent_executor.py` - Request handling
- `app/agent.py` - Agent logic
