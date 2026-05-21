"""
VA Loan Concierge — Orchestrator Agent.

The single entry point for all Veteran queries.  Routes each query to the
appropriate specialized agent(s), collects their results, and streams
per-agent partial responses back to the UI.

Architecture:
  Browser ──► FastAPI ──► Orchestrator ──┬──► AdvisorAgent    (Foundry IQ KB)
              (SSE)       (this file)    ├──► CalculatorAgent  (Custom MCP)
                                         ├──► SchedulerAgent   (Custom MCP)
                                         └──► CalendarAgent    (Work IQ Calendar)

Routing is LLM-driven: the orchestrator is itself a registered Foundry agent
that classifies each query via the Responses API into a 4-way boolean:
  (needs_advisor, needs_calculator, needs_scheduler, needs_newsletter)

Multiple may be true for mixed queries.  Keyword classification serves as a
fallback if the LLM call fails.

Human-in-the-loop (HIL) flows:
  - Calculator: pauses to collect loan details when no profile is loaded,
    with up to 3 retries before auto-skipping
  - Appointment: pauses for confirmation after booking — confirm (→ calendar),
    reschedule (→ re-run scheduler), or decline (→ skip calendar)

State between turns is tracked in api/conversation_state.py (Cosmos DB or in-memory fallback, 10-min TTL).
"""

import asyncio
import json
import logging
import os
from typing import AsyncGenerator

from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.identity.aio import DefaultAzureCredential

from api.telemetry import get_tracer
from agents.advisor_agent import AdvisorAgent
from agents.calculator_agent import CalculatorAgent
from agents.calendar_agent import CalendarAgent
from agents.newsletter_agent import NewsletterAgent
from agents.scheduler_agent import SchedulerAgent
from agents.rate_intelligence_agent import RateIntelligenceAgent
from api.conversation_state import (
    ConversationState,
    create_conversation,
    get_conversation,
    save_conversation,
)
from profiles import DEMO_PROFILES, _profile_context_block, _demo_context_block

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# 1. AGENT CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

# URL-safe agent name — visible in the Foundry portal under Build > Agents.
_ORCHESTRATOR_NAME = "va-loan-orchestrator"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. ORCHESTRATOR INSTRUCTIONS (the LLM system prompt for routing)
# ═══════════════════════════════════════════════════════════════════════════════
# The orchestrator LLM classifies each query into a JSON object with three
# boolean flags.  It does NOT answer questions directly — it only routes.
# When all flags are false (general/meta query), the LLM writes a friendly
# capabilities response in the "response" field.

ORCHESTRATOR_INSTRUCTIONS = """\
You are the VA Loan Concierge — a routing coordinator for a VA mortgage lender.

Your primary job is to classify each Veteran's query and decide which specialist
agent(s) to invoke:

  VA Loan Advisor (needs_advisor: true)
    — eligibility questions, IRRRL qualification, VA loan benefits, entitlement,
      funding fee rules, property requirements, loan process steps, FAQ, myths,
      second-time use, surviving spouse rules, or anything the Veteran needs to
      understand before taking action.

  Loan Calculator (needs_calculator: true)
    — refinance savings calculations, monthly savings, break-even timelines,
      closing costs, VA net tangible benefit test.

  Loan Scheduler (needs_scheduler: true)
    — scheduling/booking an appointment with a loan officer, checking
      availability, creating calendar events, managing meetings.

  Newsletter Agent (needs_newsletter: true)
    — requests for a market intelligence digest, weekly summary, news briefing,
      "send me the digest", "what's happening in the market", "weekly update",
      "market intel", "latest VA news", "news summary".

  Rate Intelligence Agent (needs_rate_intelligence: true)
    — requests for information about IRRRL rates, VA loan programs, or other
    VA mortgage-related topics.

When asked to classify a query, respond with ONLY a valid JSON object:
  {"needs_advisor": <bool>, "needs_calculator": <bool>, "needs_scheduler": <bool>, "needs_newsletter": <bool>, "needs_rate_intelligence": <bool>, "response": <string>}

Multiple may be true for mixed queries (e.g. "Am I eligible AND show me my savings
AND book Thursday").

The "response" field:
  — When ANY of the four flags is true, set "response" to "".
  — When ALL four flags are false, the query is general or meta (e.g. "What can
    you do?", "Hello", "How does this work?"). Write a friendly, concise answer
    describing your four capabilities: (1) answering VA loan eligibility and
    guideline questions, (2) calculating refinance savings, (3) scheduling
    appointments with a loan officer, and (4) generating a weekly market
    intelligence digest. Invite the Veteran to ask a specific question.

Do NOT default to needs_advisor for general/meta queries. Only set needs_advisor
to true when the Veteran is asking a substantive VA loan question.
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 3. KEYWORD ROUTING (fallback when LLM classification fails)
# ═══════════════════════════════════════════════════════════════════════════════
# If the LLM classification call fails (network error, unparseable output),
# we fall back to keyword matching.  This ensures the demo always works.

_ADVISOR_KEYWORDS: frozenset[str] = frozenset({
    "eligib", "qualify", "can i", "entitlement", "coe", "benefit",
    "requirement", "guideline", "myth", "misconception", "process",
    "step", "faq", "second time", "again", "surviving spouse",
    "discharge", "service-connected", "appraisal", "irrrl", "refinanc",
})

_CALCULATOR_KEYWORDS: frozenset[str] = frozenset({
    "calculat", "saving", "save", "how much", "monthly", "payment",
    "rate", "break-even", "closing cost",
})

_SCHEDULER_KEYWORDS: frozenset[str] = frozenset({
    "schedule", "book", "appointment", "call for", "meeting",
    "calendar", "availab",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
})

_NEWSLETTER_KEYWORDS: frozenset[str] = frozenset({
    "digest", "newsletter", "weekly", "market intel", "market intelligence",
    "news summary", "news briefing", "latest news", "what's happening",
    "market update", "weekly update", "send me the", "industry news",
    "latest va news", "rate news", "policy news",
})


_GENERAL_RESPONSE = (
    "I'm your VA Loan Concierge! Here's what I can help you with:\n\n"
    "1. **VA Loan Guidance** — I can answer questions about eligibility, IRRRL "
    "qualification, entitlement, funding fees, and the loan process, all grounded "
    "in official VA guidelines and lender products.\n\n"
    "2. **Refinance Savings** — I can calculate your monthly savings, break-even "
    "timeline, and closing costs if you're considering a refinance.\n\n"
    "3. **Appointment Scheduling** — I can book a consultation with a loan officer "
    "and add it to your calendar.\n\n"
    "4. **Market Intelligence Digest** — I can generate a weekly summary of VA "
    "mortgage market trends, regulatory updates, and industry news.\n\n"
    "What would you like to know?"
)


def _classify_hint(query: str) -> tuple[bool, bool, bool, bool, bool, str]:
    """
    Keyword-based pre-classification for routing decisions.
    Returns general capabilities response when no keywords match.
    """
    q = query.lower()
    needs_advisor = any(kw in q for kw in _ADVISOR_KEYWORDS)
    needs_calculator = any(kw in q for kw in _CALCULATOR_KEYWORDS)
    needs_scheduler = any(kw in q for kw in _SCHEDULER_KEYWORDS)
    needs_newsletter = any(kw in q for kw in _NEWSLETTER_KEYWORDS)
    needs_rate_intelligence = any(kw in q for kw in [
        "irrrl rate", "va loan program", "va mortgage", "current rate",
        "rate intel", "rate information", "mortgage rate",
    ])
    if not needs_advisor and not needs_calculator and not needs_scheduler and not needs_newsletter and not needs_rate_intelligence:
        return False, False, False, False, False, _GENERAL_RESPONSE
    return needs_advisor, needs_calculator, needs_scheduler, needs_newsletter, needs_rate_intelligence, ""


def _route_label(
    needs_advisor: bool,
    needs_calculator: bool,
    needs_scheduler: bool,
    needs_newsletter: bool,
    needs_rate_intelligence: bool,
) -> str:
    agents = []
    if needs_advisor:
        agents.append("Advisor Agent")
    if needs_calculator:
        agents.append("Calculator Agent")
    if needs_scheduler:
        agents.append("Scheduler Agent")
    if needs_newsletter:
        agents.append("Newsletter Agent")
    if needs_rate_intelligence:
        agents.append("Rate Intelligence Agent")
    return " + ".join(agents) if agents else "Concierge (general)"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. ORCHESTRATOR CLASS
# ═══════════════════════════════════════════════════════════════════════════════

class Orchestrator:
    """
    VA Loan Concierge orchestrator.

    Uses Python-level routing — calls sub-agents directly and forwards their
    events to the client in real time. Also registered as a new Azure AI
    Foundry agent (visible in the portal) but orchestration runs in Python.

    Instantiate once and reuse across multiple queries (the API server
    holds a single instance). Call initialize() before the first run().

    Usage::

        orch = Orchestrator()
        await orch.initialize()
        async for event in orch.run(query):
            send_to_client(event)
        await orch.close()
    """

    def __init__(self) -> None:
        self._credential: DefaultAzureCredential | None = None
        self._client: AIProjectClient | None = None
        self._advisor: AdvisorAgent | None = None
        self._calculator: CalculatorAgent | None = None
        self._scheduler: SchedulerAgent | None = None
        self._calendar: CalendarAgent | None = None
        self._newsletter: NewsletterAgent | None = None
        self._orchestrator_version: str | None = None
        self.rate_intelligence_agent = RateIntelligenceAgent()

    # ── Client Setup ───────────────────────────────────────────────────────

    def _get_client(self) -> AIProjectClient:
        if self._client is None:
            self._credential = DefaultAzureCredential()
            self._client = AIProjectClient(
                endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
                credential=self._credential,
            )
        return self._client

    # ── Agent Registration (initialize) ────────────────────────────────────
    # Creates all four sub-agents, initializes them concurrently, then
    # registers the orchestrator itself as a Foundry agent (for LLM routing).

    async def initialize(self) -> None:
        """
        Initialize sub-agents and register the orchestrator as a Foundry agent.

        Sub-agents are initialized concurrently — advisor KB connection runs
        in parallel with calculator/scheduler/calendar agent registration.
        """
        logger.info("orchestrator: initializing sub-agents")
        self._advisor = AdvisorAgent()
        self._calculator = CalculatorAgent()
        self._scheduler = SchedulerAgent()
        self._calendar = CalendarAgent()
        self._newsletter = NewsletterAgent()

        # Initialize sub-agents concurrently.
        await asyncio.gather(
            self._advisor.initialize(),
            self._calculator.initialize(),
            self._scheduler.initialize(),
            self._calendar.initialize(),
            self._newsletter.initialize(),
            self.rate_intelligence_agent.initialize(),
        )
        logger.info("orchestrator: sub-agents ready")

        # Register orchestrator as a new Foundry agent (portal visibility).
        client = self._get_client()
        model = os.environ["FOUNDRY_MODEL_DEPLOYMENT"]

        version_details = await client.agents.create_version(
            agent_name=_ORCHESTRATOR_NAME,
            description="VA Loan Concierge — multi-agent orchestrator",
            definition=PromptAgentDefinition(
                model=model,
                instructions=ORCHESTRATOR_INSTRUCTIONS,
            ),
        )
        self._orchestrator_version = version_details.version
        logger.info(
            "orchestrator: created Foundry agent '%s' version=%s",
            _ORCHESTRATOR_NAME,
            self._orchestrator_version,
        )

    # ── LLM-Driven Routing ───────────────────────────────────────────────
    # The orchestrator calls its own registered Foundry agent to classify
    # the query.  The LLM returns a JSON object with three boolean flags
    # and an optional general response string.

    async def _llm_classify(self, query: str) -> tuple[bool, bool, bool, bool, bool, str]:
        """
        Use the orchestrator Foundry agent to classify routing via LLM inference.

        Returns (needs_advisor, needs_calculator, needs_scheduler, needs_newsletter, needs_rate_intelligence, response).
        When all four flags are false, ``response`` contains a general capabilities
        message from the LLM. Otherwise ``response`` is "".

        Falls back to keyword classification if the LLM call fails or returns
        unparseable output.
        """
        if not self._orchestrator_version:
            logger.debug("orchestrator: no version set — using keyword fallback")
            return _classify_hint(query)

        client = self._get_client()
        openai_client = client.get_openai_client()
        model = os.environ["FOUNDRY_MODEL_DEPLOYMENT"]

        classify_prompt = (
            "Classify the following Veteran's query. Respond with ONLY a JSON object "
            "with five fields — needs_advisor (bool), needs_calculator (bool), "
            "needs_scheduler (bool), needs_newsletter (bool), needs_rate_intelligence (bool), and response (string) "
            "— and nothing else.\n\n"
            f'Query: "{query}"'
        )

        try:
            response = await openai_client.responses.create(
                model=model,
                input=[{"role": "user", "content": classify_prompt}],
                extra_body={
                    "agent_reference": {
                        "name": _ORCHESTRATOR_NAME,
                        "version": self._orchestrator_version,
                        "type": "agent_reference",
                    }
                },
            )
            text = (response.output_text or "").strip()
            # Strip markdown code fences if the model wraps the JSON.
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            data = json.loads(text)
            needs_advisor = bool(data.get("needs_advisor", False))
            needs_calculator = bool(data.get("needs_calculator", False))
            needs_scheduler = bool(data.get("needs_scheduler", False))
            needs_newsletter = bool(data.get("needs_newsletter", False))
            needs_rate_intelligence = bool(data.get("needs_rate_intelligence", False))
            general_response = str(data.get("response", ""))
            logger.info(
                "orchestrator: LLM routing — advisor=%s  calculator=%s  scheduler=%s  newsletter=%s  has_response=%s",
                needs_advisor, needs_calculator, needs_scheduler, needs_newsletter,
                bool(general_response),
            )
            return needs_advisor, needs_calculator, needs_scheduler, needs_newsletter, needs_rate_intelligence, general_response
        except Exception as exc:
            logger.warning(
                "orchestrator: LLM classification failed, falling back to keywords: %s", exc
            )
            return _classify_hint(query)

    # ── HIL: Appointment Confirmation Classification ──────────────────────
    # After the scheduler books an appointment, the orchestrator pauses and
    # asks the user to confirm, reschedule, or decline.  This method
    # classifies the user's response using LLM with keyword fallback.

    async def _classify_confirmation(self, user_response: str) -> str:
        """
        Classify a user's response to an appointment confirmation prompt.

        Returns one of: "confirm", "reschedule", "decline".
        Uses LLM classification with keyword fallback.
        """
        if not self._orchestrator_version:
            return self._keyword_classify_confirmation(user_response)

        prompt = (
            "The user was shown an appointment and asked to confirm, reschedule, "
            "or decline. Classify their response as EXACTLY one of: confirm, "
            "reschedule, decline.\n\n"
            f'User response: "{user_response}"\n\n'
            "Respond with ONLY one word: confirm, reschedule, or decline."
        )
        try:
            client = self._get_client()
            openai_client = client.get_openai_client()
            model = os.environ["FOUNDRY_MODEL_DEPLOYMENT"]
            response = await openai_client.responses.create(
                model=model,
                input=[{"role": "user", "content": prompt}],
                extra_body={
                    "agent_reference": {
                        "name": _ORCHESTRATOR_NAME,
                        "version": self._orchestrator_version,
                        "type": "agent_reference",
                    }
                },
            )
            text = (response.output_text or "").strip().lower()
            if text in ("confirm", "reschedule", "decline"):
                return text
            # LLM returned something unexpected — fall back to keywords.
            return self._keyword_classify_confirmation(user_response)
        except Exception:
            logger.warning("orchestrator: confirmation classification failed, using keywords")
            return self._keyword_classify_confirmation(user_response)

    @staticmethod
    def _keyword_classify_confirmation(text: str) -> str:
        t = text.lower()
        decline_kw = ("no", "skip", "don't", "cancel", "decline", "not now", "no thanks")
        reschedule_kw = (
            "instead", "change", "different", "reschedule", "another",
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
            "morning", "afternoon", "earlier", "later",
        )
        if any(kw in t for kw in decline_kw):
            return "decline"
        if any(kw in t for kw in reschedule_kw):
            return "reschedule"
        return "confirm"

    # ── Run (Main Entry Point) ─────────────────────────────────────────────
    # The FastAPI server calls run(query, profile_id, conversation_id) and
    # iterates over the yielded SSE events.  If conversation_id matches a
    # paused conversation, the flow resumes from where it left off (HIL).

    async def run(
        self,
        query: str,
        profile_id: str | None = None,
        conversation_id: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        """
        Orchestrate a query, yielding SSE-compatible events.

        If conversation_id is provided and matches a paused conversation,
        this resumes from where it left off (human-in-the-loop). Otherwise
        a new conversation is started.
        """
        if self._advisor is None:
            try:
                await self.initialize()
            except Exception as exc:
                logger.exception("orchestrator: initialization failed")
                yield {"type": "error", "message": f"Initialization error: {exc}"}
                return

        # Check for an existing paused conversation to resume.
        state: ConversationState | None = None
        if conversation_id:
            state = await get_conversation(conversation_id)

        if state and state.pending_action:
            async for event in self._resume(state, query):
                yield event
        else:
            async for event in self._run_new(query, profile_id):
                yield event

    # ── New Conversation Flow ─────────────────────────────────────────────
    # Full pipeline: classify → advisor → calculator → scheduler → calendar.
    # May pause at any point for human-in-the-loop input.

    async def _run_new(
        self, query: str, profile_id: str | None = None
    ) -> AsyncGenerator[dict, None]:
        """Run a fresh conversation, pausing for human input when needed."""

        state = await create_conversation(profile_id=profile_id, original_query=query)

        # Build the context-enriched query once — used by all agents.
        profile_ctx = _profile_context_block(profile_id)
        enriched_query = profile_ctx + "\n\n" + query
        state.enriched_query = enriched_query

        # ── Pre-run UX events ────────────────────────────────────────────
        yield {"type": "orchestrator_start", "message": "Analyzing your query..."}
        await asyncio.sleep(0.15)

        tracer = get_tracer()
        with tracer.start_as_current_span(
            "orchestrator.classify",
            attributes={"query": query[:500]},
        ):
            needs_advisor, needs_calculator, needs_scheduler, needs_newsletter, needs_rate_intelligence, general_response = (
                await self._llm_classify(query)
            )
        state.needs_advisor = needs_advisor
        state.needs_calculator = needs_calculator
        state.needs_scheduler = needs_scheduler
        await save_conversation(state)
        route_label = _route_label(needs_advisor, needs_calculator, needs_scheduler, needs_newsletter, needs_rate_intelligence)
        yield {
            "type": "orchestrator_route",
            "message": f"Routing to: {route_label}",
        }

        # ── General / meta query — respond directly, no sub-agents ─────
        if not needs_advisor and not needs_calculator and not needs_scheduler and not needs_newsletter and not needs_rate_intelligence:
            response_text = general_response or _GENERAL_RESPONSE
            yield {
                "type": "partial_response",
                "agent": "orchestrator",
                "label": "VA Loan Concierge",
                "content": response_text,
            }
            yield {"type": "complete", "message": "Response ready"}
            return

        # Build the plan chain for the chat thread.
        plan_agents = []
        if needs_advisor:
            plan_agents.append("VA Loan Advisor")
        if needs_calculator:
            plan_agents.append("Loan Calculator")
        if needs_scheduler:
            plan_agents.append("Loan Scheduler")
            plan_agents.append("Calendar")
        if needs_newsletter:
            plan_agents.append("Newsletter Agent")
        if needs_rate_intelligence:
            plan_agents.append("Rate Intelligence Agent")
        yield {
            "type": "plan",
            "message": " → ".join(plan_agents),
        }
        await asyncio.sleep(0.15)

        has_response = False

        # ── Run advisor agent ────────────────────────────────────────────
        advisor_text = ""
        if needs_advisor and self._advisor:
            with tracer.start_as_current_span("agent.advisor"):
                try:
                    async for event in self._advisor.run(enriched_query):
                        if event["type"] == "_advisor_text":
                            advisor_text = event.get("text", "")
                        else:
                            yield event
                except Exception as exc:
                    logger.exception("orchestrator: advisor run raised unexpectedly")
                    yield {"type": "error", "message": f"Advisor error: {exc}"}
                    return

            if advisor_text:
                has_response = True
                state.advisor_text = advisor_text
                await save_conversation(state)
                yield {
                    "type": "partial_response",
                    "agent": "advisor",
                    "label": "VA Loan Advisor",
                    "content": advisor_text,
                }

        # ── HIL check: no profile + needs calculator ──────────────────────
        # If no profile is loaded and the calculator needs loan details,
        # pause to let the user provide them before continuing.
        # Scheduler does NOT need loan details — only day/time preference.
        if not profile_id and needs_calculator:
            state.pending_action = "awaiting_profile_info"
            await save_conversation(state)
            yield {
                "type": "await_input",
                "message": (
                    "To calculate your refinance savings, I need five pieces "
                    "of information:\n\n"
                    "1. **Current loan balance** (e.g. $320,000)\n"
                    "2. **Current interest rate** (e.g. 6.8%)\n"
                    "3. **New/quoted interest rate** (e.g. 6.1%)\n"
                    "4. **Remaining term** in years (e.g. 27)\n"
                    "5. **VA funding fee exempt?** (yes if you have a "
                    "service-connected disability)\n\n"
                    "Please provide all five so I can run the calculation."
                ),
                "conversation_id": state.conversation_id,
                "input_type": "profile_info",
                "suggestions": [
                    "Balance $320,000, current rate 6.8%, new rate 6.1%, 27 years left, fee exempt",
                    "Balance $400,000, current rate 7.1%, quoted rate 6.3%, 29 years remaining, not fee exempt",
                ],
            }
            return

        # ── Run newsletter agent ─────────────────────────────────────────
        # No handoff divider here — newsletter is always the only agent in its
        # routing path, and the plan event already names it. A handoff would be
        # redundant ("Newsletter Agent" + "Orchestrator → Newsletter Agent" back-to-back).
        if needs_newsletter and self._newsletter:
            with tracer.start_as_current_span("agent.newsletter"):
                newsletter_text = ""
                try:
                    async for event in self._newsletter.run():
                        if event["type"] == "_newsletter_text":
                            newsletter_text = event.get("text", "")
                        else:
                            yield event
                except Exception as exc:
                    logger.exception("orchestrator: newsletter run raised unexpectedly")
                    yield {"type": "error", "message": f"Newsletter error: {exc}"}
                    return

            if newsletter_text:
                has_response = True
                yield {
                    "type": "partial_response",
                    "agent": "newsletter",
                    "label": "Market Intelligence Digest",
                    "content": newsletter_text,
                }

        # ── Run rate intelligence agent ──────────────────────────────────
        if needs_rate_intelligence:
            if has_response:
                yield {"type": "handoff", "message": "→ Rate Intelligence Agent"}
            rate_text = ""
            with tracer.start_as_current_span("agent.rate_intelligence"):
                try:
                    async for event in self.rate_intelligence_agent.run(enriched_query):
                        if event["type"] == "_rate_text":
                            rate_text = event.get("text", "")
                        else:
                            yield event
                except Exception as exc:
                    logger.exception("orchestrator: rate intelligence run raised unexpectedly")
                    yield {"type": "error", "message": f"Rate Intelligence error: {exc}"}
                    return

            if rate_text:
                has_response = True
                yield {
                    "type": "partial_response",
                    "agent": "rate_intelligence",
                    "label": "Rate Intelligence",
                    "content": rate_text,
                }

        # ── Continue with calculator + scheduler ─────────────────────────
        async for event in self._run_calculator_through_end(state, has_response):
            yield event

    # ── Resume from HIL Pause ─────────────────────────────────────────────
    # When the orchestrator pauses for user input (await_input event), the
    # next request arrives with the same conversation_id and routes here.
    # Three pause types: awaiting_profile_info, awaiting_calculator_retry,
    # awaiting_appointment_confirmation.

    async def _resume(
        self, state: ConversationState, user_response: str
    ) -> AsyncGenerator[dict, None]:
        """Resume a paused conversation with the user's input."""

        if state.pending_action == "awaiting_profile_info":
            yield {
                "type": "orchestrator_start",
                "message": "Received your details — continuing...",
            }
            await asyncio.sleep(0.15)

            # Append the user's loan details to the enriched query so
            # downstream agents have the information they need.
            state.enriched_query += (
                f"\n\n[Borrower-provided loan details: {user_response}]\n"
                "[Use these details for the refinance calculation. "
                "Extract current_rate, new_rate, balance, remaining_term, "
                "and funding_fee_exempt from the borrower's response above.]"
            )
            state.user_provided_details = True
            state.pending_action = None
            await save_conversation(state)

            # Continue from calculator onwards.
            has_response = bool(state.advisor_text)
            async for event in self._run_calculator_through_end(state, has_response):
                yield event

        elif state.pending_action == "awaiting_calculator_retry":
            # Check if the user wants to skip the calculation.
            skip_keywords = ("skip", "move on", "don't calculate", "no calc",
                             "forget it", "never mind", "use defaults", "default")
            if any(kw in user_response.lower() for kw in skip_keywords):
                yield {
                    "type": "orchestrator_start",
                    "message": "Skipping the refinance calculation.",
                }
                yield {
                    "type": "calculator_note",
                    "message": "⚠ Calculation skipped at borrower's request.",
                }
                state.pending_action = None
                await save_conversation(state)
                # Continue to scheduler if needed, otherwise complete.
                if state.needs_scheduler and self._scheduler:
                    async for event in self._run_scheduler(state):
                        yield event
                    appointment_json = (
                        self._scheduler.extract_appointment_result(
                            self._scheduler.last_response
                        )
                        if self._scheduler.last_response
                        else None
                    )
                    if appointment_json and self._calendar:
                        state.appointment_json = appointment_json
                        state.pending_action = "awaiting_appointment_confirmation"
                        await save_conversation(state)
                        yield {
                            "type": "await_input",
                            "message": (
                                "Does this appointment work for you? I can add it to "
                                "your calendar, reschedule it, or skip the calendar event."
                            ),
                            "conversation_id": state.conversation_id,
                            "input_type": "appointment_confirmation",
                            "suggestions": [
                                "Yes, add to my calendar",
                                "Can we do a different time?",
                                "No thanks, skip the calendar event",
                            ],
                        }
                        return
                yield {"type": "complete", "message": "Response ready"}
            else:
                yield {
                    "type": "orchestrator_start",
                    "message": "Received additional details — retrying calculation...",
                }
                await asyncio.sleep(0.15)

                state.calculator_retry_count += 1
                state.enriched_query += (
                    f"\n\n[Additional details from borrower: {user_response}]\n"
                    "[Use ALL borrower-provided details to call the "
                    "refi_savings_calculator tool now.]"
                )
                state.pending_action = None
                await save_conversation(state)

                has_response = bool(state.advisor_text)
                async for event in self._run_calculator_through_end(state, has_response):
                    yield event

        elif state.pending_action == "awaiting_appointment_confirmation":
            intent = await self._classify_confirmation(user_response)
            logger.info("orchestrator: appointment confirmation classified as '%s'", intent)

            if intent == "confirm":
                yield {
                    "type": "orchestrator_start",
                    "message": "Adding appointment to your calendar...",
                }
                await asyncio.sleep(0.15)
                async for event in self._run_calendar(state):
                    yield event
                yield {"type": "complete", "message": "Response ready"}

            elif intent == "reschedule":
                yield {
                    "type": "orchestrator_start",
                    "message": "Rescheduling appointment...",
                }
                await asyncio.sleep(0.15)

                # Re-run scheduler with the user's new preference.
                async for event in self._run_scheduler(state, user_response):
                    yield event

                # Pause again for confirmation of the new time.
                appointment_json = (
                    self._scheduler.extract_appointment_result(
                        self._scheduler.last_response
                    )
                    if self._scheduler.last_response
                    else None
                )
                if appointment_json:
                    state.appointment_json = appointment_json
                    state.pending_action = "awaiting_appointment_confirmation"
                    await save_conversation(state)
                    yield {
                        "type": "await_input",
                        "message": (
                            "Here's the updated appointment. Does this work for you, "
                            "or would you like a different time?"
                        ),
                        "conversation_id": state.conversation_id,
                        "input_type": "appointment_confirmation",
                        "suggestions": [
                            "Yes, add to my calendar",
                            "No thanks, skip the calendar event",
                        ],
                    }
                else:
                    yield {"type": "complete", "message": "Response ready"}

            else:  # decline
                yield {
                    "type": "orchestrator_start",
                    "message": "Skipping calendar event.",
                }
                yield {
                    "type": "partial_response",
                    "agent": "orchestrator",
                    "label": "VA Loan Concierge",
                    "content": (
                        "No problem — I've skipped the calendar event. "
                        "Your appointment is still confirmed with the details above. "
                        "Is there anything else I can help with?"
                    ),
                }
                yield {"type": "complete", "message": "Response ready"}

        else:
            # Unknown pending action — treat as a new conversation.
            logger.warning(
                "orchestrator: unknown pending_action '%s' — starting fresh",
                state.pending_action,
            )
            async for event in self._run_new(user_response, state.profile_id):
                yield event

    # ── Shared Flow: Calculator → Scheduler → Calendar → Done ────────────
    # Used by both _run_new() and _resume() to avoid duplicating the
    # calculator-through-completion pipeline.

    async def _run_calculator_through_end(
        self, state: ConversationState, has_response: bool
    ) -> AsyncGenerator[dict, None]:
        """Run calculator → scheduler → appointment confirmation → calendar → done."""
        tracer = get_tracer()

        # ── Run calculator agent ─────────────────────────────────────────
        calculator_text = ""
        if state.needs_calculator and self._calculator:
            if state.advisor_text:
                yield {"type": "handoff", "message": "Advisor → Calculator Agent"}

            # Skip demo context injection when the user manually provided
            # their loan details via HIL — those are already in enriched_query.
            if state.user_provided_details:
                calculator_query = state.enriched_query
            else:
                calc_context, calc_notices = _demo_context_block(
                    state.original_query, state.profile_id, "calculator"
                )
                for notice in calc_notices:
                    yield {"type": "calculator_note", "message": f"⚠ {notice}"}
                calculator_query = state.enriched_query + calc_context

            tool_was_called = False
            with tracer.start_as_current_span("agent.calculator"):
                try:
                    async for event in self._calculator.run(calculator_query):
                        if event["type"] == "_calculator_text":
                            calculator_text = event.get("text", "")
                        else:
                            if event["type"] == "calculator_tool_call":
                                tool_was_called = True
                            yield event
                except Exception as exc:
                    logger.exception("orchestrator: calculator run raised unexpectedly")
                    yield {"type": "error", "message": f"Calculator error: {exc}"}
                    return

            if calculator_text:
                has_response = True
                state.calculator_text = calculator_text
                await save_conversation(state)
                yield {
                    "type": "partial_response",
                    "agent": "calculator",
                    "label": "Loan Calculator",
                    "content": calculator_text,
                }

            # ── Calculator retry: tool was NOT called → missing info ───
            # The calculator LLM asked follow-up questions instead of
            # calling the tool.  Pause for user input, up to 3 attempts.
            if not tool_was_called and state.user_provided_details:
                if state.calculator_retry_count >= 3:
                    # Max retries reached — skip calculator, continue flow.
                    yield {
                        "type": "calculator_note",
                        "message": "⚠ Could not complete calculation after multiple attempts — skipping.",
                    }
                else:
                    state.pending_action = "awaiting_calculator_retry"
                    await save_conversation(state)
                    yield {
                        "type": "await_input",
                        "message": (
                            "The calculator needs a bit more information to run. "
                            "Please provide the missing details mentioned above, "
                            "or say 'skip' to move on."
                        ),
                        "conversation_id": state.conversation_id,
                        "input_type": "calculator_retry",
                        "suggestions": [
                            "New rate 6.1%, fee exempt",
                            "Quoted rate 6.3%, not fee exempt",
                            "Skip the calculation",
                        ],
                    }
                    return

        # ── Run scheduler agent ──────────────────────────────────────────
        if state.needs_scheduler and self._scheduler:
            if state.needs_calculator and calculator_text:
                yield {"type": "handoff", "message": "Calculator → Scheduler Agent"}
            elif state.advisor_text:
                yield {"type": "handoff", "message": "Advisor → Scheduler Agent"}

            async for event in self._run_scheduler(state):
                yield event

            # ── HIL: Appointment confirmation ─────────────────────────────
            appointment_json = (
                self._scheduler.extract_appointment_result(
                    self._scheduler.last_response
                )
                if self._scheduler.last_response
                else None
            )
            if appointment_json and self._calendar:
                state.appointment_json = appointment_json
                state.pending_action = "awaiting_appointment_confirmation"
                await save_conversation(state)
                yield {
                    "type": "await_input",
                    "message": (
                        "Does this appointment work for you? I can add it to "
                        "your calendar, reschedule it, or skip the calendar event."
                    ),
                    "conversation_id": state.conversation_id,
                    "input_type": "appointment_confirmation",
                    "suggestions": [
                        "Yes, add to my calendar",
                        "Can we do a different time?",
                        "No thanks, skip the calendar event",
                    ],
                }
                return  # Wait for user response.

        # ── Done (no scheduler, or no calendar agent) ────────────────────
        if not has_response:
            yield {
                "type": "partial_response",
                "agent": "advisor",
                "label": "VA Loan Concierge",
                "content": "I'm sorry, I couldn't generate a response at this time.",
            }

        yield {"type": "complete", "message": "Response ready"}

    async def _run_scheduler(
        self,
        state: ConversationState,
        override_query: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run the scheduler agent. Yields scheduler events and partial_response."""

        if override_query:
            # Rescheduling — build explicit instructions. Do NOT use
            # sched_context (which extracts day/time from the *original*
            # query — that would conflict with the new preference).
            prev_appt = state.appointment_json or "unknown"
            current_rate = (DEMO_PROFILES.get(state.profile_id or "", {})
                           .get("current_rate"))
            appt_type = ("IRRRL review and rate lock"
                         if current_rate is not None
                         else "VA Loan Consultation")
            scheduler_query = state.enriched_query + (
                f"\n\n[RESCHEDULE REQUEST: The Veteran wants to change their "
                f"appointment. Their previous appointment was: {prev_appt}\n"
                f"The Veteran's new preference: \"{override_query}\"\n"
                f"Call the appointment_scheduler tool with the day and time "
                f"the Veteran specified above. Set appointment_type to "
                f"'{appt_type}'. You MUST use a DIFFERENT day or time than "
                f"the previous appointment.]"
            )
        else:
            sched_context, sched_notices = _demo_context_block(
                state.original_query, state.profile_id, "scheduler"
            )
            for notice in sched_notices:
                yield {"type": "scheduler_note", "message": f"⚠ {notice}"}
            scheduler_query = state.enriched_query + sched_context

        scheduler_text = ""
        tracer = get_tracer()
        with tracer.start_as_current_span("agent.scheduler"):
            try:
                async for event in self._scheduler.run(scheduler_query):
                    if event["type"] == "_scheduler_text":
                        scheduler_text = event.get("text", "")
                    else:
                        yield event
            except Exception as exc:
                logger.exception("orchestrator: scheduler run raised unexpectedly")
                yield {"type": "error", "message": f"Scheduler error: {exc}"}
                return

        if scheduler_text:
            state.scheduler_text = scheduler_text
            await save_conversation(state)
            yield {
                "type": "partial_response",
                "agent": "scheduler",
                "label": "Loan Scheduler",
                "content": scheduler_text,
            }

    async def _run_calendar(
        self, state: ConversationState
    ) -> AsyncGenerator[dict, None]:
        """Run the calendar agent to create an event from the stored appointment."""
        if not state.appointment_json or not self._calendar:
            return

        yield {"type": "handoff", "message": "Scheduler → Calendar Agent"}

        calendar_query = (
            f"Call the mcp_CalendarTools_graph_createEvent tool to "
            f"add this appointment to the Veteran's calendar:\n\n"
            f"{state.appointment_json}"
        )
        calendar_text = ""
        tracer = get_tracer()
        with tracer.start_as_current_span("agent.calendar"):
            try:
                async for event in self._calendar.run(calendar_query):
                    if event["type"] == "_calendar_text":
                        calendar_text = event.get("text", "")
                    else:
                        yield event
            except Exception as exc:
                logger.exception("orchestrator: calendar run raised unexpectedly")
                yield {"type": "error", "message": f"Calendar error: {exc}"}

        if calendar_text:
            yield {
                "type": "partial_response",
                "agent": "calendar",
                "label": "Calendar",
                "content": calendar_text,
            }

    # ── Cleanup ────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Release async HTTP clients for all agents."""
        if self._advisor:
            await self._advisor.close()
        if self._calculator:
            await self._calculator.close()
        if self._scheduler:
            await self._scheduler.close()
        if self._calendar:
            await self._calendar.close()
        if self._newsletter:
            await self._newsletter.close()
        if self._client:
            await self._client.close()
            self._client = None
        if self._credential:
            await self._credential.close()
            self._credential = None
        logger.info("orchestrator: closed")
