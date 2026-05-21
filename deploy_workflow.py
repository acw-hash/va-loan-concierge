"""
Deploy the VA Loan Concierge workflow agent to Azure AI Foundry.

Registers all six sub-agents (orchestrator, advisor, calculator, scheduler,
newsletter, calendar) and uploads the workflow YAML as a WorkflowAgentDefinition.

Prerequisites:
  - az login (DefaultAzureCredential uses AzureCliCredential locally)
  - .env populated with all required environment variables
  - MCP Function App deployed (mcp-server/)
  - Knowledge Base created in Foundry portal

Usage:
  python deploy_workflow.py
"""

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import (
    MCPTool,
    PromptAgentDefinition,
    WorkflowAgentDefinition,
)
from azure.core.exceptions import ResourceNotFoundError
from azure.core.pipeline import PipelineRequest
from azure.core.pipeline.policies import SansIOHTTPPolicy
from azure.identity.aio import DefaultAzureCredential


class _WorkflowPreviewPolicy(SansIOHTTPPolicy):
    """Inject the Foundry-Features header required for workflow agents (preview)."""

    def on_request(self, request: PipelineRequest) -> None:
        request.http_request.headers["Foundry-Features"] = "WorkflowAgents=V1Preview"

from agents.advisor_agent import AdvisorAgent
from agents.calculator_agent import CalculatorAgent
from agents.calendar_agent import CalendarAgent
from agents.newsletter_agent import NewsletterAgent
from agents.scheduler_agent import SchedulerAgent

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("deploy_workflow")

# ── Workflow-specific orchestrator instructions ─────────────────────────────
# Stricter than the Python backend version: ALWAYS output JSON, never
# conversational text.  The workflow parses this via output.responseObject.

WORKFLOW_ORCHESTRATOR_INSTRUCTIONS = """\
You are a routing classifier for a VA mortgage lender's multi-agent system.

Your ONLY job is to read the Veteran's query and output a JSON routing decision.
You must ALWAYS respond with ONLY a valid JSON object — no explanation, no
markdown, no code fences, no preamble. Just the raw JSON.

Output format (exactly):
{"needs_advisor": <bool>, "needs_calculator": <bool>, "needs_scheduler": <bool>, "needs_newsletter": <bool>, "needs_rate_intelligence": <bool>, "response": <string>}

Routing rules:

  needs_advisor = true when the query involves:
    — eligibility questions, IRRRL qualification, VA loan benefits, entitlement,
      funding fee rules, property requirements, loan process steps, FAQ, myths,
      second-time use, surviving spouse rules, or anything the Veteran needs to
      understand before taking action.

  needs_calculator = true when the query involves:
    — refinance savings calculations, monthly savings, break-even timelines,
      closing costs, VA net tangible benefit test.

  needs_scheduler = true when the query involves:
    — scheduling/booking an appointment with a loan officer, checking
      availability, creating calendar events, managing meetings.

  needs_newsletter = true when the query involves:
    — requests for a market intelligence digest, weekly summary, news briefing,
      "send me the digest", "what's happening in the market", "weekly update",
      "market intel", "latest VA news", "news summary", "mortgage news",
      "rate trends", "industry report".

  needs_rate_intelligence = true when the query involves:
    — current IRRRL rates, VA loan rate information, mortgage rate comparisons,
      "what are the rates", "current VA rates", "rate intel", "rate data",
      VA loan programs and pricing, or any rate-specific question.

Multiple may be true for mixed queries (e.g. "Am I eligible AND show me my savings
AND book Thursday").

The "response" field:
  — When ANY of the flags is true, set "response" to "".
  — When ALL flags are false, the query is general or meta (e.g. "What can
    you do?", "Hello", "How does this work?"). In that case, write a friendly,
    concise answer in "response" describing what you can help with. Mention the
    five capabilities:
      1. Answer VA loan eligibility and guideline questions (grounded in official
         VA guidelines, lender products, and borrower FAQ)
      2. Calculate refinance savings (monthly savings, break-even, closing costs)
      3. Schedule an appointment with a loan officer and add it to your calendar
      4. Generate a weekly VA mortgage market intelligence digest
      5. Provide current IRRRL and VA loan rate intelligence
    Keep it conversational and invite the Veteran to ask a specific question.

Do NOT default to needs_advisor for general/meta queries. Only set needs_advisor
to true when the Veteran is asking a substantive VA loan question.
"""


async def _initialize_with_project_retry(
    label: str,
    init_coro,
    *,
    max_attempts: int = 4,
    base_delay_seconds: float = 2.0,
) -> None:
    """
    Retry agent initialization when Foundry project propagation is still in-flight.

    Immediately after project creation, data-plane APIs can briefly return
    `ResourceNotFoundError: Project not found`. This wrapper retries only that
    transient condition and re-raises any other failure.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            await init_coro()
            return
        except ResourceNotFoundError as exc:
            is_project_not_found = "Project not found" in str(exc)
            if (not is_project_not_found) or attempt == max_attempts:
                raise
            delay = base_delay_seconds * attempt
            logger.warning(
                "%s init hit transient 'Project not found' (attempt %d/%d). Retrying in %.1fs...",
                label,
                attempt,
                max_attempts,
                delay,
            )
            await asyncio.sleep(delay)


async def main() -> None:
    logger.info("Starting workflow deployment...")

    # ── Step 1: Initialize sub-agents (creates ARM connections + registers) ──
    logger.info("Step 1/3: Registering sub-agents...")

    advisor = AdvisorAgent()
    calculator = CalculatorAgent()
    scheduler = SchedulerAgent()
    newsletter = NewsletterAgent()

    # Required sub-agents: must succeed or deployment should fail.
    init_tasks = [
        _initialize_with_project_retry("Advisor Agent", advisor.initialize),
        _initialize_with_project_retry("Calculator Agent", calculator.initialize),
        _initialize_with_project_retry("Scheduler Agent", scheduler.initialize),
        _initialize_with_project_retry("Newsletter Agent", newsletter.initialize),
    ]

    # Calendar agent is optional and depends on a manual Work IQ connection.
    # If it is missing or invalid, continue deployment for all other agents.
    calendar: CalendarAgent | None = None
    calendar_endpoint = os.environ.get("SCHEDULER_CALENDAR_ENDPOINT")
    calendar_connection = os.environ.get("SCHEDULER_CALENDAR_CONNECTION")
    if calendar_endpoint and calendar_connection:
        calendar = CalendarAgent()
    elif calendar_endpoint or calendar_connection:
        logger.warning(
            "Calendar env is partially configured; skipping Calendar Agent registration. "
            "Set both SCHEDULER_CALENDAR_ENDPOINT and SCHEDULER_CALENDAR_CONNECTION "
            "to enable it."
        )
    else:
        logger.info("SCHEDULER_CALENDAR_ENDPOINT not set — skipping Calendar Agent registration")

    await asyncio.gather(*init_tasks)

    if calendar is not None:
        try:
            await _initialize_with_project_retry("Calendar Agent", calendar.initialize)
        except Exception as exc:
            logger.warning(
                "Calendar Agent registration skipped (optional) due to error: %s",
                exc,
            )
            calendar = None

    logger.info(
        "Sub-agents registered — advisor=%s, calculator=%s, scheduler=%s, newsletter=%s, calendar=%s",
        advisor.agent_version,
        calculator.agent_id,
        scheduler.agent_id,
        newsletter.agent_version,
        calendar.agent_id if calendar else "SKIPPED",
    )

    # ── Step 2: Register workflow-specific orchestrator ──────────────────────
    logger.info("Step 2/3: Registering workflow orchestrator...")

    credential = DefaultAzureCredential()
    client = AIProjectClient(
        endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        credential=credential,
        per_call_policies=[_WorkflowPreviewPolicy()],
    )

    orch_version = await client.agents.create_version(
        agent_name="va-loan-orchestrator",
        description="VA Loan Concierge — routing classifier (workflow mode)",
        definition=PromptAgentDefinition(
            model=os.environ["FOUNDRY_MODEL_DEPLOYMENT"],
            instructions=WORKFLOW_ORCHESTRATOR_INSTRUCTIONS,
        ),
    )
    logger.info("Orchestrator registered — version=%s", orch_version.version)

    # ── Step 3: Upload workflow ──────────────────────────────────────────────
    logger.info("Step 3/3: Uploading workflow...")

    workflow_yaml = Path("workflow.yaml").read_text(encoding="utf-8")

    workflow_version = await client.agents.create_version(
        agent_name="va-loan-concierge-workflow",
        description="VA Loan Concierge — multi-agent workflow (advisor + calculator + scheduler + newsletter + calendar + rate-intelligence)",
        definition=WorkflowAgentDefinition(workflow=workflow_yaml),
    )
    logger.info(
        "Workflow deployed — name=va-loan-concierge-workflow, version=%s",
        workflow_version.version,
    )

    # ── Cleanup ─────────────────────────────────────────────────────────────
    await advisor.close()
    await calculator.close()
    await scheduler.close()
    await newsletter.close()
    if calendar:
        await calendar.close()
    await client.close()
    await credential.close()

    logger.info("Deployment complete! Test in the Foundry portal playground.")
    logger.info(
        "Open your project → Build → Agents → va-loan-concierge-workflow → Playground"
    )


if __name__ == "__main__":
    asyncio.run(main())
