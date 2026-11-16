"""Model-facing API: no tool can mint grants, choose a new endpoint, or write the repo."""
from __future__ import annotations
import asyncio
import json
import time
from .config import Config, UserError
from .ledger import Ledger, TERMINAL


class Service:
    def __init__(self, cfg: Config):
        self.ledger = Ledger(cfg)

    async def get_context(self, turn_token: str) -> dict:
        turn = self.ledger.by_token(turn_token)
        return {"mode": turn["mode"], "prompt": turn["prompt"], "snapshot": json.loads(turn["snapshot"])}

    async def get_job(self, turn_token: str) -> dict:
        turn = self.ledger.by_token(turn_token)
        job = self.ledger.job_for_turn(turn["id"])
        if not job:
            return {"state": "no_job"}
        # Deliberately omit payload/result/artifact to preserve blind independence.
        return {k: job[k] for k in ("id", "state", "created", "started", "deadline", "finished", "seen", "error")}

    async def await_result(self, turn_token: str, timeout_seconds: float = 20) -> dict:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 <= timeout_seconds <= 25:
            raise UserError("timeout_seconds must be between 0 and 25; poll again if still running")
        turn = self.ledger.by_token(turn_token)
        end = time.monotonic() + timeout_seconds
        while True:
            job = self.ledger.job_for_turn(turn["id"])
            if not job or job["state"] in TERMINAL or time.monotonic() >= end:
                return self.ledger.collect(turn_token)
            await asyncio.sleep(0.1)

    async def record_gpt_opinion(self, turn_token: str, text: str) -> dict:
        return self.ledger.record_gpt(turn_token, text)

    async def fetch_peer_opinion(self, turn_token: str, speaker: str, offset: int = 0, message_id: int | None = None) -> dict:
        return self.ledger.peer(turn_token, speaker, offset, message_id)

    async def compare_opinions(self, turn_token: str) -> dict:
        return {"gpt": self.ledger.peer(turn_token, "gpt"), "gemini": self.ledger.peer(turn_token, "gemini"),
                "instruction": "Compare these labeled historical opinions. Check turn_id: they may address different turns. No additional model request was made."}

    async def cancel_job(self, turn_token: str) -> dict:
        return self.ledger.cancel(self.ledger.by_token(turn_token)["id"])
