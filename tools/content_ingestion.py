"""
Content Understanding ingestion pipeline for VA mortgage news.

This module runs inside the Azure Function App (mcp-server/), triggered either
on a 4-hour timer or via HTTP POST /ingest for manual testing. It uses Azure
Content Understanding (CU) to process RSS feed articles into structured markdown
files, then writes them to a blob storage container that is a Foundry IQ Knowledge
Base source. Foundry IQ handles vectorization and indexing automatically.

Pipeline flow:
  RSS feeds ──► feedparser ──► CU (begin_analyze) ──► structured markdown
      ──► blob container "news-articles"
          ──► Foundry IQ KB (blob storage source, manual portal step)
              ──► Advisor Agent (existing — no code changes needed)

Authentication:
  ContentUnderstandingClient: DefaultAzureCredential → AI Services endpoint
  BlobServiceClient: DefaultAzureCredential → Storage account endpoint
  (Both use the same managed identity / az login credential.)

Required environment variables:
  CU_ENDPOINT                — AI Services endpoint for Content Understanding
                               e.g. https://{ais-name}.services.ai.azure.com/
  CU_COMPLETION_DEPLOYMENT   — GPT deployment for CU field generation (e.g. gpt-4.1)
  CU_MINI_MODEL_DEPLOYMENT   — GPT-mini deployment for CU defaults (e.g. gpt-4.1-mini)
  CU_LARGE_EMBEDDING_DEPLOYMENT — Large embedding for CU defaults (e.g. text-embedding-3-large)
  CU_ANALYZER_NAME           — Name of the CU analyzer resource (e.g. vaMortgageNews)
  CU_NEWS_BLOB_CONTAINER     — Blob container for news markdown files (e.g. news-articles)
  STORAGE_ACCOUNT_ENDPOINT   — Storage account blob endpoint
                               e.g. https://{storage-name}.blob.core.windows.net

Key design decisions:
  - CU is batch/async, not real-time. Articles take seconds to analyze.
  - Deduplication: each article is written as a stable filename derived from a
    SHA-256 hash of its URL. Blob existence check before CU analysis avoids
    reprocessing articles already ingested.
  - Foundry IQ handles all vectorization and search indexing automatically —
    no manual embedding generation or search index management needed.
  - CU defaults must be set once per resource (3 model keys: gpt-4.1,
    gpt-4.1-mini, text-embedding-3-large). The analyzer itself uses only
    "completion" and "embedding" internal keys.
  - Output format is markdown — human-readable and optimally chunked for
    Foundry IQ's document processing pipeline.
"""

import hashlib
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import json

import feedparser
from azure.ai.contentunderstanding import ContentUnderstandingClient
from azure.ai.contentunderstanding.models import (
    AnalysisInput,
    ContentAnalyzer,
    ContentFieldDefinition,
    ContentFieldSchema,
)
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

logger = logging.getLogger(__name__)


# ── Field Value Extraction ──────────────────────────────────────────────────────

def _extract_field_value(field_value: Any) -> Any:
    """
    Recursively extract a plain Python value from a CU ContentField object.

    CU SDK 1.0.1 returns typed field objects (StringField, NumberField, ObjectField,
    etc.) with type-specific value attributes instead of a common `.value` property.
    Each type is tried in order; None is returned if the field has no extracted value
    (e.g. an "extract" field where CU couldn't find the content), allowing callers
    to fall back to the original article metadata.
    """
    if field_value is None:
        return None

    # Scalar types — try the most common typed attributes first.
    for attr in (
        "value_string",
        "value_number",
        "value_integer",
        "value_boolean",
        "value_date",
        "value_time",
        "value_phone_number",
        "value_address",
        "value_currency",
        "value_selection_mark",
    ):
        val = getattr(field_value, attr, None)
        if val is not None:
            return val

    # Object field — recurse into each sub-field.
    value_object = getattr(field_value, "value_object", None)
    if value_object is not None:
        return {k: _extract_field_value(v) for k, v in value_object.items()}

    # Array field — recurse into each element.
    value_array = getattr(field_value, "value_array", None)
    if value_array is not None:
        return [_extract_field_value(v) for v in value_array]

    # Generic fallback for future SDK versions that add a unified .value.
    val = getattr(field_value, "value", None)
    if val is not None:
        return val

    # No value found — let the caller fall back to source metadata.
    return None


# ── CU Analyzer Field Schema ───────────────────────────────────────────────────

# Feed sources config lives alongside this file.
_FEED_SOURCES_PATH = Path(__file__).parent / "feed_sources.json"

# This schema defines what CU extracts from each RSS article.
# Three extraction methods are used:
#   extract  — pulls literal values present in the content (title, date)
#   generate — uses the LLM to produce structured output (summary, rate info)
#   classify — categorizes the article into a predefined enum (source type)
_ANALYZER_SCHEMA = ContentFieldSchema(
    name="va-mortgage-news-schema",
    description="Structured extraction schema for VA mortgage news articles",
    fields={
        # ── Extracted fields (literal values from article content) ──────────
        "Title": ContentFieldDefinition(
            method="extract",
            type="string",
            description="The article headline or title",
        ),
        "PublishDate": ContentFieldDefinition(
            method="extract",
            type="date",
            description="The publication date of the article",
        ),
        # ── Classified field (enum categorization) ──────────────────────────
        "SourceType": ContentFieldDefinition(
            method="classify",
            type="string",
            enum=[
                "rate_change",      # Weekly/monthly rate surveys (e.g. Freddie Mac PMMS)
                "policy_update",    # Regulatory or agency policy changes (CFPB, VA)
                "industry_news",    # General mortgage market news
                "va_circular",      # VA-issued circulars and policy letters
                "lender_bulletin",  # Lender-specific announcements
            ],
            description="Category of this news item",
        ),
        # ── Generated fields (LLM-produced structured output) ───────────────
        "Summary": ContentFieldDefinition(
            method="generate",
            type="string",
            description=(
                "A 2-3 sentence summary written for VA-eligible veterans and mortgage "
                "lenders. Highlight what changed, who is affected, and any action required."
            ),
        ),
        "RateInfo": ContentFieldDefinition(
            method="generate",
            type="object",
            description=(
                "If the article contains rate information, extract: "
                "current_rate (float, e.g. 6.72 for 6.72%), "
                "previous_rate (float), "
                "effective_date (ISO date string, e.g. '2026-03-28'), "
                "direction ('up' | 'down' | 'unchanged'). "
                "Return null for all fields if this is not a rate article."
            ),
            properties={
                "current_rate": ContentFieldDefinition(type="number"),
                "previous_rate": ContentFieldDefinition(type="number"),
                "effective_date": ContentFieldDefinition(type="string"),
                "direction": ContentFieldDefinition(
                    type="string",
                    enum=["up", "down", "unchanged"],
                ),
            },
        ),
        "PolicyUpdate": ContentFieldDefinition(
            method="generate",
            type="object",
            description=(
                "If the article describes a policy or regulatory change, extract: "
                "affected_area (e.g. 'VA funding fee', 'IRRRL net tangible benefit test'), "
                "change_description (1-2 sentences describing what changed), "
                "effective_date (ISO date string). "
                "Return null for all fields if this is not a policy article."
            ),
            properties={
                "affected_area": ContentFieldDefinition(type="string"),
                "change_description": ContentFieldDefinition(type="string"),
                "effective_date": ContentFieldDefinition(type="string"),
            },
        ),
        "RelevanceToVeterans": ContentFieldDefinition(
            method="generate",
            type="string",
            description=(
                "One sentence explaining why this news item matters specifically to "
                "VA-eligible veterans or active-duty service members seeking home loans."
            ),
        ),
    },
)


# ═══════════════════════════════════════════════════════════════════════════════
# NewsIngestionPipeline
# ═══════════════════════════════════════════════════════════════════════════════
#
# Full pipeline walkthrough (call order):
#
#  ┌─────────────────────────────────────────────────────────────────────────┐
#  │  STEP 0 — ensure_analyzer()   [one-time idempotent setup]               │
#  │    - Call cu.get_analyzer(name) — skip if already exists (HTTP 200)     │
#  │    - cu.update_defaults(model_deployments={...})                        │
#  │        Maps model names → Azure deployment names (3 keys required):     │
#  │        "gpt-4.1", "gpt-4.1-mini", "text-embedding-3-large"              │
#  │    - cu.begin_create_analyzer(analyzer_id, resource=ContentAnalyzer)    │
#  │        Creates the named analyzer with _ANALYZER_SCHEMA (7 fields)      │
#  │        base_analyzer_id="prebuilt-document" handles HTML + plain text   │
#  └─────────────────────────────────────────────────────────────────────────┘
#                          │
#                          ▼
#  ┌─────────────────────────────────────────────────────────────────────────┐
#  │  STEP 1 — run() → fetch_feeds()   [for every pipeline invocation]       │
#  │    - Reads feed_sources.json (11 RSS feeds: VA, CFPB, Freddie Mac, etc) │
#  │    - feedparser.parse(url) for each feed                                │
#  │    - Normalizes each entry into a flat article dict:                    │
#  │        {url, title, published, content_html, source_name, source_type}  │
#  │    - Returns all articles as a flat list (typically 25-100 per run)     │
#  └─────────────────────────────────────────────────────────────────────────┘
#                          │
#              ┌───────────┴───────────┐
#              │  for each article...  │
#              └───────────┬───────────┘
#                          │
#                          ▼
#  ┌─────────────────────────────────────────────────────────────────────────┐
#  │  STEP 2 — is_already_ingested(url)   [deduplication gate]               │
#  │    - blob name = SHA-256(url)[:32] + ".md"                              │
#  │    - blob_client.exists() checks the "news-articles" container          │
#  │    - If blob exists → stats["skipped"] += 1 and continue to next        │
#  └─────────────────────────────────────────────────────────────────────────┘
#                          │ (not yet ingested)
#                          ▼
#  ┌─────────────────────────────────────────────────────────────────────────┐
#  │  STEP 3 — analyze_article(article)   [CU structured extraction]         │
#  │    - cu.begin_analyze(analyzer_id, inputs=[AnalysisInput(html, ...)])   │
#  │    - poller.result() blocks until CU finishes (seconds per article)     │
#  │    - _extract_field_value() recursively unwraps typed CU field objects  │
#  │        into plain Python values (string, float, dict, list)             │
#  │    - Returns dict: {Title, PublishDate, SourceType, Summary,            │
#  │                     RateInfo, PolicyUpdate, RelevanceToVeterans}         │
#  │    - On failure: logs warning, returns None → stats["errors"] += 1      │
#  └─────────────────────────────────────────────────────────────────────────┘
#                          │
#                          ▼
#  ┌─────────────────────────────────────────────────────────────────────────┐
#  │  STEP 4 — write_to_blob(article, cu_fields)   [blob storage output]     │
#  │    - Merges CU fields with original RSS metadata (fallback for nulls)   │
#  │    - Renders structured markdown:                                        │
#  │        # {Title}                                                         │
#  │        Source / Type / Published / Ingested / URL                       │
#  │        ## Summary                                                        │
#  │        ## Relevance to Veterans                                          │
#  │        ## Rate Information      ← only if RateInfo fields populated      │
#  │        ## Policy Update         ← only if PolicyUpdate fields populated  │
#  │    - blob_client.upload_blob(markdown, overwrite=True)                  │
#  │    - Foundry IQ auto-vectorizes new blobs from the "news-articles"      │
#  │      container (configured as KB source in the portal) — no manual      │
#  │      embedding generation or index management needed                    │
#  └─────────────────────────────────────────────────────────────────────────┘
#

class NewsIngestionPipeline:
    """
    Orchestrates the full VA mortgage news ingestion pipeline:

      1. ensure_analyzer()  — create CU analyzer if it doesn't exist (idempotent)
      2. fetch_feeds()      — parse all configured RSS feeds into article dicts
      3. analyze_article()  — submit each article to CU, get structured fields
      4. write_to_blob()    — write structured markdown to blob storage

    Foundry IQ polls the blob container and automatically handles vectorization
    and indexing — no manual embedding generation or search index management needed.

    Designed to run from either:
      - The 4-hour timer trigger (automated, uses ManagedIdentityCredential)
      - The POST /ingest HTTP trigger (manual testing, same credential)
      - Local Python invocation (uses DefaultAzureCredential via az login)

    Usage:
        pipeline = NewsIngestionPipeline()
        pipeline.ensure_analyzer()   # one-time idempotent setup
        stats = pipeline.run()       # returns {fetched, analyzed, indexed, skipped, errors}
    """

    def __init__(self, credential: TokenCredential | None = None) -> None:
        # Accept an explicit credential for production (ManagedIdentityCredential)
        # or fall back to DefaultAzureCredential for local dev / testing.
        self._credential = credential or DefaultAzureCredential()
        self._cu_client: ContentUnderstandingClient | None = None
        self._blob_client: BlobServiceClient | None = None

    # ── Client Accessors (lazy init, cached per pipeline instance) ─────────────

    def _get_cu_client(self) -> ContentUnderstandingClient:
        """Return a cached ContentUnderstandingClient pointed at CU_ENDPOINT."""
        if self._cu_client is None:
            # CU_ENDPOINT must be the services.ai.azure.com format, not cognitiveservices.azure.com
            endpoint = os.environ["CU_ENDPOINT"].rstrip("/")
            self._cu_client = ContentUnderstandingClient(
                endpoint=endpoint,
                credential=self._credential,
            )
        return self._cu_client

    def _get_blob_client(self) -> BlobServiceClient:
        """Return a cached BlobServiceClient for the storage account."""
        if self._blob_client is None:
            self._blob_client = BlobServiceClient(
                account_url=os.environ["STORAGE_ACCOUNT_ENDPOINT"],
                credential=self._credential,
            )
        return self._blob_client

    # ── Analyzer Management ────────────────────────────────────────────────────

    def ensure_analyzer(self) -> None:
        """
        Create the CU analyzer if it does not already exist.
        Idempotent — safe to call on every Function App startup.

        CU setup requires two steps before the first analyzer can be created:
          1. update_defaults() — maps the three required model names to deployment names.
             Keys must be exact model names ("gpt-4.1", "gpt-4.1-mini",
             "text-embedding-3-large"), not generic keys like "completion".
             This is a one-time resource-level setup that persists across sessions.
          2. begin_create_analyzer() — creates the named analyzer with the field
             schema. The analyzer's models dict uses CU's internal "completion" /
             "embedding" keys, which reference the deployment names set in step 1.
        """
        cu = self._get_cu_client()
        analyzer_name = os.environ["CU_ANALYZER_NAME"]

        # Step 1: Set resource-level model defaults on every run.
        # Existing analyzers still depend on these defaults; refreshing them is
        # idempotent and prevents runtime failures after app setting changes.
        completion_deployment = os.environ["CU_COMPLETION_DEPLOYMENT"]
        mini_deployment = os.environ["CU_MINI_MODEL_DEPLOYMENT"]
        large_embedding = os.environ["CU_LARGE_EMBEDDING_DEPLOYMENT"]

        cu.update_defaults(model_deployments={
            "gpt-4.1":                completion_deployment,
            "gpt-4.1-mini":           mini_deployment,
            "text-embedding-3-large": large_embedding,
        })
        logger.info(
            "content_ingestion: CU defaults set — "
            "gpt-4.1=%s, gpt-4.1-mini=%s, text-embedding-3-large=%s",
            completion_deployment, mini_deployment, large_embedding,
        )

        # Skip creation if the analyzer already exists.
        try:
            existing = cu.get_analyzer(analyzer_name)
            logger.info(
                "content_ingestion: analyzer '%s' already exists (status=%s)",
                analyzer_name, existing.status,
            )
            return
        except Exception:
            pass  # 404 Not Found — proceed to create.

        logger.info("content_ingestion: creating CU analyzer '%s'", analyzer_name)

        # Step 2: Create the analyzer.
        # base_analyzer_id="prebuilt-document" handles HTML and plain text input.
        # models uses CU's internal "completion"/"embedding" keys with model names.
        # CU resolves these through the defaults mapping configured above.
        analyzer = ContentAnalyzer(
            analyzer_id=analyzer_name,
            description="VA mortgage news structured extraction analyzer",
            base_analyzer_id="prebuilt-document",
            field_schema=_ANALYZER_SCHEMA,
            models={
                "completion": "gpt-4.1",
                "embedding":  "text-embedding-3-large",
            },
        )

        poller = cu.begin_create_analyzer(
            analyzer_id=analyzer_name,
            resource=analyzer,
        )
        result = poller.result()
        logger.info(
            "content_ingestion: analyzer '%s' created (status=%s)",
            analyzer_name, result.status,
        )

    # ── Feed Fetching ──────────────────────────────────────────────────────────

    def fetch_feeds(self) -> list[dict[str, Any]]:
        """
        Parse all RSS feeds from feed_sources.json and return a flat list of articles.

        Each article dict contains:
            url           — canonical article URL (used as deduplication key)
            title         — article title from the feed
            published     — publication date string from the feed entry
            content_html  — full HTML body if available, else the summary snippet
            source_name   — human-readable feed name (e.g. "CFPB Newsroom")
            source_type   — feed-level category (e.g. "policy_update")
        """
        sources = json.loads(_FEED_SOURCES_PATH.read_text())
        articles: list[dict[str, Any]] = []

        for source in sources:
            try:
                feed = feedparser.parse(source["url"])
                logger.info(
                    "content_ingestion: fetched '%s' — %d entries",
                    source["name"], len(feed.entries),
                )
                for entry in feed.entries:
                    url = entry.get("link", "")
                    if not url:
                        continue  # Skip entries with no canonical URL.

                    # Prefer full HTML content if available (some feeds include it);
                    # fall back to the summary snippet.
                    content_html = ""
                    if entry.get("content"):
                        content_html = entry["content"][0].get("value", "")
                    if not content_html:
                        content_html = entry.get("summary", "")

                    articles.append({
                        "url":          url,
                        "title":        entry.get("title", ""),
                        "published":    entry.get("published", ""),
                        "content_html": content_html,
                        "source_name":  source["name"],
                        "source_type":  source["source_type"],
                    })
            except Exception as exc:
                logger.warning(
                    "content_ingestion: failed to fetch feed '%s': %s",
                    source["name"], exc,
                )

        logger.info("content_ingestion: %d total articles fetched across all feeds", len(articles))
        return articles

    # ── CU Analysis ───────────────────────────────────────────────────────────

    def analyze_article(self, article: dict[str, Any]) -> dict[str, Any] | None:
        """
        Submit one article to Content Understanding and return extracted fields.

        The article HTML (or title fallback) is sent to the CU analyzer as
        text/html. CU runs gpt-4.1 to extract, classify, and generate the
        7 fields defined in _ANALYZER_SCHEMA.

        Returns a dict of field name → Python value, or None if analysis fails.
        Per-article failures are logged and skipped — they do not abort the batch.
        """
        cu = self._get_cu_client()
        analyzer_name = os.environ["CU_ANALYZER_NAME"]

        content = article["content_html"] or article["title"]
        if not content:
            logger.warning(
                "content_ingestion: skipping article with no content: %s", article["url"]
            )
            return None

        try:
            poller = cu.begin_analyze(
                analyzer_id=analyzer_name,
                inputs=[
                    AnalysisInput(
                        data=content.encode("utf-8"),
                        mime_type="text/html",
                        name="article",
                    )
                ],
                string_encoding="utf8",
            )
            result = poller.result()
        except Exception as exc:
            logger.warning(
                "content_ingestion: CU analysis failed for '%s': %s",
                article["url"], exc,
            )
            return None

        # Extract the 7 schema fields from the first (and only) content item.
        fields: dict[str, Any] = {}
        if result.contents:
            raw_fields = result.contents[0].fields or {}
            for field_name, field_value in raw_fields.items():
                fields[field_name] = _extract_field_value(field_value)

        return fields

    # ── Blob Storage Output ────────────────────────────────────────────────────

    def _blob_name(self, url: str) -> str:
        """
        Stable blob filename derived from the article URL.
        SHA-256 truncated to 32 hex chars ensures uniqueness and safe filenames.
        e.g. "a3f8c1d2e4b5f6a7b8c9d0e1f2a3b4c5.md"
        """
        return hashlib.sha256(url.encode()).hexdigest()[:32] + ".md"

    def is_already_ingested(self, url: str) -> bool:
        """
        Return True if this article has already been written to blob storage.
        Checks for blob existence by name — avoids re-processing and re-analyzing
        articles that are already in the Foundry IQ knowledge source.
        """
        container_name = os.environ["CU_NEWS_BLOB_CONTAINER"]
        blob_client = self._get_blob_client().get_blob_client(
            container=container_name,
            blob=self._blob_name(url),
        )
        return blob_client.exists()

    def write_to_blob(
        self,
        article: dict[str, Any],
        cu_fields: dict[str, Any],
    ) -> None:
        """
        Write one structured article to blob storage as a markdown file.

        Foundry IQ automatically picks up new blobs, chunks them, generates
        embeddings, and makes them queryable via the knowledge_base_retrieve tool.
        No manual vectorization or search index management needed.

        The markdown format is human-readable (useful for demo presentations)
        and optimally structured for Foundry IQ's document chunking pipeline.

        CU "extract" fields (Title, PublishDate) may return None if the content
        didn't contain those values — in that case we fall back to the original
        RSS feed metadata (article["title"], article["published"]).
        """
        title = cu_fields.get("Title") or article["title"]
        publish_date = cu_fields.get("PublishDate") or article["published"]
        source_type = cu_fields.get("SourceType") or article["source_type"]
        summary = cu_fields.get("Summary") or ""
        relevance = cu_fields.get("RelevanceToVeterans") or ""
        rate_info = cu_fields.get("RateInfo")
        policy_update = cu_fields.get("PolicyUpdate")

        # Build a structured markdown document.
        # Frontmatter metadata helps Foundry IQ surface source/date in citations.
        lines = [
            f"# {title}",
            "",
            f"**Source:** {article['source_name']}  ",
            f"**Type:** {source_type}  ",
            f"**Published:** {publish_date}  ",
            f"**Ingested:** {datetime.now(UTC).strftime('%Y-%m-%d')}  ",
            f"**URL:** {article['url']}",
            "",
            "## Summary",
            "",
            summary,
            "",
            "## Relevance to Veterans",
            "",
            relevance,
        ]

        # Append rate info section only if CU extracted rate data.
        if rate_info and any(rate_info.get(k) for k in ("current_rate", "previous_rate", "direction")):
            lines += [
                "",
                "## Rate Information",
                "",
                f"- **Current rate:** {rate_info.get('current_rate', 'N/A')}%",
                f"- **Previous rate:** {rate_info.get('previous_rate', 'N/A')}%",
                f"- **Direction:** {rate_info.get('direction', 'N/A')}",
                f"- **Effective date:** {rate_info.get('effective_date', 'N/A')}",
            ]

        # Append policy update section only if CU extracted policy data.
        if policy_update and any(policy_update.get(k) for k in ("affected_area", "change_description")):
            lines += [
                "",
                "## Policy Update",
                "",
                f"- **Affected area:** {policy_update.get('affected_area', 'N/A')}",
                f"- **Change:** {policy_update.get('change_description', 'N/A')}",
                f"- **Effective date:** {policy_update.get('effective_date', 'N/A')}",
            ]

        markdown = "\n".join(lines)

        container_name = os.environ["CU_NEWS_BLOB_CONTAINER"]
        blob_name = self._blob_name(article["url"])
        blob_client = self._get_blob_client().get_blob_client(
            container=container_name,
            blob=blob_name,
        )
        blob_client.upload_blob(
            markdown.encode("utf-8"),
            overwrite=True,
            content_settings=None,
        )
        logger.info(
            "content_ingestion: wrote blob '%s' for '%s'",
            blob_name, title[:60],
        )

    # ── Full Pipeline ──────────────────────────────────────────────────────────

    def run(self) -> dict[str, int]:
        """
        Execute the full ingestion pipeline end-to-end.

        For each article across all configured feeds:
          1. Skip if already ingested (blob existence check)
          2. Analyze with Content Understanding
          3. Write structured markdown to blob storage

        Returns a summary dict: {fetched, analyzed, indexed, skipped, errors}
        """
        stats = {"fetched": 0, "analyzed": 0, "indexed": 0, "skipped": 0, "errors": 0}

        articles = self.fetch_feeds()
        stats["fetched"] = len(articles)

        for article in articles:
            url = article["url"]

            # Deduplication check — skip articles already written to blob.
            if self.is_already_ingested(url):
                logger.debug("content_ingestion: already ingested, skipping: %s", url)
                stats["skipped"] += 1
                continue

            # Analyze with CU — skip this article on failure, continue the batch.
            cu_fields = self.analyze_article(article)
            if cu_fields is None:
                stats["errors"] += 1
                continue
            stats["analyzed"] += 1

            # Write structured markdown to blob storage.
            try:
                self.write_to_blob(article, cu_fields)
                stats["indexed"] += 1
            except Exception as exc:
                logger.warning(
                    "content_ingestion: blob write failed for '%s': %s", url, exc
                )
                stats["errors"] += 1

        logger.info("content_ingestion: pipeline complete — %s", stats)
        return stats
