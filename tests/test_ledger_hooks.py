from dataclasses import replace
import json
import time
import pytest
from dual_agent.config import UserError
from dual_agent.hooks import handle, GEMINI_HEADER
from dual_agent.ledger import Ledger
from dual_agent.service import Service


def event(workspace, prompt="@both test", turn="host-turn", name="UserPromptSubmit", **kwargs):
    return {"hook_event_name": name, "cwd": str(workspace), "session_id": "s", "turn_id": turn, "prompt": prompt, **kwargs}


def token_from(result):
    return json.loads(result["hookSpecificOutput"]["additionalContext"].splitlines()[1])["turn_token"]


def test_hook_starts_before_return_and_deduplicates(cfg, workspace):
    launched = []
    value = handle(event(workspace), cfg, launcher=launched.append)
    token = token_from(value)
    ledger = Ledger(cfg)
    turn = ledger.by_token(token)
    job = ledger.job_for_turn(turn["id"])
    assert launched == [job["id"]]
    assert token_from(handle(event(workspace), cfg, launcher=launched.append)) == token
    assert ledger.claim(job["id"])
    assert not ledger.claim(job["id"])
    assert len(launched) == 2  # Redelivery can launch another worker, but cannot claim twice.
    with ledger.db() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_default_does_not_capture_inactive(cfg, workspace):
    assert handle(event(workspace, "normal prompt"), cfg, launcher=lambda _: None) == {}
    with Ledger(cfg).db() as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_duplicate_files_rejected(cfg, workspace):
    handle(event(workspace, "@both --files main.kt -- test"), cfg, launcher=lambda _: None)
    with pytest.raises(UserError):
        handle(event(workspace, "@both test"), cfg, launcher=lambda _: None)


async def test_blind_independence(make_turn, cfg):
    ledger, turn = make_turn(mode="both")
    job = ledger.job_for_turn(turn["id"])
    ledger.claim(job["id"])
    ledger.finish(job["id"], "Gemini independent opinion")
    with pytest.raises(UserError):
        await Service(cfg).await_result(turn["token"], 0)
    ledger.record_gpt(turn["token"], "GPT independent opinion")
    assert (await Service(cfg).await_result(turn["token"], 0))["text"] == "Gemini independent opinion"
    with pytest.raises(UserError):
        ledger.record_gpt(turn["token"], "Changed after reading Gemini")


def test_history_durable_and_scoped(make_turn, cfg):
    ledger, first = make_turn()
    job = ledger.job_for_turn(first["id"])
    ledger.claim(job["id"])
    ledger.finish(job["id"], "独立意见原文")
    _, followup = make_turn(mode="gpt", turn="turn-b")
    assert Ledger(cfg).peer(followup["token"], "gemini")["content"] == "独立意见原文"
    _, stranger = make_turn(mode="gpt", session="session-b", turn="turn-b")
    assert Ledger(cfg).peer(stranger["token"], "gemini")["found"] is False


def test_completed_history_frozen(make_turn):
    ledger, old = make_turn(mode="gpt")
    ledger.record_gpt(old["token"], "Old GPT")
    _, current = make_turn(turn="next")
    job = ledger.job_for_turn(current["id"])
    ledger.close_turn(old, "Rendered later")
    payload = json.loads(job["payload"])
    assert any(r["content"] == "Old GPT" for r in payload["history"])
    assert all(r["speaker"] != "host" for r in payload["history"])
    assert all(r["content"] != "Rendered later" for r in payload["history"])


def test_gemini_render_not_mislabeled_gpt(cfg, workspace):
    token = token_from(handle(event(workspace, "@gemini hello"), cfg, launcher=lambda _: None))
    ledger = Ledger(cfg)
    turn = ledger.by_token(token)
    job = ledger.job_for_turn(turn["id"])
    ledger.claim(job["id"])
    ledger.finish(job["id"], "Gemini text")
    ledger.collect(token)
    assert handle(event(workspace, name="Stop", last_assistant_message=GEMINI_HEADER + "Gemini text"), cfg) == {}
    with ledger.db() as db:
        assert db.execute("SELECT count(*) FROM messages WHERE speaker='gpt'").fetchone()[0] == 0


def test_stop_continuation_is_alias_not_new_api_call(cfg, workspace):
    token = token_from(handle(event(workspace, "@gemini hi"), cfg, launcher=lambda _: None))
    stop = handle(event(workspace, name="Stop", last_assistant_message="wrong"), cfg)
    assert stop["decision"] == "block"
    resumed = handle(event(workspace, stop["reason"], turn="continued"), cfg, launcher=lambda _: pytest.fail("must not relaunch"))
    assert token_from(resumed) == token
    stop2 = handle(event(workspace, name="Stop", turn="continued", stop_hook_active=True, last_assistant_message="still wrong"), cfg)
    assert "decision" not in stop2
    assert "warning" in stop2["systemMessage"]
    with Ledger(cfg).db() as db:
        assert db.execute("SELECT count(*) FROM turns").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_only_gemini_blocks_native_write_tools(cfg, workspace):
    handle(event(workspace, "@gemini hi"), cfg, launcher=lambda _: None)
    result = handle(event(workspace, name="PreToolUse", tool_name="apply_patch"), cfg)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    handle(event(workspace, "@gpt hi", turn="second"), cfg)
    assert handle(event(workspace, name="PreToolUse", turn="second", tool_name="apply_patch"), cfg) == {}


def test_interrupt_does_not_save_draft(cfg, workspace):
    token = token_from(handle(event(workspace, "@gemini hi"), cfg, launcher=lambda _: None))
    handle(event(workspace, name="Interrupt", last_assistant_message="partial draft"), cfg)
    ledger = Ledger(cfg)
    turn = ledger.by_token(token)
    assert ledger.job_for_turn(turn["id"])["state"] == "cancelled"
    with ledger.db() as db:
        assert not db.execute("SELECT 1 FROM messages WHERE content='partial draft'").fetchone()


def test_deadline_fails_without_replay(make_turn):
    ledger, turn = make_turn()
    with ledger.db(True) as db:
        db.execute("UPDATE jobs SET deadline=?", (time.time() - 1,))
    job = ledger.job_for_turn(turn["id"])
    assert job["state"] == "failed"
    assert not ledger.claim(job["id"])


def test_cancel_prevents_late_completion(make_turn):
    ledger, turn = make_turn()
    job = ledger.job_for_turn(turn["id"])
    ledger.claim(job["id"])
    ledger.cancel(turn["id"])
    assert not ledger.finish(job["id"], "late response")
    assert ledger.collect(turn["token"])["state"] == "cancelled"


def test_parallel_job_limit(cfg, workspace):
    cfg = replace(cfg, max_parallel_jobs=1)
    handle(event(workspace, "@gemini first", turn="first"), cfg, launcher=lambda _: None)
    with pytest.raises(UserError):
        handle(event(workspace, "@gemini second", turn="second"), cfg, launcher=lambda _: None)
