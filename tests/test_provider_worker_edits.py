import asyncio
from dataclasses import replace
import json
import subprocess
import pytest
import httpx
from dual_agent.config import UserError
from dual_agent.edits import stage_edit, make_diff
from dual_agent.provider import Relay
from dual_agent.worker import run_job


async def test_chat_adapter(cfg, monkeypatch):
    monkeypatch.setenv("DUAL_AGENT_API_KEY", "test-only-key")
    def respond(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "test-gemini"
        assert "tools" not in body
        assert request.headers["Authorization"] == "Bearer test-only-key"
        return httpx.Response(200, json={"choices": [{"message": {"content": "独立回答"}, "finish_reason": "stop"}]})
    value = await Relay(cfg, httpx.MockTransport(respond)).generate({"mode": "gemini", "prompt": "hi"})
    assert value == "独立回答"


async def test_responses_adapter(cfg):
    def respond(request):
        assert request.url.path == "/v1/responses"
        assert json.loads(request.content)["store"] is False
        return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]})
    assert await Relay(replace(cfg, protocol="responses"), httpx.MockTransport(respond)).generate({"mode": "gemini"}) == "hello"


@pytest.mark.parametrize("status,body", [(401, {"secret": "must not leak"}), (302, {}), (200, {"choices": []}), (200, {"choices": [{"message": {"content": "truncated"}, "finish_reason": "length"}]}), (200, {"choices": [{"message": {"content": None}, "finish_reason": "tool_calls"}]})])
async def test_bad_relay_response(cfg, status, body):
    count = 0
    def respond(request):
        nonlocal count
        count += 1
        return httpx.Response(status, json=body)
    with pytest.raises(UserError) as error:
        await Relay(cfg, httpx.MockTransport(respond)).generate({"mode": "gemini"})
    assert "must not leak" not in str(error.value)
    assert count == 1


async def test_response_limit(cfg):
    with pytest.raises(UserError):
        await Relay(replace(cfg, max_response_bytes=1024), httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 1100))).generate({"mode": "gemini"})


class SlowRelay:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.cancelled = False

    async def generate(self, _):
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return "second opinion"


async def test_concurrency_and_claim(make_turn, cfg):
    ledger, turn = make_turn()
    job = ledger.job_for_turn(turn["id"])
    relay = SlowRelay()
    a = asyncio.create_task(run_job(cfg, job["id"], relay))
    await relay.started.wait()
    await run_job(cfg, job["id"], relay)
    # Host-equivalent work proceeds while the Gemini request is still in flight.
    assert not a.done()
    assert relay.calls == 1
    relay.release.set()
    await a
    assert ledger.collect(turn["token"])["text"] == "second opinion"


async def test_cancel_aborts_request(make_turn, cfg):
    ledger, turn = make_turn()
    job = ledger.job_for_turn(turn["id"])
    relay = SlowRelay()
    task = asyncio.create_task(run_job(cfg, job["id"], relay))
    await relay.started.wait()
    ledger.cancel(turn["id"])
    await asyncio.wait_for(task, 2)
    assert relay.cancelled
    assert ledger.job(job["id"])["state"] == "cancelled"


def proposal(job, path="main.kt", content="fun main() = println(2)\n"):
    snap = json.loads(job["payload"])["snapshot"]
    return json.dumps({"summary": "Change greeting", "files": [{"path": path, "base_sha256": snap["files"][0]["sha256"], "content": content}]})


def test_edit_staged_not_applied(make_turn, workspace):
    ledger, turn = make_turn(mode="gemini:edit", files=("main.kt",))
    job = ledger.job_for_turn(turn["id"])
    ledger.claim(job["id"])
    result = stage_edit(ledger, job, proposal(job))
    assert result["main_workspace_modified"] is False
    assert (workspace / "main.kt").read_text() == "fun main() = println(1)\n"
    assert len(result["patch_sha256"]) == 64
    with pytest.raises(UserError):
        stage_edit(ledger, job, proposal(job))


@pytest.mark.parametrize("path", ["../escape.kt", "/tmp/escape.kt", "other.kt", ".git/config", ".env"])
def test_edit_deny_escape(make_turn, path):
    ledger, turn = make_turn(mode="gemini:edit", files=("main.kt",))
    job = ledger.job_for_turn(turn["id"])
    ledger.claim(job["id"])
    with pytest.raises(UserError):
        stage_edit(ledger, job, proposal(job, path=path))


def test_edit_requires_grant(make_turn):
    ledger, turn = make_turn(files=("main.kt",))
    job = ledger.job_for_turn(turn["id"])
    ledger.claim(job["id"])
    with pytest.raises(UserError):
        stage_edit(ledger, job, proposal(job))


def test_edit_base_hash(make_turn):
    ledger, turn = make_turn(mode="gemini:edit", files=("main.kt",))
    job = ledger.job_for_turn(turn["id"])
    ledger.claim(job["id"])
    obj = json.loads(proposal(job))
    obj["files"][0]["base_sha256"] = "invented"
    with pytest.raises(UserError):
        stage_edit(ledger, job, json.dumps(obj))


@pytest.mark.parametrize("path", ["main.kt", "a b.kt", "中文.kt"])
@pytest.mark.parametrize("before,after", [("old\n", "new\n"), ("old", "new"), ("old\n", "new"), ("", "new\n")])
def test_generated_diff_accepted_by_git(tmp_path, path, before, after):
    root = tmp_path / "git"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / path).write_text(before, encoding="utf-8")
    patch = make_diff(path, before, after)
    if not patch:
        return
    result = subprocess.run(["git", "-C", str(root), "apply", "--check", "-"], input=patch, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr + "\n" + patch
