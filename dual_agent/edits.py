"""Validate model-suggested replacements and stage a reviewable patch only."""
from __future__ import annotations
import difflib
import json
import os
from pathlib import Path
import tempfile
from .config import UserError
from .context import sha, validate_path
from .ledger import Ledger, dumps


def quote_git_path(path: str) -> str:
    raw = path.encode("utf-8")
    if all(33 <= b <= 126 and b not in (34, 92) for b in raw):
        return path
    return '"' + ''.join(chr(b) if 33 <= b <= 126 and b not in (34, 92) else f"\\{b:03o}" for b in raw) + '"'


def make_diff(path: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                 fromfile=quote_git_path("a/" + path), tofile=quote_git_path("b/" + path))
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


def stage_edit(ledger: Ledger, job: dict, text: str) -> dict:
    source = text.strip()
    if source.startswith("```json\n") and source.endswith("\n```"):
        source = source[8:-4]
    try:
        data = json.loads(source)
    except json.JSONDecodeError as exc:
        raise UserError("Edit proposal must be a JSON object, not executable instructions") from exc
    if not isinstance(data, dict) or set(data) != {"summary", "files"} or not isinstance(data["summary"], str):
        raise UserError("Edit proposal schema: {summary: string, files: array}")
    payload = json.loads(job["payload"])
    snap = payload["snapshot"]
    originals = {f["path"]: f for f in snap["files"]}
    files = data["files"]
    if not isinstance(files, list) or not files or len(files) > ledger.cfg.max_files:
        raise UserError("Edit proposal needs a nonempty bounded files array")
    seen: set[str] = set()
    total = 0
    patches = []
    for file in files:
        if not isinstance(file, dict) or set(file) != {"path", "base_sha256", "content"}:
            raise UserError("Invalid file replacement object")
        path = validate_path(file["path"])
        if path not in originals or path in seen:
            raise UserError("Edit path is not authorized or is duplicated")
        seen.add(path)
        if file["base_sha256"] != originals[path]["sha256"]:
            raise UserError("Edit base hash does not match the frozen snapshot")
        content = file["content"]
        if not isinstance(content, str) or "\x00" in content:
            raise UserError("Only UTF-8 text replacements are allowed")
        try:
            size = len(content.encode("utf-8"))
        except UnicodeError as exc:
            raise UserError("Replacement contains invalid Unicode") from exc
        total += size
        if size > ledger.cfg.max_file_bytes or total > ledger.cfg.max_total_bytes:
            raise UserError("Edit exceeds byte limits")
        patches.append(make_diff(path, originals[path]["content"], content))
    # Consume once, transactionally, BEFORE any materialization. Failure is fail-closed.
    ledger.consume_grant(job["id"], sorted(seen), snap["snapshot_id"])
    parent = ledger.root / "artifacts"
    if parent.is_symlink():
        raise UserError("Artifacts directory must not be a symlink")
    parent.mkdir(mode=0o700, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=job["id"] + "-", dir=parent))
    for file in files:
        target = directory / "tree" / file["path"]
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(file["content"].encode("utf-8"))
        if os.name == "posix":
            target.chmod(0o600)
    patch = "".join(patches)
    (directory / "proposal.patch").write_bytes(patch.encode("utf-8"))
    (directory / "gemini-raw.txt").write_bytes(text.encode("utf-8"))
    metadata = {"job_id": job["id"], "snapshot_id": snap["snapshot_id"], "summary": data["summary"],
                "patch_sha256": sha(patch), "paths": sorted(seen),
                "base_hashes": {p: originals[p]["sha256"] for p in sorted(seen)},
                "main_workspace_modified": False, "staging_directory": str(directory)}
    (directory / "manifest.json").write_text(dumps(metadata) + "\n", encoding="utf-8")
    if os.name == "posix":
        for name in ("proposal.patch", "gemini-raw.txt", "manifest.json"):
            (directory / name).chmod(0o600)
    return metadata
