"""
Rate Intelligence Hosted Agent Server — LangGraph + Foundry Responses Protocol.

This module is the CONTAINER ENTRYPOINT for the hosted agent running in
Microsoft Foundry. It exposes the Foundry Responses protocol endpoint at
POST /responses (port 8088) so the platform can route queries to it.

Architecture inside the container:
  Foundry platform ──► POST /responses ──► LangGraph graph
                                                │
                                           ┌────┴────┐
                                           │ chatbot  │ (AzureAI LLM)
                                           └────┬─────┘
                                     tool_calls?│
                                           ┌────▼────┐
                                           │  tools   │
                                           │ (search) │
                                           └──────────┘

The LangGraph graph:
  1. chatbot node — calls the Azure AI model with tools bound
  2. tools node   — executes search_rate_knowledge_base against Azure AI Search
  3. Conditional routing: if the model made tool calls → tools, else END

Required environment variables (injected by Foundry at runtime via agent.yaml):
  FOUNDRY_PROJECT_ENDPOINT       — Foundry project data-plane endpoint
  AZURE_AI_MODEL_DEPLOYMENT_NAME — e.g. gpt-4.1
  RATE_SEARCH_ENDPOINT           — Azure AI Search endpoint
  RATE_KNOWLEDGE_BASE_NAME       — AI Search index name for rate data
"""

import asyncio
import logging
import os
from typing import Annotated

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from langchain_azure_ai.chat_models import AzureAIOpenAIApiChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import TypedDict

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponsesAgentServerHost,
    ResponsesServerOptions,
    TextResponse,
)
from azure.ai.agentserver.responses.models import (
    MessageContentInputTextContent,
    MessageContentOutputTextContent,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# 1. ENVIRONMENT
# ═══════════════════════════════════════════════════════════════════════════════

FOUNDRY_PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
AZURE_AI_MODEL_DEPLOYMENT_NAME = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4.1")
RATE_SEARCH_ENDPOINT = os.environ.get("RATE_SEARCH_ENDPOINT", "")
RATE_KNOWLEDGE_BASE_NAME = os.environ.get("RATE_KNOWLEDGE_BASE_NAME", "")
RATE_SEARCH_API_KEY = os.environ.get("RATE_SEARCH_API_KEY", "")

if not FOUNDRY_PROJECT_ENDPOINT:
    raise EnvironmentError("FOUNDRY_PROJECT_ENDPOINT is not set.")

SYSTEM_PROMPT = (
    "You are a VA loan rate specialist. Use the search_rate_knowledge_base tool "
    "to retrieve:\n"
    "1. The most recent IRRRL (VA streamline refinance) rates from news articles.\n"
    "2. The current rate trend (rising / falling / stable).\n\n"
    "Return your findings as a JSON object with these fields:\n"
    "  { current_rate, rate_date, source, trend, confidence }\n\n"
    "Rules:\n"
    "- Always call the search tool before answering.\n"
    "- Never fabricate rates — only report what the knowledge base returns.\n"
    "- Do NOT make buy/sell recommendations — present data only.\n"
    "- If no rate data is found, say so explicitly."
)

# ═══════════════════════════════════════════════════════════════════════════════
# 2. TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@tool
def search_rate_knowledge_base(query: str) -> str:
    """
    Search the VA loan knowledge base for IRRRL rates and mortgage market data.
    Returns the top-5 matching document chunks with their source filenames.
    """
    if not RATE_SEARCH_ENDPOINT or not RATE_KNOWLEDGE_BASE_NAME:
        return "Rate search is not configured (missing RATE_SEARCH_ENDPOINT or RATE_KNOWLEDGE_BASE_NAME)."

    credential = (
        AzureKeyCredential(RATE_SEARCH_API_KEY)
        if RATE_SEARCH_API_KEY
        else DefaultAzureCredential()
    )
    try:
        if RATE_SEARCH_API_KEY:
            logger.info("search_rate_knowledge_base: using query key auth")
        else:
            logger.info("search_rate_knowledge_base: using managed identity auth")
        with SearchClient(
            endpoint=RATE_SEARCH_ENDPOINT,
            index_name=RATE_KNOWLEDGE_BASE_NAME,
            credential=credential,
        ) as client:
            results = client.search(query, top=5)
            docs = []
            for result in results:
                content = result.get("content") or result.get("chunk") or ""
                source = result.get("sourcefile") or result.get("title") or "unknown"
                if content:
                    docs.append(f"[{source}]\n{content}")
            return "\n\n---\n\n".join(docs) if docs else "No rate data found in knowledge base."
    except Exception as exc:
        logger.exception("search_rate_knowledge_base: search failed")
        return f"Search error: {exc}"


TOOLS = [search_rate_knowledge_base]

# ═══════════════════════════════════════════════════════════════════════════════
# 3. LANGGRAPH GRAPH
# ═══════════════════════════════════════════════════════════════════════════════


class State(TypedDict):
    messages: Annotated[list, add_messages]


def _build_graph() -> StateGraph:
    """Build and compile the Rate Intelligence LangGraph agent."""
    llm = AzureAIOpenAIApiChatModel(
        project_endpoint=FOUNDRY_PROJECT_ENDPOINT,
        credential=DefaultAzureCredential(),
        model=AZURE_AI_MODEL_DEPLOYMENT_NAME,
        streaming=True,
    )
    llm_with_tools = llm.bind_tools(TOOLS)

    def chatbot(state: State):
        return {"messages": [llm_with_tools.invoke(state["messages"])]}

    def route_tools(state: State):
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "tools"
        return END

    graph = StateGraph(State)
    graph.add_node("chatbot", chatbot)
    graph.add_node("tools", ToolNode(tools=TOOLS))
    graph.add_edge(START, "chatbot")
    graph.add_conditional_edges("chatbot", route_tools, {"tools": "tools", END: END})
    graph.add_edge("tools", "chatbot")
    return graph.compile()


GRAPH = _build_graph()

# ═══════════════════════════════════════════════════════════════════════════════
# 4. HISTORY CONVERSION
# ═══════════════════════════════════════════════════════════════════════════════


def _history_to_langchain_messages(history: list) -> list:
    """Convert Foundry Responses protocol history to LangChain message objects."""
    messages = []
    for item in history:
        if hasattr(item, "content") and item.content:
            for content in item.content:
                if isinstance(content, MessageContentOutputTextContent) and content.text:
                    messages.append(AIMessage(content=content.text))
                elif isinstance(content, MessageContentInputTextContent) and content.text:
                    messages.append(HumanMessage(content=content.text))
    return messages


# ═══════════════════════════════════════════════════════════════════════════════
# 5. FOUNDRY RESPONSES SERVER
# ═══════════════════════════════════════════════════════════════════════════════

app = ResponsesAgentServerHost(
    options=ResponsesServerOptions(default_fetch_history_count=20)
)


@app.response_handler
async def handle_create(
    request: CreateResponse,
    context: ResponseContext,
    cancellation_signal: asyncio.Event,
):
    """Run the LangGraph Rate Intelligence graph and stream the response."""

    async def run_graph():
        try:
            try:
                history = await context.get_history()
            except Exception:
                history = []

            current_input = (await context.get_input_text()) or "What are the current IRRRL rates?"

            lc_messages = _history_to_langchain_messages(history)

            # Inject the system prompt on the first turn.
            if not lc_messages:
                lc_messages.insert(0, SystemMessage(content=SYSTEM_PROMPT))

            lc_messages.append(HumanMessage(content=current_input))

            result = await GRAPH.ainvoke({"messages": lc_messages})

            raw = result["messages"][-1].content
            if isinstance(raw, list):
                yield "".join(
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in raw
                )
            else:
                yield raw or ""

        except Exception as exc:
            logger.exception("rate_intelligence_server: run_graph failed")
            yield f"[ERROR] {type(exc).__name__}: {exc}"

    return TextResponse(context, request, text=run_graph())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run()
