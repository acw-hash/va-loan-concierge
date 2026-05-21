"""
Rate Intelligence Agent — orchestrator-side client for the Foundry hosted agent.

This module is the ORCHESTRATOR CLIENT that invokes the Rate Intelligence
hosted agent running in a Foundry-managed container. The container itself runs
agents/rate_intelligence_server.py (LangGraph + azure-ai-agentserver-responses).

Architecture:
  Browser ──► Orchestrator ──► RateIntelligenceAgent (this file)
                                        │
                                        ▼ Foundry Responses endpoint
                              rate-intelligence-agent (hosted container)
                                        │
                                        ▼
                               LangGraph graph (rate_intelligence_server.py)
                                        │
                                        ▼
                              Azure AI Search (IRRRL rates + news)

The hosted agent is registered in Foundry by `azd deploy` using agent.yaml.
initialize() on this client is a no-op — the agent is already registered.
run(query) calls the agent's dedicated Responses endpoint and streams SSE events.

Required environment variables (set by azd env after provisioning):
  FOUNDRY_PROJECT_ENDPOINT — Foundry project data-plane endpoint
"""

import logging
import os
from typing import AsyncGenerator

from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# 1. AGENT CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

# Must match the `name:` field in agent.yaml.
_AGENT_NAME = "rate-intelligence-agent"

# ═══════════════════════════════════════════════════════════════════════════════
# 2. CLIENT CLASS
# ═══════════════════════════════════════════════════════════════════════════════


class RateIntelligenceAgent:
    """
    Orchestrator-side client for the VA Loan Rate Intelligence hosted agent.

    The hosted agent runs as a LangGraph container in Foundry (registered via
    agent.yaml + azd deploy). This client invokes it via the Foundry Responses
    endpoint and yields SSE-compatible events to the orchestrator.

    Lifecycle:
      1. initialize() — no-op; registration is handled by azd deploy
      2. run(query)   — calls the hosted agent endpoint, streams SSE events
      3. close()      — releases async credentials
    """

    def __init__(self) -> None:
        self._credential: DefaultAzureCredential | None = None

    def _get_credential(self) -> DefaultAzureCredential:
        if self._credential is None:
            self._credential = DefaultAzureCredential()
        return self._credential

    def _agent_openai_client(self) -> AsyncAzureOpenAI:
        """
        Return an AsyncAzureOpenAI client pre-pointed at the hosted agent's
        dedicated Responses endpoint.

        Endpoint pattern:
          {project_endpoint}/agents/{name}/endpoint/protocols/openai/v1
        """
        project_endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
        # The Foundry hosted-agent Responses base URL has NO /v1 segment.
        # The SDK appends /responses automatically, giving:
        #   {project_endpoint}/agents/{name}/endpoint/protocols/openai/responses
        agent_base = (
            f"{project_endpoint}/agents/{_AGENT_NAME}"
            "/endpoint/protocols/openai"
        )
        credential = self._get_credential()
        token_provider = get_bearer_token_provider(
            credential, "https://ai.azure.com/.default"
        )
        return AsyncAzureOpenAI(
            base_url=agent_base,
            azure_ad_token_provider=token_provider,
            api_version="2025-11-15-preview",
        )

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """
        No-op — the hosted agent is registered by `azd deploy` via agent.yaml.
        This method exists so the orchestrator can treat all agents uniformly.
        """
        logger.info(
            "rate_intelligence_agent: hosted agent '%s' managed by azd deploy — "
            "no initialization needed",
            _AGENT_NAME,
        )

    # ── Run (Main Entry Point) ─────────────────────────────────────────────

    async def run(self, query: str) -> AsyncGenerator[dict, None]:
        """
        Invoke the Rate Intelligence hosted agent and stream SSE events.

        Event sequence:
          rate_start   → agent activated
          rate_result  → rate data retrieved
          _rate_text   → full response text (consumed by orchestrator)
        """
        yield {"type": "rate_start", "message": "Rate Intelligence Agent activated"}
        yield {
            "type": "rate_source",
            "message": "Searching: IRRRL rates knowledge base",
            "source_id": "rate_knowledge_base",
        }

        openai_client = self._agent_openai_client()
        try:
            response = await openai_client.responses.create(
                model=_AGENT_NAME,
                input=[{"role": "user", "content": query}],
            )
            response_text: str = response.output_text or ""
        except Exception as exc:
            logger.exception("rate_intelligence_agent: hosted agent call failed")
            yield {"type": "error", "message": f"Rate Intelligence error: {exc}"}
            return

        yield {
            "type": "rate_result",
            "message": "Rate data retrieved from knowledge base",
        }
        yield {"type": "_rate_text", "text": response_text}

    # ── Cleanup ────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Release async credentials."""
        if self._credential:
            await self._credential.close()