"""One detached worker per job; SQLite claims prevent duplicate relay requests."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from .config import Config, UserError
from .edits import stage_edit
from .ledger import Ledger, TERMINAL
from .provider import Relay
from .tools import ToolRuntime


def launch(job_id: str) -> None:
    # Run from our trusted package directory, never from an arbitrary user's repo.
    if os.name == "posix":
        kwargs = {"start_new_session": True}
    else:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs = {"creationflags": flags}
    subprocess.Popen([sys.executable, *(["-I"] if sys.flags.isolated else []), "-m", "dual_agent", "worker", job_id],
                     cwd=str(Path(__file__).resolve().parent.parent), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **kwargs)


async def run_job(cfg: Config, job_id: str, relay: Relay | None = None) -> None:
    ledger = Ledger(cfg)
    if not ledger.claim(job_id):
        return
    job = ledger.job(job_id)
    if relay is None:
        try:
            with ledger.db() as db:
                workspace = db.execute("SELECT s.workspace FROM sessions s JOIN turns t ON t.session_key=s.session_key WHERE t.id=?", (job["turn_id"],)).fetchone()[0]
            runtime = ToolRuntime(cfg, Path(workspace), audit=lambda *args: ledger.audit_tool(job_id, *args)) if cfg.tools_enabled else None
            relay = Relay(cfg, runtime=runtime)
        except Exception:
            ledger.fail(job_id, "Could not initialize local Agent tools; check workspace/configuration")
            return
    task = asyncio.create_task(relay.generate(json.loads(job["payload"])))
    try:
        while True:
            await asyncio.wait({task}, timeout=0.2)
            current = ledger.job(job_id)
            if current["state"] in TERMINAL:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return
            if task.done():
                text = task.result()
                artifact = None
                if json.loads(job["payload"])["mode"] == "gemini:edit":
                    artifact = stage_edit(ledger, job, text)
                ledger.finish(job_id, text, artifact)
                return
    except UserError as exc:
        ledger.fail(job_id, str(exc))
    except asyncio.CancelledError:
        ledger.cancel(job["turn_id"])
        raise
    except Exception:
        # Never persist raw HTTP errors, headers, secret-bearing URLs, or tracebacks.
        ledger.fail(job_id, "Unexpected worker error; inspect configuration and run local tests")
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
