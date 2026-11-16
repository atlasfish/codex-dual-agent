"""Real stdio, subprocess Hooks/workers and loopback HTTP; no provider account needed."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import pytest
from dual_agent.config import Config
from dual_agent.ledger import Ledger
from dual_agent.mcp_server import Server, RpcError, tools
from dual_agent.service import Service

ROOT = Path(__file__).resolve().parents[1]


def config_file(tmp_path, workspace, port):
    path = tmp_path / "config.toml"
    state = tmp_path / "state"
    path.write_text(f'''state_dir = {json.dumps(str(state))}
base_url = "http://127.0.0.1:{port}/v1"
model = "mock-gemini"
allowed_roots = [{json.dumps(str(workspace))}]
timeout_seconds = 10
''')
    return path, state


async def read_json(process):
    raw = await asyncio.wait_for(process.stdout.readline(), 5)
    assert raw, (await process.stderr.read()).decode()
    return json.loads(raw)


async def send(process, value):
    process.stdin.write((json.dumps(value) + "\n").encode())
    await process.stdin.drain()


async def test_mcp_protocol(cfg):
    server = Server(cfg)
    with pytest.raises(RpcError):
        await server.dispatch("tools/list", {})
    result = await server.dispatch("initialize", {"protocolVersion": "2099-01-01", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
    assert result["protocolVersion"] == "2025-06-18"
    server.notify("notifications/initialized", {})
    assert len((await server.dispatch("tools/list", {}))["tools"]) == 7
    with pytest.raises(RpcError):
        await server.dispatch("tools/call", {"name": "get_job", "arguments": {"turn_token": "x" * 32, "unexpected": True}})
    error = await server.dispatch("tools/call", {"name": "get_job", "arguments": {"turn_token": "x" * 32}})
    assert error["isError"]
    with pytest.raises(RpcError):
        await server.dispatch("tools/call", {"name": "await_result", "arguments": {"turn_token": "x" * 32, "timeout_seconds": True}})
    assert all("set_gemini_mode" != t["name"] for t in tools())


async def test_real_hook_worker_relay_and_mcp(tmp_path, workspace):
    received = threading.Event()
    release = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            received.set()
            release.wait(7)
            raw = json.dumps({"choices": [{"message": {"content": "Gemini 原文：保留这段意见。"}, "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    cfg_path, state = config_file(tmp_path, workspace, http.server_port)
    env = {**os.environ, "DUAL_AGENT_CONFIG": str(cfg_path)}
    process = None
    try:
        event = {"hook_event_name": "UserPromptSubmit", "session_id": "e2e", "turn_id": "e2e-first", "cwd": str(workspace), "prompt": "@both --files main.kt -- independently review"}
        hook = await asyncio.create_subprocess_exec(sys.executable, "-m", "dual_agent", "hook", cwd=ROOT, env=env,
                                                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        output, error = await asyncio.wait_for(hook.communicate(json.dumps(event).encode()), 5)
        assert hook.returncode == 0, error.decode()
        result = json.loads(output)
        metadata = json.loads(result["hookSpecificOutput"]["additionalContext"].splitlines()[1])
        token = metadata["turn_token"]
        assert await asyncio.to_thread(received.wait, 5)
        assert not release.is_set()  # Hook returned without waiting for network completion.
        cfg = Config(state_dir=state, allowed_roots=(str(workspace),), model="mock-gemini")
        assert Ledger(cfg).job(metadata["job_id"])["state"] == "running"
        Ledger(cfg).record_gpt(token, "GPT 的独立分析")
        release.set()
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "dual_agent", "mcp", cwd=ROOT, env=env,
                                                       stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        assert (await read_json(process))["result"]["protocolVersion"] == "2025-06-18"
        await send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "await_result", "arguments": {"turn_token": token, "timeout_seconds": 3}}})
        response = await read_json(process)
        assert response["id"] == 2
        value = json.loads(response["result"]["content"][0]["text"])
        assert value["state"] == "succeeded"
        assert value["text"] == "Gemini 原文：保留这段意见。"
        assert len(requests) == 1
        payload = json.loads(requests[0]["messages"][1]["content"])
        assert payload["snapshot"]["files"][0]["path"] == "main.kt"
        assert "GPT 的独立分析" not in json.dumps(payload, ensure_ascii=False)
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), 5) == 0
    finally:
        release.set()
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        http.shutdown()
        http.server_close()


async def test_wire_parse_errors_and_cancellation(tmp_path, workspace, make_turn, cfg):
    _, turn = make_turn()
    path = tmp_path / "wire.toml"
    path.write_text(f'state_dir = {json.dumps(str(cfg.state_dir))}\nmodel = "mock"\n')
    env = {**os.environ, "DUAL_AGENT_CONFIG": str(path)}
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "dual_agent", "mcp", cwd=ROOT, env=env,
                                                  stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        process.stdin.write(b"not-json\n")
        await process.stdin.drain()
        assert (await read_json(process))["error"]["code"] == -32700
        await send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {}}})
        assert (await read_json(process))["result"]["protocolVersion"] == "2024-11-05"
        await send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "await_result", "arguments": {"turn_token": turn["token"], "timeout_seconds": 10}}})
        await asyncio.sleep(0.1)
        await send(process, {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}})
        await send(process, {"jsonrpc": "2.0", "id": 3, "method": "ping"})
        assert (await read_json(process))["id"] == 3
        assert Ledger(cfg).job_for_turn(turn["id"])["state"] == "queued"  # Only the wait was cancelled.
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), 5) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
