"""Small tools-only MCP stdio transport, with protocol negotiation and cancellation.

Implements the required subset of MCP 2025-06-18 / 2024-11-05 for the
capabilities it advertises; does not pretend to implement resources or sampling.
Keeping this transport SDK-independent makes Hook startup small and testable.
"""
from __future__ import annotations
import asyncio
import json
import sys
from typing import Any
from . import __version__
from .config import Config, UserError
from .service import Service

TOKEN = {"type": "string", "minLength": 20, "maxLength": 100, "description": "Exact turn_token injected by the current UserPromptSubmit Hook"}
DEFINITIONS = {
    "get_context": ("Read this turn's frozen file evidence. No live workspace reads.", {}, []),
    "get_job": ("Check this turn's Gemini job without revealing its answer.", {}, []),
    "await_result": ("Collect the Gemini result or poll while pending; @both requires record_gpt_opinion first.", {"timeout_seconds": {"type": "number", "minimum": 0, "maximum": 25}}, []),
    "record_gpt_opinion": ("Persist an immutable independent GPT opinion before reading current Gemini output.", {"text": {"type": "string", "minLength": 1, "maxLength": 200000}}, ["text"]),
    "fetch_peer_opinion": ("Retrieve a prior completed opinion from this session, not the current round.", {"speaker": {"type": "string", "enum": ["gpt", "gemini"]}, "offset": {"type": "integer", "minimum": 0, "maximum": 1000}, "message_id": {"type": ["integer", "null"]}}, ["speaker"]),
    "compare_opinions": ("Fetch both most-recent historical opinions with provenance; no model call.", {}, []),
    "cancel_job": ("Cancel this turn's Gemini job. Already-incurred provider usage may still be billed.", {}, []),
}


def tools() -> list[dict]:
    return [{"name": name, "description": desc, "inputSchema": {"type": "object", "properties": {"turn_token": TOKEN, **props},
             "required": ["turn_token", *required], "additionalProperties": False},
             "annotations": {"readOnlyHint": name in {"get_context", "get_job", "fetch_peer_opinion", "compare_opinions"},
                             "destructiveHint": False, "openWorldHint": False}}
            for name, (desc, props, required) in DEFINITIONS.items()]


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        self.code, self.message = code, message


def validate_arguments(name: str, args: Any) -> dict:
    if name not in DEFINITIONS:
        raise RpcError(-32602, "Unknown tool")
    _, props, required = DEFINITIONS[name]
    schema = {"turn_token": TOKEN, **props}
    if not isinstance(args, dict) or set(args) - set(schema) or not {"turn_token", *required} <= set(args):
        raise RpcError(-32602, "Invalid or missing tool arguments")
    for key, value in args.items():
        rule = schema[key]
        kinds = rule["type"] if isinstance(rule["type"], list) else [rule["type"]]
        valid = (("string" in kinds and isinstance(value, str)) or
                 ("number" in kinds and isinstance(value, (int, float)) and not isinstance(value, bool)) or
                 ("integer" in kinds and isinstance(value, int) and not isinstance(value, bool)) or
                 ("null" in kinds and value is None))
        if not valid:
            raise RpcError(-32602, "Invalid argument type")
        if "enum" in rule and value not in rule["enum"]:
            raise RpcError(-32602, "Invalid argument value")
        if isinstance(value, str) and not rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", 1_000_000):
            raise RpcError(-32602, "Invalid argument length")
        if isinstance(value, (int, float)) and not rule.get("minimum", float("-inf")) <= value <= rule.get("maximum", float("inf")):
            raise RpcError(-32602, "Invalid numeric argument")
    return args


class Server:
    def __init__(self, cfg: Config):
        self.service = Service(cfg)
        self.initialized = False
        self.ready = False
        self.pending: dict[Any, asyncio.Task] = {}

    async def dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            if self.initialized:
                raise RpcError(-32600, "Already initialized")
            if not isinstance(params.get("protocolVersion"), str) or not isinstance(params.get("capabilities"), dict) or not isinstance(params.get("clientInfo"), dict):
                raise RpcError(-32602, "Invalid initialization parameters")
            self.initialized = True
            version = params["protocolVersion"]
            if version not in {"2024-11-05", "2025-06-18"}:
                version = "2025-06-18"
            return {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "codex-dual-agent", "version": __version__},
                    "instructions": "Use only the current Hook's turn_token. Gemini jobs are started by the Hook, never by a tool."}
        if method == "ping":
            return {}
        if not self.ready:
            raise RpcError(-32000, "Initialize and send notifications/initialized first")
        if method == "tools/list":
            if params.get("cursor"):
                raise RpcError(-32602, "Invalid cursor: all tools fit on one page")
            return {"tools": tools()}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                raise RpcError(-32602, "Tool name must be a string")
            args = validate_arguments(name, params.get("arguments", {}))
            try:
                value = await getattr(self.service, name)(**args)
                return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
            except UserError as exc:
                return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        raise RpcError(-32601, "Method not found")

    def notify(self, method: str, params: dict) -> None:
        if method == "notifications/initialized" and self.initialized:
            self.ready = True
        elif method == "notifications/cancelled":
            request_id = params.get("requestId")
            if isinstance(request_id, (str, int)) and not isinstance(request_id, bool):
                task = self.pending.get(request_id)
                if task:
                    task.cancel()  # Cancels this wait, not the durable Gemini job.

    @staticmethod
    def write(value: dict) -> None:
        line = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()

    async def respond(self, request: dict) -> None:
        request_id = request["id"]
        try:
            result = await self.dispatch(request["method"], request.get("params", {}))
            self.write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except asyncio.CancelledError:
            return
        except RpcError as exc:
            self.write({"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": exc.message}})
        except Exception:
            self.write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "Internal error (details withheld to protect local data)"}})
        finally:
            self.pending.pop(request_id, None)

    async def run(self) -> None:
        # One blocking stdin reader thread, asynchronous requests, bounded messages.
        try:
            while True:
                raw = await asyncio.to_thread(sys.stdin.buffer.readline, 1_048_577)
                if not raw:
                    break
                if len(raw) > 1_048_576:
                    self.write({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Message exceeds 1 MiB"}})
                    break
                try:
                    msg = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                except (ValueError, UnicodeError):
                    self.write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                    continue
                if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str) or not isinstance(msg.get("params", {}), dict):
                    self.write({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}})
                    continue
                if "id" not in msg:
                    self.notify(msg["method"], msg.get("params", {}))
                    continue
                request_id = msg["id"]
                if isinstance(request_id, bool) or not isinstance(request_id, (int, str)) or request_id in self.pending:
                    self.write({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid or duplicate request id"}})
                    continue
                if len(self.pending) >= 32:
                    self.write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": "Too many pending requests"}})
                    continue
                task = asyncio.create_task(self.respond(msg))
                self.pending[request_id] = task
                # Complete initialization before accepting the initialized notification.
                if msg["method"] == "initialize":
                    await task
        finally:
            tasks = list(self.pending.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
