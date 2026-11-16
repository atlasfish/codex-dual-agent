#!/usr/bin/env python3
"""Install a private runtime + local plugin; preserve other marketplaces and configs."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import venv

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILES = ("plugin.json", "mcp.json", ".mcp.json", ".codex-plugin", "hooks", "skills")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def register_marketplace(home: Path, plugin: Path) -> str:
    catalog = home / ".agents/plugins/marketplace.json"
    source = "./" + plugin.relative_to(home).as_posix()
    data = json.loads(catalog.read_text(encoding="utf-8")) if catalog.exists() else {
        "name": "dual-agent-personal", "interface": {"displayName": "Personal Developer Plugins"}, "plugins": []}
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list) or not isinstance(data.get("name"), str):
        raise RuntimeError("Existing marketplace has an invalid shape; no changes made")
    for entry in data["plugins"]:
        if entry.get("name") == "codex-dual-agent":
            existing = entry.get("source")
            existing_path = existing.get("path") if isinstance(existing, dict) else existing
            if existing_path != source:
                raise RuntimeError("A codex-dual-agent entry already points elsewhere; no overwrite performed")
            return data["name"]
    data["plugins"].append({"name": "codex-dual-agent", "source": {"source": "local", "path": source},
                            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Developer Tools"})
    if catalog.exists():
        shutil.copy2(catalog, catalog.with_name(f"marketplace.json.backup-{time.time_ns()}"))
    atomic_json(catalog, data)
    return data["name"]


def build_plugin(source: Path, target: Path, python: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name in PLUGIN_FILES:
        src, dst = source / name, target / name
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    for name in ("mcp.json", ".mcp.json"):
        path = target / name
        data = json.loads(path.read_text(encoding="utf-8"))
        data["mcpServers"]["dual_agent"].update(command=str(python), args=["-I", "-m", "dual_agent", "mcp"])
        atomic_json(path, data)
    hooks = json.loads((target / "hooks/hooks.json").read_text(encoding="utf-8"))
    cmd_args = [str(python), "-I", "-m", "dual_agent", "hook"]
    hook_command = subprocess.list2cmdline(cmd_args) if os.name == "nt" else shlex.join(cmd_args)
    for groups in hooks["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                hook["command"] = hook_command
                hook["commandWindows"] = "& '" + str(python).replace("'", "''") + "' -I -m dual_agent hook"
    atomic_json(target / "hooks/hooks.json", hooks)


def runtime_python_path(runtime: Path) -> Path:
    return runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11+ is required")
    home = Path.home()
    runtime = home / ".local/share/codex-dual-agent/runtime"
    plugin = home / ".codex/plugins/codex-dual-agent"
    config = home / ".config/codex-dual-agent/config.toml"
    python = runtime_python_path(runtime)
    if args.dry_run:
        print(json.dumps({"runtime": str(runtime), "plugin": str(plugin), "config": str(config),
                          "marketplace": str(home / ".agents/plugins/marketplace.json"), "network": "pip install dependencies only; no relay call"}, indent=2))
        return 0
    if os.name == "posix":
        os.umask(0o077)
    venv.EnvBuilder(with_pip=True).create(runtime)
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", str(ROOT)], check=True)
    build_plugin(ROOT, plugin, python)
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(ROOT / "config.example.toml", config)
        if os.name == "posix":
            config.chmod(0o600)
    marketplace = register_marketplace(home, plugin)
    print(f"Installed runtime: {runtime}\nPreserved/created config: {config}\n")
    print("NOTICE: Gemini tools now execute local shell/scripts and network requests with your user privileges. Advisor is NOT a read-only sandbox.")
    print("Next: set relay model/base_url/allowed_roots and optional key file in that config.")
    doctor_cmd = [str(python), "-I", "-m", "dual_agent", "doctor"]
    print("Validate: " + (subprocess.list2cmdline(doctor_cmd) if os.name == "nt" else shlex.join(doctor_cmd)))
    print(f"Install from your local plugin source in Codex Desktop, or run: codex plugin add codex-dual-agent@{marketplace}")
    print("Restart Codex Desktop and review/trust this plugin's Hooks before testing.")
    print("No main-model provider configuration or other plugin entries were modified.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
