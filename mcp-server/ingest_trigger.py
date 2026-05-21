"""
Azure Function triggers for the VA mortgage news ingestion pipeline.

Two triggers share the same NewsIngestionPipeline logic:

  ingest_timer  — runs automatically every 4 hours (CRON: 0 0 */4 * * *)
  ingest_now    — HTTP POST /ingest for manual/test invocations (no auth)

Both triggers call NewsIngestionPipeline.run() and return a JSON stats summary:
  {fetched, analyzed, indexed, skipped, errors}

Source of truth:
  The pipeline code lives in tools/content_ingestion.py at the repo root.
  At deploy time, postprovision.ps1 copies that file into mcp-server/tools/
  so the Function App can import it without a parent-directory path hack.
  See the "Content Understanding" section in README.md for details.

  *** If you modify tools/content_ingestion.py, run postprovision.ps1 (or
      `azd up`) to sync the copy before publishing the Function App. ***

Environment variables (set by Bicep + postprovision.ps1):
  CU_ENDPOINT                   — AI Services endpoint (services.ai.azure.com format)
  CU_COMPLETION_DEPLOYMENT      — GPT deployment for CU field generation (e.g. gpt-4.1)
  CU_MINI_MODEL_DEPLOYMENT      — GPT-mini deployment (required by CU defaults)
  CU_LARGE_EMBEDDING_DEPLOYMENT — Large embedding deployment (required by CU defaults)
  CU_ANALYZER_NAME              — CU analyzer resource name (e.g. vaMortgageNews)
  CU_NEWS_BLOB_CONTAINER        — Blob container for news markdown output (e.g. news-articles)
  STORAGE_ACCOUNT_ENDPOINT      — Storage account blob endpoint URL
"""

import json
import logging

import azure.functions as func

logger = logging.getLogger(__name__)

# Register triggers on a Blueprint and let function_app.py attach it.
bp = func.Blueprint()


def _build_pipeline():
    """Create the ingestion pipeline lazily to keep worker startup lightweight."""
    from azure.identity import ManagedIdentityCredential
    # tools/content_ingestion.py is copied into mcp-server/tools/ before deployment.
    # See README.md "Content Understanding" -> "Code Deployment" for the sync process.
    from tools.content_ingestion import NewsIngestionPipeline

    return NewsIngestionPipeline(credential=ManagedIdentityCredential())


# ── Timer Trigger — runs every 4 hours on the hour ────────────────────────────

@bp.timer_trigger(
    arg_name="timer",
    schedule="0 0 */4 * * *",  # At 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC
    run_on_startup=False,       # Don't run immediately on cold start — wait for schedule
)
def ingest_timer(timer: func.TimerRequest) -> None:
    """
    Scheduled ingestion — fetches all configured RSS feeds and indexes new articles.
    Skips articles already in the search index (deduplication by URL hash).
    """
    if timer.past_due:
        logger.warning("ingest_timer: timer is past due — running now")

    logger.info("ingest_timer: starting scheduled ingestion")
    pipeline = _build_pipeline()
    pipeline.ensure_analyzer()
    stats = pipeline.run()
    logger.info("ingest_timer: complete — %s", stats)


# ── HTTP Trigger — manual invocation for testing ──────────────────────────────

@bp.route(route="ingest", methods=["POST"])
def ingest_now(req: func.HttpRequest) -> func.HttpResponse:
    """
    Manual ingestion trigger for testing and on-demand refresh.

    POST /ingest  (no request body needed)

    Returns JSON: {"fetched": N, "analyzed": N, "indexed": N, "skipped": N, "errors": N}

    Example:
        curl -X POST https://<func-app>.azurewebsites.net/ingest
    """
    logger.info("ingest_now: manual ingestion triggered")
    try:
        pipeline = _build_pipeline()
        pipeline.ensure_analyzer()
        stats = pipeline.run()
        return func.HttpResponse(
            json.dumps(stats),
            status_code=200,
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:
        logger.exception("ingest_now: pipeline error")
        return func.HttpResponse(
            json.dumps({"error": str(exc)}),
            status_code=500,
            headers={"Content-Type": "application/json"},
        )
