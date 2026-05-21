"""
Run Foundry evaluations against the VA Loan Concierge agents.

Uses the OpenAI Evals API to run evaluations server-side in Foundry.
Queries are sent directly to registered agents, responses are evaluated
by builtin evaluators, and results appear in the Foundry portal.

Evaluates the Advisor Agent for:
  - Task adherence, groundedness, coherence, relevance

Evaluates the Orchestrator for:
  - Task adherence, coherence

Usage:
    az login
    python evals/run_eval.py                       # run advisor eval
    python evals/run_eval.py --agent orchestrator   # run orchestrator eval
    python evals/run_eval.py --all                  # run both
    python evals/run_eval.py --cleanup              # delete old evals + files

Results appear in the Foundry portal: Build > Evaluations
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FOUNDRY_PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
MODEL_DEPLOYMENT = os.environ.get("FOUNDRY_MODEL_DEPLOYMENT", "gpt-4.1")

ADVISOR_AGENT_NAME = "va-loan-advisor-iq"
ORCHESTRATOR_AGENT_NAME = "va-loan-orchestrator"

ADVISOR_EVAL_DATASET = os.path.join(os.path.dirname(__file__), "eval_advisor.jsonl")
ORCHESTRATOR_EVAL_DATASET = os.path.join(os.path.dirname(__file__), "eval_orchestrator.jsonl")

POLL_INTERVAL = 10  # seconds between status checks


def _refresh_advisor_agent_version() -> None:
    """Register the latest Advisor agent version from local code.

    Some stale versions can return only tool-approval requests during eval
    runs, which leaves sample.output_text empty and causes evaluator errors.
    Refreshing ensures evals target the latest Advisor definition.
    """
    try:
        from agents.advisor_agent import AdvisorAgent

        async def _refresh() -> None:
            agent = AdvisorAgent()
            await agent.initialize()
            logger.info("  Refreshed Advisor agent to version: %s", agent.agent_version)
            await agent.close()

        logger.info("  Refreshing Advisor agent definition...")
        asyncio.run(_refresh())
    except Exception as exc:
        logger.warning(
            "  Could not refresh Advisor agent definition; continuing with existing version (%s)",
            exc,
        )


def _get_project_client() -> AIProjectClient:
    """Create an authenticated sync Foundry project client."""
    if not FOUNDRY_PROJECT_ENDPOINT:
        logger.error("FOUNDRY_PROJECT_ENDPOINT not set. Run 'azd up' or check .env.")
        sys.exit(1)
    return AIProjectClient(
        endpoint=FOUNDRY_PROJECT_ENDPOINT,
        credential=DefaultAzureCredential(),
    )


def _load_jsonl(path: str) -> list[dict]:
    """Load a JSONL file into a list of dicts."""
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


ORCHESTRATOR_TASK_INSTRUCTIONS = (
    "You are a routing orchestrator for a VA mortgage lending concierge. "
    "Your task is to classify the user's intent and route to the correct "
    "specialized agent(s): advisor (eligibility/knowledge questions), "
    "calculator (refinance savings), scheduler (appointment booking), "
    "or respond directly for general/meta questions. "
    "A correct response identifies the right agent(s) and delegates. "
    "You do NOT need to answer the user's question directly — routing "
    "to the right agent IS the correct behavior."
)


def _build_testing_criteria(
    evaluator_names: list[str],
    task_instructions: str | None = None,
) -> list[dict]:
    """Build testing_criteria list for the OpenAI Evals API."""
    criteria = []
    for name in evaluator_names:
        data_mapping = {
            "query": "{{item.query}}",
            "response": "{{sample.output_text}}",
        }
        # Reference instructions from dataset items (if present) so the evaluator
        # understands what "adherence" means for routing agents
        if task_instructions and name == "task_adherence":
            data_mapping["instructions"] = "{{item.instructions}}"

        criterion = {
            "type": "azure_ai_evaluator",
            "name": name.replace("_", " ").title(),
            "evaluator_name": f"builtin.{name}",
            "data_mapping": data_mapping,
        }
        # Quality evaluators need a deployment for the LLM judge
        if name in ("task_adherence", "groundedness", "coherence", "relevance"):
            criterion["initialization_parameters"] = {
                "deployment_name": MODEL_DEPLOYMENT,
            }
        criteria.append(criterion)
    return criteria


def _wait_for_run(oai, eval_id: str, run_id: str) -> object:
    """Poll an eval run until it completes or fails."""
    while True:
        run = oai.evals.runs.retrieve(run_id=run_id, eval_id=eval_id)
        status = run.status
        if status in ("completed", "failed", "canceled"):
            return run
        logger.info("  Status: %s — checking again in %ds...", status, POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Cleanup — delete old OpenAI evals and files
# ---------------------------------------------------------------------------

def cleanup_evals() -> None:
    """Delete all OpenAI evals, runs, and uploaded files from the Foundry project."""
    logger.info("=== Cleaning up old evals and files ===")
    client = _get_project_client()
    oai = client.get_openai_client()

    eval_count = 0
    for eval_item in oai.evals.list():
        eval_name = getattr(eval_item, "name", eval_item.id)
        for run in oai.evals.runs.list(eval_id=eval_item.id):
            oai.evals.runs.delete(run_id=run.id, eval_id=eval_item.id)
        oai.evals.delete(eval_id=eval_item.id)
        eval_count += 1
        logger.info("  Deleted eval: %s", eval_name)

    file_count = 0
    for f in oai.files.list():
        oai.files.delete(file_id=f.id)
        file_count += 1
        logger.info("  Deleted file: %s (%s)", f.id, getattr(f, "filename", ""))

    logger.info("  Cleaned up %d evals and %d files", eval_count, file_count)


# ---------------------------------------------------------------------------
# Run evaluation against a registered agent
# ---------------------------------------------------------------------------

def _find_existing_eval(oai, eval_name: str) -> str | None:
    """Find an existing eval by name. Returns the eval ID or None."""
    for eval_item in oai.evals.list():
        if getattr(eval_item, "name", None) == eval_name:
            return eval_item.id
    return None


def _run_agent_eval(
    agent_name: str,
    dataset_path: str,
    eval_name: str,
    evaluator_names: list[str],
    task_instructions: str | None = None,
) -> str | None:
    """Run a server-side evaluation against a registered Foundry agent.

    If an eval with the same name already exists, adds a new run to it
    instead of creating a duplicate.  This lets you track score trends
    across runs in the Foundry portal.

    Returns the portal report URL on success, None on failure.
    """
    logger.info("=== %s ===", eval_name)
    logger.info("  Agent: %s", agent_name)
    logger.info("  Dataset: %s", os.path.basename(dataset_path))

    client = _get_project_client()
    oai = client.get_openai_client()

    # Load dataset items for inline content
    items = _load_jsonl(dataset_path)
    logger.info("  Loaded %d test queries", len(items))

    # Build testing criteria
    testing_criteria = _build_testing_criteria(evaluator_names, task_instructions)
    logger.info("  Evaluators: %s", ", ".join(evaluator_names))

    # Step 1: Reuse existing eval or create a new one
    eval_id = _find_existing_eval(oai, eval_name)
    if eval_id:
        logger.info("  Reusing existing eval: %s", eval_id)
    else:
        logger.info("  Creating new eval definition...")
        item_properties = {"query": {"type": "string"}}
        item_required = ["query"]
        if task_instructions:
            item_properties["instructions"] = {"type": "string"}

        evaluation = oai.evals.create(
            name=eval_name,
            data_source_config={
                "type": "custom",
                "item_schema": {
                    "type": "object",
                    "properties": item_properties,
                    "required": item_required,
                },
                "include_sample_schema": True,
            },
            testing_criteria=testing_criteria,
        )
        eval_id = evaluation.id
        logger.info("  Eval ID: %s", eval_id)

    # Step 2: Create a new run against the eval
    logger.info("  Creating eval run (server-side, targeting agent)...")
    eval_run = oai.evals.runs.create(
        eval_id=eval_id,
        name=f"{agent_name}-{time.strftime('%Y%m%d-%H%M%S')}",
        data_source={
            "type": "azure_ai_target_completions",
            "source": {
                "type": "file_content",
                "content": [{"item": item} for item in items],
            },
            "input_messages": {
                "type": "template",
                "template": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": {
                            "type": "input_text",
                            "text": "{{item.query}}",
                        },
                    }
                ],
            },
            "target": {
                "type": "azure_ai_agent",
                "name": agent_name,
            },
        },
    )
    logger.info("  Run ID: %s", eval_run.id)

    # Step 3: Poll until complete
    logger.info("  Waiting for server-side evaluation to complete...")
    result = _wait_for_run(oai, eval_id, eval_run.id)

    if result.status != "completed":
        logger.error("  Eval run failed: %s", getattr(result, "error", "unknown error"))
        return None

    # Step 4: Print results
    logger.info("")
    logger.info("  === %s Results ===", eval_name)
    result_counts = getattr(result, "result_counts", None)
    if result_counts:
        for key, value in vars(result_counts).items():
            if not key.startswith("_"):
                logger.info("  %s: %s", key, value)

    report_url = getattr(result, "report_url", None)
    if report_url:
        logger.info("")
        logger.info("  Portal: %s", report_url)

    return report_url


# ---------------------------------------------------------------------------
# Advisor evaluation
# ---------------------------------------------------------------------------

def run_advisor_eval() -> str | None:
    """Run evaluations against the Advisor Agent."""
    _refresh_advisor_agent_version()
    return _run_agent_eval(
        agent_name=ADVISOR_AGENT_NAME,
        dataset_path=ADVISOR_EVAL_DATASET,
        eval_name="VA Loan Advisor Quality",
        evaluator_names=["task_adherence", "groundedness", "coherence", "relevance"],
    )


# ---------------------------------------------------------------------------
# Orchestrator evaluation
# ---------------------------------------------------------------------------

def run_orchestrator_eval() -> str | None:
    """Run evaluations against the Orchestrator Agent."""
    return _run_agent_eval(
        agent_name=ORCHESTRATOR_AGENT_NAME,
        dataset_path=ORCHESTRATOR_EVAL_DATASET,
        eval_name="VA Loan Orchestrator Routing",
        evaluator_names=["task_adherence", "coherence"],
        task_instructions=ORCHESTRATOR_TASK_INSTRUCTIONS,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Run Foundry agent evaluations")
    parser.add_argument(
        "--agent",
        choices=["advisor", "orchestrator"],
        default="advisor",
        help="Which agent to evaluate (default: advisor)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run evaluations for all agents",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete all old OpenAI evals and files, then exit",
    )
    args = parser.parse_args()

    if args.cleanup:
        cleanup_evals()
        return

    if args.all:
        run_advisor_eval()
        print()
        run_orchestrator_eval()
    elif args.agent == "advisor":
        run_advisor_eval()
    elif args.agent == "orchestrator":
        run_orchestrator_eval()


if __name__ == "__main__":
    main()
