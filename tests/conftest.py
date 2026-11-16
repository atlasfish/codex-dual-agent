from dataclasses import replace
import pytest
from dual_agent.config import Config
from dual_agent.context import snapshot
from dual_agent.ledger import Ledger
from dual_agent.routing import Route


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "workspace"
    path.mkdir()
    (path / "main.kt").write_text("fun main() = println(1)\n", encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def cfg(tmp_path, workspace):
    return Config(state_dir=tmp_path / "state", allowed_roots=(str(workspace),), model="test-gemini", timeout_seconds=5)


@pytest.fixture
def make_turn(cfg, workspace):
    def make(mode="gemini", session="session-a", turn="turn-a", files=(), prompt="Analyze this"):
        ledger = Ledger(cfg)
        result, _ = ledger.create_turn(session, workspace, turn, Route(mode, prompt, tuple(files), True), snapshot(workspace, tuple(files), cfg))
        return ledger, result
    return make
