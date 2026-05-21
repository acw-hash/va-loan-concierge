"""
Azure Functions entry point for the VA Loan MCP Server.

This file is the Azure Functions entry point and owns the single FunctionApp
instance.  All triggers in this package must register on this same `app` object.

Three groups of triggers are defined here:

  MCP trigger (this file):
    POST /mcp — MCP JSON-RPC handler for the Calculator and Scheduler agents.
    Implements the MCP Streamable HTTP transport (initialize / tools/list / tools/call).
    No ASGI or `mcp` Python package required — plain JSON-RPC over HTTP.

  Ingestion triggers (ingest_trigger.py — imported below):
    POST /ingest  — manual trigger for the news ingestion pipeline (Phase 14)
    Timer /4h     — automatic ingestion every 4 hours

  Newsletter triggers (newsletter_trigger.py — imported below):
    POST /newsletter — manual/on-demand digest generation (Phase 15)
    Timer Monday 9am — weekly market intelligence digest

    Both trigger files are imported AFTER `app` is defined so they can attach
    their triggers to this same FunctionApp instance.  Azure Functions requires
    exactly one FunctionApp per Python worker — creating a second one would cause
    a runtime error.

MCP endpoint URL (written to .env as MCP_TOOLS_ENDPOINT by postprovision.ps1):
  https://<func-app-name>.azurewebsites.net/mcp
"""

import json
import logging

import azure.functions as func

from server import (
    TOOL_SCHEMAS,
    _validate_refi_inputs,
    _validate_scheduler_inputs,
    appointment_scheduler,
    refi_savings_calculator,
)

logger = logging.getLogger(__name__)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Register optional trigger blueprints after app creation. Keep MCP alive even
# if an optional trigger module fails to import.
try:
    from ingest_trigger import bp as ingest_bp
    app.register_blueprint(ingest_bp)
except Exception:
    logger.exception("function_app: failed to register ingest_trigger; /ingest disabled")

try:
    from newsletter_trigger import bp as newsletter_bp
    app.register_blueprint(newsletter_bp)
except Exception:
    logger.exception("function_app: failed to register newsletter_trigger; /newsletter disabled")

_PROTOCOL_VERSION = "2024-11-05"
_SERVER_INFO = {"name": "va-loan-tools", "version": "1.0.0"}


def _ok(request_id: object, result: dict) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}),
        status_code=200,
        headers={"Content-Type": "application/json"},
    )


def _err(request_id: object, code: int, message: str) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": code, "message": message}}),
        status_code=200,
        headers={"Content-Type": "application/json"},
    )


@app.route(route="mcp", methods=["GET", "POST", "DELETE", "HEAD", "OPTIONS"])
def mcp(req: func.HttpRequest) -> func.HttpResponse:
    """
    MCP Streamable HTTP endpoint.

    Handles the three JSON-RPC methods the Foundry runtime invokes:
      initialize   — exchange protocol version and capabilities
      tools/list   — return available tool schemas
      tools/call   — execute a tool and return the result
    """
    # GET and DELETE are part of the SSE session management handshake;
    # return 200 so the Foundry runtime doesn't treat them as errors.
    if req.method in ("GET", "DELETE", "HEAD", "OPTIONS"):
        return func.HttpResponse(status_code=200)

    try:
        body = req.get_json()
    except ValueError:
        try:
            raw = req.get_body().decode("utf-8")
            body = json.loads(raw) if raw else {}
        except Exception:
            return _err(None, -32700, "Parse error")

    method = body.get("method", "")
    request_id = body.get("id")
    params = body.get("params") or {}

    logger.info("mcp: %s (id=%s)", method, request_id)

    # Notifications (no id) require no response.
    if request_id is None:
        return func.HttpResponse(status_code=202)

    if method == "initialize":
        return _ok(request_id, {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": _SERVER_INFO,
        })

    if method == "ping":
        return _ok(request_id, {})

    if method == "tools/list":
        return _ok(request_id, {"tools": TOOL_SCHEMAS})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            if name == "refi_savings_calculator":
                if err := _validate_refi_inputs(arguments):
                    return _err(request_id, -32602, f"Input validation failed: {err}")
                result = refi_savings_calculator(**arguments)
            elif name == "appointment_scheduler":
                if err := _validate_scheduler_inputs(arguments):
                    return _err(request_id, -32602, f"Input validation failed: {err}")
                result = appointment_scheduler(**arguments)
            else:
                return _err(request_id, -32601, f"Unknown tool: {name}")
            return _ok(request_id, {
                "content": [{"type": "text", "text": json.dumps(result)}],
            })
        except Exception as exc:
            logger.exception("mcp: tool error for '%s'", name)
            return _err(request_id, -32603, str(exc))

    return _err(request_id, -32601, f"Method not found: {method}")
