from dataclasses import replace
import os
import pytest
from dual_agent.config import UserError
from dual_agent.context import snapshot, safe_read, validate_path
from dual_agent.routing import parse_prompt


@pytest.mark.parametrize("text,mode", [("@both hi", "both"), ("@gemini hi", "gemini"), ("@gpt hi", "gpt"), ("hello @both", "gpt"), ("```\n@both hi\n```", "gpt"), ("  @compare", "compare"), ("@gemini\n你好", "gemini")])
def test_routes(text, mode):
    assert parse_prompt(text).mode == mode


def test_files_and_edit():
    route = parse_prompt('@gemini:edit --files "src/a b.kt" main.kt -- 修复: x -- y')
    assert route.files == ("src/a b.kt", "main.kt")
    assert route.prompt == "修复: x -- y"


@pytest.mark.parametrize("text", ["@geminix hi", "@gemini:rw hi", "@both", "@gpt", "@gemini:edit fix", "@both --files x", "@both --files -- hi"])
def test_bad_route(text):
    with pytest.raises(UserError):
        parse_prompt(text)


@pytest.mark.parametrize("path", ["../etc/passwd", "/etc/passwd", "src/../a", "src//a", "C:/x", "src\\a", ".env", ".env.test", ".git/config", "private.pem", "key.jks", "gradle.properties", "a\x00b", "a\nb", "../x", "node_modules/a.js"])
def test_unsafe_paths(path):
    with pytest.raises(UserError):
        validate_path(path)


def test_snapshot_immutable(workspace, cfg):
    snap = snapshot(workspace, ("main.kt",), cfg)
    (workspace / "main.kt").write_text("changed\n")
    assert snap["files"][0]["content"] == "fun main() = println(1)\n"
    assert len(snap["snapshot_id"]) == 64


def test_denies_symlink_file(workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (workspace / "linked.txt").symlink_to(outside)
    with pytest.raises(UserError):
        safe_read(workspace, "linked.txt", 1024)


def test_denies_symlink_parent(workspace, tmp_path):
    (workspace / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(UserError):
        safe_read(workspace, "link/workspace/main.kt", 1024)


def test_denies_hardlink(workspace):
    os.link(workspace / "main.kt", workspace / "linked.kt")
    with pytest.raises(UserError):
        safe_read(workspace, "linked.kt", 1024)


def test_limits_and_binary(workspace, cfg):
    with pytest.raises(UserError):
        snapshot(workspace, ("main.kt",), replace(cfg, max_total_bytes=3))
    (workspace / "binary").write_bytes(b"a\x00b")
    with pytest.raises(UserError):
        safe_read(workspace, "binary", 1024)


def test_ignore(workspace, cfg):
    (workspace / ".dual-agentignore").write_text("# comment\n*.kt\n")
    with pytest.raises(UserError):
        snapshot(workspace, ("main.kt",), cfg)


def test_config_fail_closed(cfg, workspace):
    with pytest.raises(UserError):
        replace(cfg, allowed_roots=()).check_workspace(workspace)
    with pytest.raises(UserError):
        replace(cfg, base_url="http://example.com/v1", allow_remote_relay=True).validate()
    with pytest.raises(UserError):
        replace(cfg, base_url="https://name:secret@example.com/v1", allow_remote_relay=True).validate()
    replace(cfg, base_url="https://example.com/v1", allow_remote_relay=True).validate()
    cfg.validate()


def test_key_file_permissions(tmp_path, cfg, monkeypatch):
    monkeypatch.delenv("DUAL_AGENT_API_KEY", raising=False)
    key = tmp_path / "relay.key"
    key.write_text("not-a-real-key\n")
    key.chmod(0o644)
    with pytest.raises(UserError):
        replace(cfg, api_key_file=str(key)).api_key()
    key.chmod(0o600)
    assert replace(cfg, api_key_file=str(key)).api_key() == "not-a-real-key"


def test_config_rejects_allowed_roots_string(tmp_path, monkeypatch):
    from dual_agent.config import Config
    path = tmp_path / "config.toml"
    path.write_text('allowed_roots = "/tmp/project"\n')
    monkeypatch.setenv("DUAL_AGENT_CONFIG", str(path))
    with pytest.raises(UserError, match="array"):
        Config.load()


def test_git_evidence_diff_uses_frozen_content(workspace, cfg, monkeypatch):
    import subprocess
    import dual_agent.context as context
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "add", "main.kt"], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
    (workspace / "main.kt").write_text("frozen content\n")
    original = context.git
    def mutate_after_freeze(root, *args, **kwargs):
        if args[:2] == ("rev-parse", "--verify"):
            (workspace / "main.kt").write_text("live mutation\n")
        return original(root, *args, **kwargs)
    monkeypatch.setattr(context, "git", mutate_after_freeze)
    snap = snapshot(workspace, ("main.kt",), replace(cfg, include_git_diff=True))
    assert snap["files"][0]["content"] == "frozen content\n"
    assert "+frozen content" in snap["git_diff"]
    assert "live mutation" not in snap["git_diff"]
    assert len(snap["git_head"]) in {40, 64}
