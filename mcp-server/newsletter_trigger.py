"""
Azure Function triggers for the VA mortgage market intelligence newsletter.

Two triggers share the same NewsletterAgent logic:

  newsletter_timer — runs automatically every Monday at 09:00 UTC
  newsletter_now   — HTTP POST /newsletter for manual/on-demand invocations

Both triggers call NewsletterAgent.run() and return a JSON summary:
  {subject, period_days, articles, status}

The newsletter agent queries the same Foundry IQ Knowledge Base as the Advisor
Agent (news-articles + loan-guidelines sources) and produces a structured
markdown digest organized into five market intelligence categories:
  - Market Trends
  - Regulatory & Policy
  - Competitor & Industry Moves
  - Client & Partner News
  - Industry Events

Phase 15 (chat-rendered):  The digest is returned as JSON from the HTTP trigger
and rendered in the chat UI by the orchestrator.

Phase 15b (ACS email):  Email delivery via Azure Communication Services will
be added in a future phase. The trigger will call send_digest() from
tools/newsletter_tool.py after the agent produces the markdown.

Source of truth:
  The NewsletterAgent code lives in agents/newsletter_agent.py at the repo root.
  At deploy time, postprovision.ps1 (or a manual copy) places newsletter_agent.py
  into mcp-server/ so the Function App can import it directly.
  *** If you modify agents/newsletter_agent.py, copy it to mcp-server/ before
      publishing the Function App (or run `azd up`). ***

Environment variables:
  FOUNDRY_PROJECT_ENDPOINT    — Foundry project data-plane endpoint
  FOUNDRY_MODEL_DEPLOYMENT    — e.g. gpt-4.1
  ADVISOR_KNOWLEDGE_BASE_NAME — KB index name in Azure AI Search
  ADVISOR_SEARCH_ENDPOINT     — Azure AI Search service endpoint
  FOUNDRY_PROJECT_RESOURCE_ID — ARM resource ID of the Foundry project
  ADVISOR_MCP_CONNECTION      — RemoteTool connection (shared with AdvisorAgent)
"""

import asyncio
import json
import logging
import os

import azure.functions as func

logger = logging.getLogger(__name__)

# Register triggers on a Blueprint and let function_app.py attach it.
bp = func.Blueprint()


# ── Shared helper ─────────────────────────────────────────────────────────────

def _run_newsletter(period_days: int = 7) -> dict:
    """
    Instantiate NewsletterAgent and run the digest pipeline synchronously.

    The agent's run() method is an async generator. We collect all events,
    extract the _newsletter_text payload, and return a summary dict.

    newsletter_agent.py is copied into mcp-server/ at deploy time so the
    Function App can import it without a parent-directory path hack.
    Source of truth: agents/newsletter_agent.py at the repo root.
    """
    from newsletter_agent import NewsletterAgent

    async def _collect() -> dict:
        agent = NewsletterAgent()
        await agent.resolve_version()  # look up existing version; fails if backend hasn't run yet
        digest_text = ""
        article_count = 0
        async for event in agent.run(period_days=period_days):
            if event.get("type") == "_newsletter_text":
                digest_text = event.get("text", "")
            elif event.get("type") == "newsletter_tool_result":
                msg = event.get("message", "")
                # Extract article count from message like "Digest compiled — 12 source(s) referenced"
                parts = msg.split("—")
                if len(parts) > 1:
                    try:
                        article_count = int(parts[1].strip().split()[0])
                    except (ValueError, IndexError):
                        article_count = 0
        await agent.close()
        return {
            "subject": f"VA Mortgage Market Intelligence Digest",
            "period_days": period_days,
            "articles": article_count,
            "digest": digest_text,
            "status": "complete" if digest_text else "empty",
        }

    return asyncio.run(_collect())


# ── Timer Trigger — every Monday at 09:00 UTC ─────────────────────────────────

@bp.timer_trigger(
    arg_name="timer",
    schedule="0 0 9 * * 1",   # Every Monday at 09:00 UTC
    run_on_startup=False,      # Don't run on cold start — wait for schedule
)
def newsletter_timer(timer: func.TimerRequest) -> None:
    """
    Scheduled weekly digest — queries the KB for the past 7 days of news and
    produces a structured market intelligence digest. Email delivery (Phase 15b)
    will be wired here once ACS is provisioned.
    """
    if timer.past_due:
        logger.warning("newsletter_timer: timer is past due — running now")

    logger.info("newsletter_timer: starting weekly digest generation")
    result = _run_newsletter(period_days=7)
    logger.info(
        "newsletter_timer: complete — %d articles, status=%s",
        result.get("articles", 0),
        result.get("status"),
    )


# ── HTTP Trigger — manual/on-demand invocation ────────────────────────────────

@bp.route(route="newsletter", methods=["POST"])
def newsletter_now(req: func.HttpRequest) -> func.HttpResponse:
    """
    On-demand newsletter trigger for testing and manual generation.

    POST /newsletter

    Optional JSON body:
        {"period_days": 14}   — override look-back window (default: 7)

    Returns JSON:
        {
          "subject": "VA Mortgage Market Intelligence Digest",
          "period_days": 7,
          "articles": 12,
          "digest": "## Market Trends\\n...",
          "status": "complete"
        }

    Example:
        curl -X POST https://<func-app>.azurewebsites.net/newsletter
        curl -X POST https://<func-app>.azurewebsites.net/newsletter \\
             -H "Content-Type: application/json" \\
             -d '{"period_days": 14}'
    """
    logger.info("newsletter_now: manual digest triggered")

    period_days = 7
    try:
        body = req.get_json()
        if isinstance(body, dict) and "period_days" in body:
            period_days = int(body["period_days"])
    except (ValueError, TypeError):
        pass

    try:
        result = _run_newsletter(period_days=period_days)
        return func.HttpResponse(
            json.dumps(result),
            status_code=200,
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:
        logger.exception("newsletter_now: error")
        return func.HttpResponse(
            json.dumps({"error": str(exc)}),
            status_code=500,
            headers={"Content-Type": "application/json"},
        )
