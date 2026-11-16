"""User-owned configuration. Project files never select endpoints or credentials."""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from dataclasses import dataclass, field
import stat
import tomllib
from urllib.parse import urlparse


class UserError(ValueError):
    """Safe, actionable error; must not contain credentials or relay responses."""


@dataclass(frozen=True)
class Config:
    state_dir: Path = field(default_factory=lambda: Path.home() / ".local/share/codex-dual-agent/state")
    base_url: str = "http://127.0.0.1:8045/v1"
    model: str = "REPLACE_WITH_YOUR_GEMINI_MODEL"
    protocol: str = "chat_completions"
    api_key_env: str = "DUAL_AGENT_API_KEY"
    api_key_file: str = ""
    timeout_seconds: float = 120.0
    max_output_tokens: int = 8192
    max_response_bytes: int = 2_000_000
    max_parallel_jobs: int = 4
    max_files: int = 24
    max_file_bytes: int = 64_000
    max_total_bytes: int = 240_000
    history_chars: int = 48_000
    auto_changed_files: bool = False
    include_git_diff: bool = False
    allowed_roots: tuple[str, ...] = ()
    allow_remote_relay: bool = False

    @classmethod
    def load(cls) -> "Config":
        path = Path(os.environ.get("DUAL_AGENT_CONFIG", "~/.config/codex-dual-agent/config.toml")).expanduser()
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            raise UserError("Unknown configuration keys: " + ", ".join(sorted(unknown)))
        if "state_dir" in data and not isinstance(data["state_dir"], str):
            raise UserError("state_dir must be a path string")
        if "allowed_roots" in data and not isinstance(data["allowed_roots"], list):
            raise UserError("allowed_roots must be an array of paths")
        if "state_dir" in data:
            data["state_dir"] = Path(data["state_dir"]).expanduser().resolve()
        if "allowed_roots" in data:
            data["allowed_roots"] = tuple(data["allowed_roots"])
        cfg = cls(**data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        for name in ("base_url", "model", "protocol", "api_key_env", "api_key_file"):
            if not isinstance(getattr(self, name), str):
                raise UserError(f"{name} must be a string")
        if not isinstance(self.allowed_roots, (tuple, list)):
            raise UserError("allowed_roots must be an array of paths")
        if not isinstance(self.state_dir, Path):
            raise UserError("state_dir must be a filesystem path")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise UserError("base_url must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise UserError("Do not put credentials, queries, or fragments in base_url")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if not loopback and (not self.allow_remote_relay or parsed.scheme != "https"):
            raise UserError("Remote relays require HTTPS and allow_remote_relay = true")
        if self.protocol not in {"chat_completions", "responses"}:
            raise UserError("protocol must be chat_completions or responses")
        for name, low, high in (
            ("timeout_seconds", 1, 600), ("max_output_tokens", 1, 65536),
            ("max_response_bytes", 1024, 8_000_000), ("max_parallel_jobs", 1, 16),
            ("max_files", 1, 128), ("max_file_bytes", 1, 1_000_000),
            ("max_total_bytes", 1, 4_000_000), ("history_chars", 1, 400_000),
        ):
            value = getattr(self, name)
            if name != "timeout_seconds" and (not isinstance(value, int) or isinstance(value, bool)):
                raise UserError(f"{name} must be an integer")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise UserError(f"Invalid {name}; expected {low}..{high}")
        for name in ("auto_changed_files", "include_git_diff", "allow_remote_relay"):
            if not isinstance(getattr(self, name), bool):
                raise UserError(f"{name} must be a boolean")
        if not all(isinstance(root, str) and root for root in self.allowed_roots):
            raise UserError("allowed_roots must contain filesystem paths")

    def check_workspace(self, workspace: Path) -> Path:
        root = workspace.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise UserError("Workspace must be a directory")
        if not self.allowed_roots:
            raise UserError("Set allowed_roots in the user config before sharing project context")
        allowed = [Path(r).expanduser().resolve() for r in self.allowed_roots]
        if not any(root == r or root.is_relative_to(r) for r in allowed):
            raise UserError("Workspace is not covered by allowed_roots")
        return root

    def api_key(self) -> str:
        value = os.environ.get(self.api_key_env, "")
        if not value and self.api_key_file:
            path = Path(self.api_key_file).expanduser()
            if not path.is_absolute() or path.is_symlink():
                raise UserError("api_key_file must be an absolute, non-symlink user-owned file")
            try:
                info = path.stat()
                if os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
                    raise UserError("api_key_file must be owned by you with mode 0600")
                if info.st_size > 8192:
                    raise UserError("api_key_file is too large")
                value = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise UserError("Cannot read api_key_file") from exc
        if "\n" in value or "\r" in value:
            raise UserError("Invalid API key")
        return value  # Empty is allowed for a local relay with no authentication.
