import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer", ROOT / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_build_plugin_uses_absolute_isolated_python(tmp_path):
    target = tmp_path / "plugin"
    interpreter = tmp_path / "runtime with space/bin/python"
    installer.build_plugin(ROOT, target, interpreter)
    for name in ("mcp.json", ".mcp.json"):
        server = json.loads((target / name).read_text())["mcpServers"]["dual_agent"]
        assert server["command"] == str(interpreter)
        assert server["args"] == ["-I", "-m", "dual_agent", "mcp"]
    hooks = json.loads((target / "hooks/hooks.json").read_text())["hooks"]
    assert set(hooks) == {"UserPromptSubmit", "Stop", "Interrupt", "PreToolUse"}
    for groups in hooks.values():
        for group in groups:
            for hook in group["hooks"]:
                assert shlex.split(hook["command"]) == [str(interpreter), "-I", "-m", "dual_agent", "hook"]
    assert not (target / "config.toml").exists()


def test_marketplace_preserves_other_entries_and_is_idempotent(tmp_path):
    catalog = tmp_path / ".agents/plugins/marketplace.json"
    catalog.parent.mkdir(parents=True)
    original = {"name": "existing", "other": {"keep": True}, "plugins": [{"name": "other", "source": "./other"}]}
    catalog.write_text(json.dumps(original))
    plugin = tmp_path / ".codex/plugins/codex-dual-agent"
    assert installer.register_marketplace(tmp_path, plugin) == "existing"
    result = json.loads(catalog.read_text())
    assert result["plugins"][0] == original["plugins"][0]
    assert result["other"] == original["other"]
    assert len(result["plugins"]) == 2
    assert len(list(catalog.parent.glob("*.backup-*"))) == 1
    assert installer.register_marketplace(tmp_path, plugin) == "existing"
    assert json.loads(catalog.read_text()) == result


def test_marketplace_refuses_conflicting_source(tmp_path):
    catalog = tmp_path / ".agents/plugins/marketplace.json"
    catalog.parent.mkdir(parents=True)
    original = {"name": "personal", "plugins": [{"name": "codex-dual-agent", "source": {"path": "./other"}}]}
    catalog.write_text(json.dumps(original))
    with pytest.raises(RuntimeError):
        installer.register_marketplace(tmp_path, tmp_path / ".codex/plugins/codex-dual-agent")
    assert json.loads(catalog.read_text()) == original


@pytest.mark.parametrize("script,args", [("install.py", []), ("publish.py", ["--repo", "atlasfish/codex-dual-agent"])])
def test_script_dry_run_has_no_external_auth_requirement(script, args):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args, "--dry-run"], capture_output=True, text=True, check=True)
    output = json.loads(result.stdout)
    if script == "publish.py":
        assert output["visibility"] == "private"
        assert output["tests_before_push"] is True
    else:
        assert "runtime" in output


def test_manifest_and_templates_are_valid_json():
    for name in ("plugin.json", "mcp.json", ".mcp.json", ".codex-plugin/plugin.json", ".agents/plugins/marketplace.json", "hooks/hooks.json"):
        assert isinstance(json.loads((ROOT / name).read_text()), dict)
