"""
Run cloud AI red teaming against a single Foundry agent.

This script follows the Foundry cloud red-teaming workflow:
1) Create or reuse a red-team eval group
2) Create a prohibited-actions taxonomy for the target agent
3) Start a red-team run using taxonomy-driven attack generation
4) Poll for completion and export output items to JSON

Default target: VA Loan Advisor agent

Usage:
    az login
    python evals/run_redteam.py
    python evals/run_redteam.py --attack-strategies Flip Base64 IndirectJailbreak --num-turns 5
    python evals/run_redteam.py --agent-name va-loan-advisor-iq --model-deployment gpt-4.1
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

try:
    from azure.ai.projects.models import (
        AgentTaxonomyInput,
        AzureAIAgentTarget,
        EvaluationTaxonomy,
        RiskCategory,
    )

    HAS_TAXONOMY_MODELS = True
except Exception:
    HAS_TAXONOMY_MODELS = False


load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


FOUNDRY_PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or os.environ.get(
    "AZURE_AI_PROJECT_ENDPOINT", ""
)
MODEL_DEPLOYMENT = os.environ.get("FOUNDRY_MODEL_DEPLOYMENT") or os.environ.get(
    "AZURE_AI_MODEL_DEPLOYMENT_NAME", ""
)

DEFAULT_AGENT_NAME = "va-loan-advisor-iq"
DEFAULT_EVAL_NAME = "VA Loan Advisor Red Team Safety"
DEFAULT_ATTACK_STRATEGIES = ["Flip", "Base64", "IndirectJailbreak"]
DEFAULT_NUM_TURNS = 5
DEFAULT_TIMEOUT_SECONDS = 1800
POLL_INTERVAL_SECONDS = 10
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent


def _ensure_repo_root_on_path() -> None:
    """Make local imports work when executed as a file path script."""
    repo_root_str = str(REPO_ROOT)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)


def _refresh_advisor_agent_version(agent_name: str) -> None:
    """Register latest Advisor agent version from local code to avoid stale targets."""
    if agent_name != DEFAULT_AGENT_NAME:
        return

    try:
        _ensure_repo_root_on_path()
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


def _extract_region_unavailable_message(exc: Exception) -> str | None:
    """Return a user-friendly message when red-team isn't available in this region."""
    text = str(exc)
    lowered = text.lower()
    if "redteamevaluationnotavailable" not in lowered and "not supported" not in lowered:
        return None

    if "redteam" not in lowered:
        return None

    region = "this region"
    marker = " not supported in "
    if marker in lowered:
        start = lowered.find(marker) + len(marker)
        end = lowered.find(" region", start)
        if end > start:
            region = text[start:end].strip()

    return (
        f"Cloud AI red teaming is not available in {region}. "
        "Use a Foundry project in a supported region, then rerun this script."
    )


def _get_project_client() -> AIProjectClient:
    """Create an authenticated Foundry project client."""
    if not FOUNDRY_PROJECT_ENDPOINT:
        logger.error(
            "FOUNDRY_PROJECT_ENDPOINT or AZURE_AI_PROJECT_ENDPOINT not set. Run 'azd up' or check .env."
        )
        sys.exit(1)

    return AIProjectClient(
        endpoint=FOUNDRY_PROJECT_ENDPOINT,
        credential=DefaultAzureCredential(),
    )


def _find_existing_eval(oai: Any, eval_name: str) -> str | None:
    """Return eval id if a matching eval name exists."""
    for eval_item in oai.evals.list():
        if getattr(eval_item, "name", None) == eval_name:
            return eval_item.id
    return None


def _build_redteam_criteria(model_deployment: str) -> list[dict[str, Any]]:
    """Build built-in evaluators for red-teaming safety runs."""
    criteria: list[dict[str, Any]] = [
        {
            "type": "azure_ai_evaluator",
            "name": "Prohibited Actions",
            "evaluator_name": "builtin.prohibited_actions",
            "evaluator_version": "1",
        },
        {
            "type": "azure_ai_evaluator",
            "name": "Sensitive Data Leakage",
            "evaluator_name": "builtin.sensitive_data_leakage",
            "evaluator_version": "1",
        },
    ]

    if model_deployment:
        criteria.append(
            {
                "type": "azure_ai_evaluator",
                "name": "Task Adherence",
                "evaluator_name": "builtin.task_adherence",
                "evaluator_version": "1",
                "initialization_parameters": {"deployment_name": model_deployment},
            }
        )

    return criteria


def _create_or_reuse_redteam_eval(oai: Any, eval_name: str, model_deployment: str) -> str:
    """Create a new red-team eval group or reuse an existing one by name."""
    existing_eval_id = _find_existing_eval(oai, eval_name)
    if existing_eval_id:
        logger.info("  Reusing existing red-team eval: %s", existing_eval_id)
        return existing_eval_id

    logger.info("  Creating red-team eval definition...")
    evaluation = oai.evals.create(
        name=eval_name,
        data_source_config={"type": "azure_ai_source", "scenario": "red_team"},
        testing_criteria=_build_redteam_criteria(model_deployment),
    )
    logger.info("  Created red-team eval id: %s", evaluation.id)
    return evaluation.id


def _create_taxonomy(
    project_client: AIProjectClient,
    agent_name: str,
    agent_version: str | None,
) -> str:
    """Create a prohibited-actions taxonomy used to generate attack prompts."""
    if not hasattr(project_client, "beta"):
        raise RuntimeError(
            "Installed azure-ai-projects does not expose beta APIs required for red-team taxonomy."
        )

    logger.info("  Creating prohibited-actions taxonomy...")

    if HAS_TAXONOMY_MODELS:
        target_kwargs: dict[str, Any] = {"name": agent_name}
        if agent_version:
            target_kwargs["version"] = agent_version

        target = AzureAIAgentTarget(**target_kwargs)
        body = EvaluationTaxonomy(
            description="Taxonomy for cloud red-teaming run",
            taxonomy_input=AgentTaxonomyInput(
                risk_categories=[RiskCategory.PROHIBITED_ACTIONS],
                target=target,
            ),
        )
    else:
        target: dict[str, Any] = {"name": agent_name}
        if agent_version:
            target["version"] = agent_version

        body = {
            "description": "Taxonomy for cloud red-teaming run",
            "taxonomy_input": {
                "risk_categories": ["ProhibitedActions"],
                "target": target,
            },
        }

    taxonomy = project_client.beta.evaluation_taxonomies.create(
        name=f"{agent_name}-prohibited-actions",
        body=body,
    )
    logger.info("  Taxonomy id: %s", taxonomy.id)
    return taxonomy.id


def _create_redteam_run(
    oai: Any,
    eval_id: str,
    agent_name: str,
    taxonomy_file_id: str,
    attack_strategies: list[str],
    num_turns: int,
    agent_version: str | None,
) -> Any:
    """Create a red-team run against a single Foundry agent target."""
    target: dict[str, Any] = {
        "type": "azure_ai_agent",
        "name": agent_name,
    }
    if agent_version:
        target["version"] = agent_version

    logger.info("  Creating red-team run...")
    return oai.evals.runs.create(
        eval_id=eval_id,
        name=f"{agent_name}-redteam-{time.strftime('%Y%m%d-%H%M%S')}",
        data_source={
            "type": "azure_ai_red_team",
            "item_generation_params": {
                "type": "red_team_taxonomy",
                "attack_strategies": attack_strategies,
                "num_turns": num_turns,
                "source": {
                    "type": "file_id",
                    "id": taxonomy_file_id,
                },
            },
            "target": target,
        },
    )


def _wait_for_run(
    oai: Any,
    eval_id: str,
    run_id: str,
    poll_interval_seconds: int,
    timeout_seconds: int,
) -> Any:
    """Poll a run until completion or timeout."""
    deadline = time.time() + timeout_seconds
    while True:
        run = oai.evals.runs.retrieve(run_id=run_id, eval_id=eval_id)
        status = run.status
        if status in ("completed", "failed", "canceled"):
            return run

        if time.time() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for red-team run {run_id} after {timeout_seconds} seconds"
            )

        logger.info("  Status: %s - checking again in %ds...", status, poll_interval_seconds)
        time.sleep(poll_interval_seconds)


def _to_json_primitive(value: Any) -> Any:
    """Convert SDK objects into JSON-serializable primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, list):
        return [_to_json_primitive(v) for v in value]

    if isinstance(value, dict):
        return {str(k): _to_json_primitive(v) for k, v in value.items()}

    for attr in ("model_dump", "as_dict"):
        method = getattr(value, attr, None)
        if callable(method):
            return _to_json_primitive(method())

    if hasattr(value, "__dict__"):
        return {
            k: _to_json_primitive(v)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }

    return str(value)


def _write_output_items(
    oai: Any,
    eval_id: str,
    run_id: str,
    agent_name: str,
    output_dir: Path,
) -> Path:
    """Persist red-team output items to a JSON file for offline analysis."""
    output_dir.mkdir(parents=True, exist_ok=True)
    items = list(oai.evals.runs.output_items.list(run_id=run_id, eval_id=eval_id))

    filename = f"redteam_output_{agent_name}_{time.strftime('%Y%m%d-%H%M%S')}_{run_id}.json"
    output_path = output_dir / filename

    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(_to_json_primitive(items), handle, indent=2)

    return output_path


def run_red_team(
    agent_name: str,
    eval_name: str,
    model_deployment: str,
    num_turns: int,
    attack_strategies: list[str],
    timeout_seconds: int,
    poll_interval_seconds: int,
    output_dir: Path,
    agent_version: str | None,
    refresh_agent: bool,
) -> int:
    """Run one cloud red-team execution and print summary output."""
    if num_turns < 1:
        logger.error("num_turns must be >= 1")
        return 2

    if refresh_agent:
        _refresh_advisor_agent_version(agent_name)

    logger.info("=== Cloud Red Team Run ===")
    logger.info("  Agent: %s", agent_name)
    logger.info("  Agent version: %s", agent_version or "latest (service default)")
    logger.info("  Eval group: %s", eval_name)
    logger.info("  Attack strategies: %s", ", ".join(attack_strategies))
    logger.info("  Num turns: %s", num_turns)

    client = _get_project_client()
    oai = client.get_openai_client()

    eval_id = _create_or_reuse_redteam_eval(oai, eval_name, model_deployment)
    taxonomy_id = _create_taxonomy(client, agent_name, agent_version)

    run = _create_redteam_run(
        oai=oai,
        eval_id=eval_id,
        agent_name=agent_name,
        taxonomy_file_id=taxonomy_id,
        attack_strategies=attack_strategies,
        num_turns=num_turns,
        agent_version=agent_version,
    )

    logger.info("  Run id: %s", run.id)
    logger.info("  Waiting for run completion...")

    result = _wait_for_run(
        oai=oai,
        eval_id=eval_id,
        run_id=run.id,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
    )

    output_path = _write_output_items(
        oai=oai,
        eval_id=eval_id,
        run_id=run.id,
        agent_name=agent_name,
        output_dir=output_dir,
    )

    logger.info("")
    logger.info("=== Red Team Results ===")
    logger.info("  Status: %s", result.status)

    result_counts = getattr(result, "result_counts", None)
    if result_counts:
        for key, value in vars(result_counts).items():
            if not key.startswith("_"):
                logger.info("  %s: %s", key, value)

    report_url = getattr(result, "report_url", None)
    if report_url:
        logger.info("  Portal: %s", report_url)

    logger.info("  Output items: %s", output_path)

    if result.status != "completed":
        logger.error("  Red-team run did not complete successfully.")
        return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run cloud AI red teaming against a single Foundry agent"
    )
    parser.add_argument(
        "--agent-name",
        default=DEFAULT_AGENT_NAME,
        help=f"Target Foundry agent name (default: {DEFAULT_AGENT_NAME})",
    )
    parser.add_argument(
        "--agent-version",
        default=None,
        help="Optional explicit target agent version",
    )
    parser.add_argument(
        "--eval-name",
        default=DEFAULT_EVAL_NAME,
        help=f"Red-team eval group name (default: {DEFAULT_EVAL_NAME})",
    )
    parser.add_argument(
        "--model-deployment",
        default=MODEL_DEPLOYMENT,
        help="Model deployment for task-adherence evaluator",
    )
    parser.add_argument(
        "--attack-strategies",
        nargs="+",
        default=DEFAULT_ATTACK_STRATEGIES,
        help="Attack strategies to generate prompts from taxonomy",
    )
    parser.add_argument(
        "--num-turns",
        type=int,
        default=DEFAULT_NUM_TURNS,
        help=f"Number of turns for generated attacks (default: {DEFAULT_NUM_TURNS})",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Maximum wait for run completion in seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=POLL_INTERVAL_SECONDS,
        help=f"Polling interval in seconds (default: {POLL_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).parent / "redteam_outputs"),
        help="Directory to store output items JSON",
    )
    parser.add_argument(
        "--skip-refresh-agent",
        action="store_true",
        help="Skip refreshing local Advisor agent definition before run",
    )

    args = parser.parse_args()

    try:
        return run_red_team(
            agent_name=args.agent_name,
            eval_name=args.eval_name,
            model_deployment=args.model_deployment,
            num_turns=args.num_turns,
            attack_strategies=args.attack_strategies,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
            output_dir=Path(args.output_dir),
            agent_version=args.agent_version,
            refresh_agent=not args.skip_refresh_agent,
        )
    except KeyboardInterrupt:
        logger.error("Interrupted by user")
        return 130
    except Exception as exc:
        region_message = _extract_region_unavailable_message(exc)
        if region_message:
            logger.error(region_message)
            logger.error(
                "Tip: keep this project for normal evals and run red teaming from a second Foundry project in a supported region."
            )
            return 4
        logger.exception("Red-team run failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
