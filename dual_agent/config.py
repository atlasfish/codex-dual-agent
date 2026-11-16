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
    timeout_seconds: float = 300.0
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

    # Trusted local Agent mode. These switches remove tools, NOT OS capabilities.
    tools_enabled: bool = True
    shell_enabled: bool = True
    network_enabled: bool = True
    skills_enabled: bool = True
    skill_roots: tuple[str, ...] = ()
    shell_command: tuple[str, ...] = ()  # Auto: /bin/sh -c or PowerShell -Command
    shell_timeout_seconds: float = 60.0
    max_tool_rounds: int = 12
    max_tool_calls: int = 32
    max_tool_output_bytes: int = 64_000
    max_agent_context_bytes: int = 1_000_000
    search_backend: str = "duckduckgo"  # best-effort HTML, searxng, or command
    search_url: str = ""
    search_command: tuple[str, ...] = ()  # argv; {query} replaced without a shell

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
        for key in ("skill_roots", "shell_command", "search_command"):
            if key in data:
                if not isinstance(data[key], list) or not all(isinstance(v, str) and v for v in data[key]):
                    raise UserError(f"{key} must be an array of nonempty strings")
                data[key] = tuple(data[key])
        cfg = cls(**data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        for name in ("base_url", "model", "protocol", "api_key_env", "api_key_file", "search_backend", "search_url"):
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
            ("shell_timeout_seconds", 1, 600), ("max_tool_rounds", 1, 64),
            ("max_tool_calls", 1, 256), ("max_tool_output_bytes", 1024, 1_000_000),
            ("max_agent_context_bytes", 4096, 8_000_000),
        ):
            value = getattr(self, name)
            if name not in {"timeout_seconds", "shell_timeout_seconds"} and (not isinstance(value, int) or isinstance(value, bool)):
                raise UserError(f"{name} must be an integer")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise UserError(f"Invalid {name}; expected {low}..{high}")
        for name in ("auto_changed_files", "include_git_diff", "allow_remote_relay", "tools_enabled", "shell_enabled", "network_enabled", "skills_enabled"):
            if not isinstance(getattr(self, name), bool):
                raise UserError(f"{name} must be a boolean")
        if not all(isinstance(root, str) and root for root in self.allowed_roots):
            raise UserError("allowed_roots must contain filesystem paths")

        for key in ("skill_roots", "shell_command", "search_command"):
            value = getattr(self, key)
            if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) and v for v in value):
                raise UserError(f"{key} must be an array of nonempty strings")
        if self.search_backend not in {"duckduckgo", "searxng", "command"}:
            raise UserError("search_backend must be duckduckgo, searxng, or command")
        if self.search_backend == "searxng":
            url = urlparse(self.search_url)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise UserError("SearXNG search_url must be an HTTP(S) URL without credentials")
        if self.search_backend == "command" and not self.search_command:
            raise UserError("command search requires search_command argv")

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
