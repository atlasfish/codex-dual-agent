"""Discover local SKILL.md instructions; no claim to inherit host MCP/OAuth tools."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import yaml
from .config import Config, UserError


class Skills:
    def __init__(self, cfg: Config, workspace: Path):
        self.cfg = cfg
        self.roots = [workspace / ".agents/skills", workspace / ".codex/skills",
                      Path.home() / ".agents/skills", Path.home() / ".codex/skills"]
        self.roots += [Path(p).expanduser() for p in cfg.skill_roots]
        self.entries: dict[str, dict] = {}
        self.truncated = False
        if cfg.skills_enabled:
            self._discover()

    @staticmethod
    def _text(path: Path) -> str:
        with path.open("rb") as stream:
            data = stream.read(128_001)
        if len(data) > 128_000:
            raise UserError("Skill exceeds 128 kB; keep SKILL.md focused")
        return data.decode("utf-8-sig")

    def _discover(self) -> None:
        visited = set()
        directories = set()
        scanned = 0
        for root in self.roots:
            if not root.is_dir():
                continue
            for current, dirs, files in os.walk(root, followlinks=True):
                resolved = Path(current).resolve()
                if resolved in directories:
                    dirs[:] = []
                    continue
                directories.add(resolved)
                scanned += 1
                if scanned > 4096 or len(self.entries) >= 128:
                    self.truncated = True
                    return
                depth = len(Path(current).relative_to(root).parts)
                dirs[:] = sorted(d for d in dirs if d not in {".git", ".venv", "node_modules", "__pycache__"}) if depth < 5 else []
                if "SKILL.md" not in files:
                    continue
                path = (Path(current) / "SKILL.md").resolve()
                if path in visited:
                    continue
                visited.add(path)
                try:
                    text = self._text(path)
                    parts = text.split("---", 2)
                    meta = yaml.safe_load(parts[1]) if text.startswith("---") and len(parts) == 3 else {}
                    if not isinstance(meta, dict):
                        meta = {}
                    key = hashlib.sha256(str(path).encode()).hexdigest()[:16]
                    self.entries[key] = {"id": key, "name": str(meta.get("name", path.parent.name))[:200],
                                         "description": str(meta.get("description", ""))[:1000], "path": str(path)}
                except (OSError, UnicodeError, yaml.YAMLError, UserError):
                    continue

    def list(self) -> dict:
        return {"skills": list(self.entries.values()), "truncated": self.truncated,
                "note": "Local instructions/scripts only. Host connector tools, MCP authentication, and subscriptions are not inherited."}

    def read(self, skill_id: str) -> dict:
        entry = self.entries.get(skill_id)
        if entry is None:
            raise UserError("Unknown skill_id; use list_skills first")
        path = Path(entry["path"])
        return {**entry, "directory": str(path.parent), "content": self._text(path),
                "execution": "Read the instructions, then use shell with directory as cwd for scripts. Do not invent unavailable MCP tools."}
