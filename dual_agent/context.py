"""Optional bounded evidence: POSIX hardened reads, portable best-effort fallback."""
from __future__ import annotations
import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile
from .config import Config, UserError

DENIED_PARTS = {".git", ".ssh", ".aws", ".azure", ".gnupg", ".venv", "node_modules", ".gradle", ".idea", ".codex", ".agents", "__pycache__"}
DENIED_NAMES = (".env*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "id_rsa*", "id_ed25519*", "*credential*", "*secret*", "local.properties", "gradle.properties", "*.sqlite*", "*.db", ".npmrc", ".pypirc", ".netrc")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_path(name: str, patterns: tuple[str, ...] = ()) -> str:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or any(ord(c) < 32 for c in name):
        raise UserError("Invalid file path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in name.split("/")):
        raise UserError("Only workspace-relative paths without traversal are allowed")
    if any(p.lower() in DENIED_PARTS for p in path.parts):
        raise UserError("Blocked sensitive or generated directory")
    if any(fnmatch.fnmatchcase(p.lower(), pattern) for p in path.parts for pattern in DENIED_NAMES):
        raise UserError("Blocked sensitive filename")
    if any(fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(path.name, p) for p in patterns):
        raise UserError("Blocked by .dual-agentignore")
    return name


def safe_read(root: Path, name: str, max_bytes: int) -> tuple[str, str]:
    validate_path(name)
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        return portable_read(root, name, max_bytes)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = name.split("/")
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise UserError("Only regular, non-hardlinked files are allowed")
            if before.st_size > max_bytes:
                raise UserError("File exceeds snapshot size limit")
            with os.fdopen(file_fd, "rb", closefd=False) as stream:
                raw = stream.read(max_bytes + 1)
            after = os.fstat(file_fd)
            if len(raw) > max_bytes or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise UserError("File changed while being snapshotted, or exceeds limit")
            if b"\x00" in raw:
                raise UserError("Binary files are not supported")
            return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()
        finally:
            os.close(file_fd)
    except (OSError, UnicodeError) as exc:
        raise UserError("Cannot safely read file (missing, symlink, encoding, or permission)") from exc
    finally:
        os.close(fd)



def portable_read(root: Path, name: str, max_bytes: int) -> tuple[str, str]:
    """Best-effort Windows evidence read, NOT an adversarial race-proof sandbox."""
    validate_path(name)
    target = root
    for component in name.split("/"):
        target = target / component
        info = target.lstat()
        if target.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise UserError("Snapshot does not follow symlinks/junctions")
    if not target.resolve().is_relative_to(root.resolve()):
        raise UserError("Snapshot path escapes workspace")
    with target.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > max_bytes:
            raise UserError("Snapshot requires a bounded regular non-hardlinked file")
        data = stream.read(max_bytes + 1)
        after = os.fstat(stream.fileno())
    if len(data) > max_bytes or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise UserError("File changed or exceeds snapshot limit")
    if b"\x00" in data:
        raise UserError("Binary files are not supported")
    return data.decode("utf-8"), hashlib.sha256(data).hexdigest()

def git(root: Path, *args: str, max_bytes: int = 1_000_000) -> bytes:
    try:
        # Spool stdout instead of buffering unbounded Git output in memory.
        # Never execute a shell, external diff driver, textconv, or fsmonitor.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                ["git", "--no-pager", "--literal-pathspecs", "-c", "core.fsmonitor=false", "-C", str(root), *args],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                timeout=5, check=False,
            )
            if result.returncode != 0 or output.tell() > max_bytes:
                return b""
            output.seek(0)
            return output.read(max_bytes)
    except (OSError, subprocess.TimeoutExpired):
        return b""


def snapshot(root: Path, files: tuple[str, ...], cfg: Config) -> dict:
    patterns: tuple[str, ...] = ()
    ignore = root / ".dual-agentignore"
    if ignore.exists() or ignore.is_symlink():
        text, _ = safe_read(root, ".dual-agentignore", 16_000)
        patterns = tuple(line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    names = list(files)
    explicit = bool(files)
    warnings: list[str] = []
    if not names and cfg.auto_changed_files:
        raw_names = git(root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD", "--")
        names = [n.decode("utf-8") for n in raw_names.split(b"\x00") if n]
    if len(names) > cfg.max_files:
        if explicit:
            raise UserError("Too many requested snapshot files")
        names = names[:cfg.max_files]
        warnings.append("Changed-file list truncated to max_files")
    entries: list[dict] = []
    total = 0
    for name in dict.fromkeys(names):
        try:
            validate_path(name, patterns)
            content, digest = safe_read(root, name, cfg.max_file_bytes)
            total += len(content.encode("utf-8"))
            if total > cfg.max_total_bytes:
                raise UserError("Snapshot exceeds total size limit")
            entries.append({"path": name, "sha256": digest, "content": content})
        except UserError:
            if explicit:
                raise
            warnings.append("A changed file was omitted by safety or size policy")
    # Verify selected files once more. Stored bytes stay immutable afterwards.
    for entry in entries:
        if safe_read(root, entry["path"], cfg.max_file_bytes)[1] != entry["sha256"]:
            raise UserError("Workspace changed during snapshot; resubmit the request")
    diff = ""
    head = ""
    if cfg.include_git_diff and entries:
        head = git(root, "rev-parse", "--verify", "HEAD", max_bytes=128).decode("ascii", errors="ignore").strip()
        prefix = git(root, "rev-parse", "--show-prefix", max_bytes=4096).decode("utf-8", errors="replace").strip()
        budget = max(0, cfg.max_total_bytes - total)
        pieces: list[str] = []
        if head:
            for entry in entries:
                object_name = head + ":" + prefix + entry["path"]
                size = git(root, "cat-file", "-s", object_name, max_bytes=128).strip()
                if not size.isdigit() or int(size) > cfg.max_file_bytes:
                    warnings.append("Unavailable or oversized Git base omitted from evidence diff")
                    continue
                raw = git(root, "cat-file", "blob", object_name, max_bytes=cfg.max_file_bytes)
                if len(raw) != int(size) or b"\x00" in raw:
                    warnings.append("Unreadable or binary Git base omitted from evidence diff")
                    continue
                try:
                    before = raw.decode("utf-8")
                except UnicodeError:
                    warnings.append("Non-UTF8 Git base omitted from evidence diff")
                    continue
                # Compare the pinned Git base with frozen snapshot bytes, never a
                # live working-tree reread. This is evidence, not an apply patch.
                pieces.extend(difflib.unified_diff(before.splitlines(keepends=True),
                    entry["content"].splitlines(keepends=True), fromfile="HEAD/" + entry["path"],
                    tofile="snapshot/" + entry["path"]))
            raw_diff = "".join(pieces).encode("utf-8")
            diff = raw_diff[:budget].decode("utf-8", errors="ignore")
            if len(raw_diff) > budget:
                warnings.append("Evidence diff truncated to remaining byte budget")
        else:
            warnings.append("Git HEAD unavailable; evidence diff omitted")
    value = {"files": entries, "git_diff": diff, "git_head": head,
             "warnings": warnings, "requested_files": list(files)}
    value["snapshot_id"] = sha(json.dumps(value, sort_keys=True, ensure_ascii=False))
    return value
