"""CLI entrypoints used by the plugin, installer and diagnostics."""
from __future__ import annotations
import argparse
import asyncio
import importlib.util
import json
import os
import sys
from .config import Config, UserError


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="dual-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="Read one Codex Hook JSON object from stdin")
    sub.add_parser("mcp", help="Serve tools on stdio")
    sub.add_parser("doctor", help="Validate local configuration; makes no network request")
    worker = sub.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("job_id")
    inspect = sub.add_parser("job", help="Inspect a durable job locally (may contain project data)")
    inspect.add_argument("job_id")
    args = parser.parse_args()
    event: dict = {}
    try:
        if args.command == "hook":
            raw = sys.stdin.buffer.read(1_000_001)
            if len(raw) > 1_000_000:
                raise UserError("Hook input exceeds 1 MB")
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise UserError("Hook input must be an object")
        cfg = Config.load()
        if args.command == "hook":
            from .hooks import handle
            print(json.dumps(handle(event, cfg), ensure_ascii=False))
        elif args.command == "mcp":
            from .mcp_server import Server
            asyncio.run(Server(cfg).run())
        elif args.command == "worker":
            from .worker import run_job
            asyncio.run(run_job(cfg, args.job_id))
        elif args.command == "job":
            from .ledger import Ledger
            value = Ledger(cfg).job(args.job_id)
            value.pop("payload", None)
            print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            from .ledger import Ledger
            ledger = Ledger(cfg)
            ready = bool(cfg.allowed_roots) and cfg.model != "REPLACE_WITH_YOUR_GEMINI_MODEL" and importlib.util.find_spec("httpx") is not None
            # Validate the credential source without displaying its contents.
            has_key = bool(cfg.api_key())
            print(json.dumps({"config_valid": True, "ready": ready, "state_dir": str(ledger.root),
                  "allowed_roots_configured": bool(cfg.allowed_roots), "relay_model_configured": cfg.model != "REPLACE_WITH_YOUR_GEMINI_MODEL",
                  "credential_configured": has_key, "httpx_installed": importlib.util.find_spec("httpx") is not None,
                  "network_tested": False, "desktop_tested": False}, indent=2))
            return 0 if ready else 1
        return 0
    except (UserError, ValueError, OSError) as exc:
        # Only UserError is intentionally safe; other details might contain input/secrets.
        text = str(exc) if isinstance(exc, UserError) else "Invalid configuration/input or local I/O failure"
        if args.command == "hook":
            if event.get("hook_event_name") == "UserPromptSubmit" and str(event.get("prompt", "")).lstrip().startswith(("@gpt", "@gemini", "@both", "@compare", "[dual-agent-resume:")):
                print(json.dumps({"decision": "block", "reason": "Dual-Agent: " + text}))
            else:
                print(json.dumps({"systemMessage": "Dual-Agent: " + text}))
            return 0
        print("dual-agent: " + text, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
