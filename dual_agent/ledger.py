"""SQLite/WAL ledger shared by short-lived hooks, workers, and stdio servers."""
from __future__ import annotations
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid
from .config import Config, UserError
from .context import sha
from .routing import Route

TERMINAL = {"succeeded", "failed", "cancelled"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
 session_key TEXT PRIMARY KEY, host_session TEXT NOT NULL, workspace TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
 id TEXT PRIMARY KEY, session_key TEXT NOT NULL, host_turn TEXT NOT NULL,
 token TEXT UNIQUE NOT NULL, mode TEXT NOT NULL, prompt TEXT NOT NULL,
 snapshot TEXT NOT NULL, created REAL NOT NULL, stop_blocks INTEGER NOT NULL DEFAULT 0,
 continuation_pending INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0,
 UNIQUE(session_key, host_turn)
);
CREATE TABLE IF NOT EXISTS aliases (
 session_key TEXT NOT NULL, host_turn TEXT NOT NULL, turn_id TEXT NOT NULL,
 PRIMARY KEY(session_key, host_turn)
);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_key TEXT NOT NULL, turn_id TEXT NOT NULL,
 speaker TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(turn_id, speaker, kind)
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_key,id);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, turn_id TEXT UNIQUE NOT NULL, state TEXT NOT NULL,
 payload TEXT NOT NULL, created REAL NOT NULL, started REAL, deadline REAL NOT NULL,
 finished REAL, seen INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT, artifact TEXT
);
CREATE TABLE IF NOT EXISTS grants (
 job_id TEXT PRIMARY KEY, paths TEXT NOT NULL, snapshot_id TEXT NOT NULL,
 expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tool_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, name TEXT NOT NULL,
 arguments_sha256 TEXT NOT NULL, duration REAL NOT NULL, status TEXT NOT NULL,
 output_bytes INTEGER NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_events_job ON tool_events(job_id,id);
PRAGMA user_version = 1;
"""


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Ledger:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = cfg.state_dir.expanduser().absolute()
        if self.root.is_symlink():
            raise UserError("State directory must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            if self.root.stat().st_uid != os.getuid():
                raise UserError("State directory must be owned by the current user")
            self.root.chmod(0o700)
        self.path = self.root / "ledger.sqlite3"
        if self.path.is_symlink():
            raise UserError("Ledger must not be a symlink")
        with self.db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise UserError("Unsupported ledger schema version")
            conn.executescript(SCHEMA)
        if os.name == "posix":
            self.path.chmod(0o600)

    @contextmanager
    def db(self, write: bool = False):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.execute("COMMIT")
        except BaseException:
            if write and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def session_key(session: str, workspace: Path) -> str:
        if not isinstance(session, str) or not session or len(session) > 512:
            raise UserError("Missing/invalid Codex session_id")
        return sha(session + "\x00" + str(workspace.resolve()))

    def active(self, session: str, workspace: Path) -> bool:
        with self.db() as db:
            return db.execute("SELECT 1 FROM sessions WHERE session_key=?", (self.session_key(session, workspace),)).fetchone() is not None

    def by_host(self, session: str, workspace: Path, host_turn: str) -> dict | None:
        key = self.session_key(session, workspace)
        with self.db() as db:
            row = db.execute("SELECT t.* FROM turns t JOIN aliases a ON a.turn_id=t.id WHERE a.session_key=? AND a.host_turn=?", (key, host_turn)).fetchone()
            return dict(row) if row else None

    def by_token(self, token: str) -> dict:
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            raise UserError("Missing/invalid turn_token; use the value from the current Hook")
        with self.db() as db:
            row = db.execute("SELECT * FROM turns WHERE token=?", (token,)).fetchone()
            if row is None:
                raise UserError("Unknown turn_token; do not guess session identifiers")
            return dict(row)

    def create_turn(self, session: str, workspace: Path, host_turn: str, route: Route, snap: dict) -> tuple[dict, bool]:
        if not isinstance(host_turn, str) or not host_turn or len(host_turn) > 512:
            raise UserError("Codex must supply turn_id; upgrade the host instead of guessing an id")
        key = self.session_key(session, workspace)
        now = time.time()
        with self.db(True) as db:
            existing = db.execute("SELECT t.* FROM turns t JOIN aliases a ON a.turn_id=t.id WHERE a.session_key=? AND a.host_turn=?", (key, host_turn)).fetchone()
            if existing:
                if existing["mode"] != route.mode or existing["prompt"] != route.prompt:
                    raise UserError("Host turn_id reused with different input; start a new turn")
                return dict(existing), False
            needs_job = route.mode in {"both", "gemini", "gemini:edit"}
            if needs_job:
                count = db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running') AND deadline>?", (now,)).fetchone()[0]
                if count >= self.cfg.max_parallel_jobs:
                    raise UserError("Too many active Gemini jobs; cancel or await existing jobs")
            history = [dict(r) for r in db.execute(
                "SELECT id,speaker,kind,content FROM messages WHERE session_key=? AND speaker IN ('user','gpt','gemini') ORDER BY id DESC LIMIT 200", (key,)
            ).fetchall()][::-1]
            history, truncated = self._bound_history(history)
            turn_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
            db.execute("INSERT OR IGNORE INTO sessions VALUES(?,?,?)", (key, session, str(workspace)))
            db.execute("INSERT INTO turns(id,session_key,host_turn,token,mode,prompt,snapshot,created) VALUES(?,?,?,?,?,?,?,?)", (turn_id, key, host_turn, token, route.mode, route.prompt, dumps(snap), now))
            db.execute("INSERT INTO aliases VALUES(?,?,?)", (key, host_turn, turn_id))
            self._message(db, key, turn_id, "user", "input", route.prompt)
            if needs_job:
                job_id = uuid.uuid4().hex
                payload = {"mode": route.mode, "prompt": route.prompt, "history": history, "history_truncated": truncated, "snapshot": snap}
                # Deadline is end-to-end, not per read; no job is silently replayed.
                deadline = now + self.cfg.timeout_seconds + 30
                db.execute("INSERT INTO jobs(id,turn_id,state,payload,created,deadline) VALUES(?,?,?,?,?,?)", (job_id, turn_id, "queued", dumps(payload), now, deadline))
                if route.mode == "gemini:edit":
                    db.execute("INSERT INTO grants(job_id,paths,snapshot_id,expires) VALUES(?,?,?,?)", (job_id, dumps(list(route.files)), snap["snapshot_id"], deadline))
            return dict(db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()), True

    def _bound_history(self, rows: list[dict]) -> tuple[list[dict], bool]:
        selected: list[dict] = []
        remaining = self.cfg.history_chars
        truncated = False
        for row in reversed(rows):
            content = row["content"]
            if remaining <= 0:
                truncated = True
                break
            if len(content) > remaining:
                row = {**row, "content": "[older portion omitted]\n" + content[-remaining:], "truncated": True}
                truncated = True
            selected.append(row)
            remaining -= len(row["content"])
        return selected[::-1], truncated

    @staticmethod
    def _message(db, key: str, turn_id: str, speaker: str, kind: str, content: str):
        db.execute("INSERT OR IGNORE INTO messages(session_key,turn_id,speaker,kind,content,created) VALUES(?,?,?,?,?,?)", (key, turn_id, speaker, kind, content, time.time()))

    def record_gpt(self, token: str, text: str) -> dict:
        turn = self.by_token(token)
        if turn["mode"] not in {"gpt", "both"}:
            raise UserError("This route must not create an independent GPT opinion")
        if not isinstance(text, str) or not text.strip() or len(text) > 200_000:
            raise UserError("GPT opinion must be nonempty and <=200,000 characters")
        with self.db(True) as db:
            row = db.execute("SELECT content FROM messages WHERE turn_id=? AND speaker='gpt' AND kind='opinion'", (turn["id"],)).fetchone()
            if row and row["content"] != text:
                raise UserError("Independent opinion is immutable once recorded")
            self._message(db, turn["session_key"], turn["id"], "gpt", "opinion", text)
        return {"recorded": True, "sha256": sha(text)}

    def gpt_recorded(self, turn_id: str) -> bool:
        with self.db() as db:
            return db.execute("SELECT 1 FROM messages WHERE turn_id=? AND speaker='gpt' AND kind='opinion'", (turn_id,)).fetchone() is not None

    def peer(self, token: str, speaker: str, offset: int = 0, message_id: int | None = None) -> dict:
        turn = self.by_token(token)
        if speaker not in {"gpt", "gemini"} or not isinstance(offset, int) or not 0 <= offset <= 1000:
            raise UserError("Invalid speaker or offset")
        with self.db() as db:
            if message_id is not None:
                row = db.execute("SELECT id,turn_id,speaker,kind,content,created FROM messages WHERE session_key=? AND speaker=? AND id=? AND turn_id!=?", (turn["session_key"], speaker, message_id, turn["id"])).fetchone()
            else:
                row = db.execute("SELECT id,turn_id,speaker,kind,content,created FROM messages WHERE session_key=? AND speaker=? AND turn_id!=? ORDER BY id DESC LIMIT 1 OFFSET ?", (turn["session_key"], speaker, turn["id"], offset)).fetchone()
        return dict(row) if row else {"found": False, "speaker": speaker}

    def job_for_turn(self, turn_id: str) -> dict | None:
        with self.db(True) as db:
            db.execute("UPDATE jobs SET state='failed', error='Job deadline expired; not automatically retried', finished=? WHERE turn_id=? AND state IN ('queued','running') AND deadline<?", (time.time(), turn_id, time.time()))
            row = db.execute("SELECT * FROM jobs WHERE turn_id=?", (turn_id,)).fetchone()
            return dict(row) if row else None

    def job(self, job_id: str) -> dict:
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise UserError("Unknown job")
        return self.job_for_turn(row["turn_id"])  # type: ignore[return-value]

    def audit_tool(self, job_id: str, name: str, args_sha: str, duration: float, status: str, size: int) -> None:
        # Never persist command text, headers, web bodies or full tool outputs.
        with self.db(True) as db:
            db.execute("INSERT INTO tool_events(job_id,name,arguments_sha256,duration,status,output_bytes,created) VALUES(?,?,?,?,?,?,?)",
                       (job_id, name, args_sha, duration, status, size, time.time()))

    def tool_usage(self, job_id: str) -> list[dict]:
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT name,status,duration,output_bytes FROM tool_events WHERE job_id=? ORDER BY id", (job_id,))]

    def claim(self, job_id: str) -> bool:
        with self.db(True) as db:
            return db.execute("UPDATE jobs SET state='running',started=? WHERE id=? AND state='queued' AND deadline>?", (time.time(), job_id, time.time())).rowcount == 1

    def consume_grant(self, job_id: str, paths: list[str], snapshot_id: str) -> None:
        with self.db(True) as db:
            grant = db.execute("SELECT * FROM grants WHERE job_id=?", (job_id,)).fetchone()
            job = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not grant or not job or job["state"] != "running" or grant["used"] or grant["expires"] < time.time():
                raise UserError("Edit grant missing, expired, consumed, or cancelled")
            if snapshot_id != grant["snapshot_id"] or not set(paths) <= set(json.loads(grant["paths"])):
                raise UserError("Edit exceeds the user-authorized paths/snapshot")
            db.execute("UPDATE grants SET used=1 WHERE job_id=?", (job_id,))

    def finish(self, job_id: str, text: str, artifact: dict | None = None) -> bool:
        with self.db(True) as db:
            row = db.execute("SELECT j.*,t.session_key FROM jobs j JOIN turns t ON j.turn_id=t.id WHERE j.id=?", (job_id,)).fetchone()
            if not row or row["state"] != "running" or row["deadline"] < time.time():
                return False
            db.execute("UPDATE jobs SET state='succeeded',result=?,artifact=?,finished=? WHERE id=?", (text, dumps(artifact) if artifact else None, time.time(), job_id))
            self._message(db, row["session_key"], row["turn_id"], "gemini", "opinion", text)
            db.execute("UPDATE jobs SET payload='{}' WHERE id=?", (job_id,))
            return True

    def fail(self, job_id: str, safe_error: str) -> None:
        with self.db(True) as db:
            db.execute("UPDATE jobs SET state='failed',error=?,finished=? WHERE id=? AND state IN ('queued','running')", (safe_error, time.time(), job_id))

    def cancel(self, turn_id: str) -> dict:
        with self.db(True) as db:
            changed = db.execute("UPDATE jobs SET state='cancelled',finished=? WHERE turn_id=? AND state IN ('queued','running')", (time.time(), turn_id)).rowcount
        return {"cancelled": bool(changed)}

    def collect(self, token: str) -> dict:
        turn = self.by_token(token)
        job = self.job_for_turn(turn["id"])
        if not job:
            return {"state": "no_job"}
        if job["state"] == "succeeded" and turn["mode"] == "both" and not self.gpt_recorded(turn["id"]):
            raise UserError("Record the independent GPT opinion before reading this round's Gemini result")
        result = {k: job[k] for k in ("id", "state", "created", "started", "finished", "error")}
        result["tool_usage"] = self.tool_usage(job["id"])
        if job["state"] in TERMINAL:
            with self.db(True) as db:
                db.execute("UPDATE jobs SET seen=1 WHERE id=?", (job["id"],))
            if job["state"] == "succeeded":
                result.update(text=job["result"], sha256=sha(job["result"]), artifact=json.loads(job["artifact"]) if job["artifact"] else None)
        return result

    def request_continuation(self, turn_id: str) -> bool:
        with self.db(True) as db:
            return db.execute("UPDATE turns SET stop_blocks=1,continuation_pending=1 WHERE id=? AND stop_blocks=0", (turn_id,)).rowcount == 1

    def resume_continuation(self, session: str, workspace: Path, host_turn: str, token: str) -> dict | None:
        key = self.session_key(session, workspace)
        with self.db(True) as db:
            row = db.execute("SELECT * FROM turns WHERE token=? AND session_key=? AND continuation_pending=1", (token, key)).fetchone()
            if not row:
                return None
            # Internal Stop continuation is an alias, not another user turn or API call.
            db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?)", (key, host_turn, row["id"]))
            db.execute("UPDATE turns SET continuation_pending=0 WHERE id=?", (row["id"],))
            return dict(row)

    def close_turn(self, turn: dict, text: str) -> None:
        with self.db(True) as db:
            self._message(db, turn["session_key"], turn["id"], "host", "rendered", text)
            if turn["mode"] == "gpt" and text.strip():
                self._message(db, turn["session_key"], turn["id"], "gpt", "opinion", text)
            db.execute("UPDATE turns SET closed=1,continuation_pending=0 WHERE id=?", (turn["id"],))
