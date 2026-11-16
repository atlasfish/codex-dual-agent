"""Trusted-user tools. Deliberately NOT a read-only sandbox or network firewall."""
from __future__ import annotations
import asyncio
import codecs
from contextlib import suppress
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import signal
import time
from urllib.parse import parse_qs, urljoin, urlparse
import httpx
from .config import Config, UserError
from .skills import Skills


def spec(name: str, description: str, properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": list(required), "additionalProperties": False}}


TEXT = {"type": "string"}
SPECS = [
    spec("shell", "Execute a local shell command (SSH, curl, scripts, code search). NOT sandboxed; avoid unsolicited source edits and remote mutations. Use existing credentials without printing them.",
         {"command": TEXT, "cwd": TEXT}, ("command",)),
    spec("read_file", "Read a UTF-8 file, relative to the workspace or absolute. For code browsing; do not request credentials.",
         {"path": TEXT, "offset": {"type": "integer", "minimum": 0}}, ("path",)),
    spec("list_skills", "Discover installed local SKILL.md metadata, including configured extra roots.", {}),
    spec("read_skill", "Load a discovered skill. Follow its workflow and run scripts with shell; host-only connectors are not implicitly available.",
         {"skill_id": TEXT}, ("skill_id",)),
    spec("http_request", "Call HTTP(S), local servers or REST APIs. Never output credentials. ${ENV_NAME} in header values is resolved locally. Mutations require the user's task to authorize them.",
         {"url": TEXT, "method": TEXT, "headers": {"type": "object", "additionalProperties": {"type": "string"}}, "body": TEXT}, ("url",)),
    spec("web_search", "Search the live web; return source URLs. No results or an error is not proof of absence. Do not send private code or secrets as queries.",
         {"query": TEXT}, ("query",)),
]


class SearchHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self.active: dict | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "result__a" in attrs.get("class", "").split():
            link = urljoin("https://duckduckgo.com", attrs.get("href", ""))
            link = parse_qs(urlparse(link).query).get("uddg", [link])[0]
            if urlparse(link).scheme in {"http", "https"}:
                self.active = {"title": "", "url": link}

    def handle_data(self, data):
        if self.active is not None:
            self.active["title"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.active is not None:
            self.results.append(self.active)
            self.active = None


class ToolRuntime:
    def __init__(self, cfg: Config, workspace: Path, audit=None, transport=None):
        self.cfg = cfg
        self.workspace = cfg.check_workspace(workspace)
        self.skills = Skills(cfg, self.workspace)
        self.audit = audit
        self.transport = transport
        self.calls = 0
        self.names = {s["name"] for s in SPECS}
        if not cfg.shell_enabled:
            self.names.discard("shell")
        if not cfg.network_enabled:
            self.names -= {"http_request", "web_search"}
        if not cfg.skills_enabled:
            self.names -= {"list_skills", "read_skill"}
        self.secrets = [v for k, v in os.environ.items() if len(v) >= 8 and re.search(r"TOKEN|SECRET|PASSWORD|API_?KEY", k, re.I)]
        key = cfg.api_key()
        if key and len(key) >= 8:
            self.secrets.append(key)

    def definitions(self, protocol: str) -> list[dict]:
        items = [s for s in SPECS if s["name"] in self.names]
        if protocol == "responses":
            return [{"type": "function", **s, "strict": False} for s in items]
        return [{"type": "function", "function": s} for s in items]

    def description(self) -> str:
        return json.dumps({"workspace": str(self.workspace), "platform": os.name,
                           "shell": list(self.cfg.shell_command) or (["powershell.exe", "-Command"] if os.name == "nt" else ["/bin/sh", "-c"]),
                           "tools": sorted(self.names), "available_skills": self.skills.list()}, ensure_ascii=False)

    def redact(self, text: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    async def call(self, name: str, arguments: str) -> str:
        if self.calls >= self.cfg.max_tool_calls:
            raise UserError("Agent tool-call budget exhausted; start a narrower task")
        self.calls += 1
        start = time.monotonic()
        result: dict = {}
        try:
            if name not in self.names:
                raise UserError("Tool is unavailable; use only advertised tools")
            args = json.loads(arguments)
            schema = next(s for s in SPECS if s["name"] == name)["parameters"]
            if not isinstance(args, dict) or set(args) - schema["properties"].keys() or set(schema["required"]) - args.keys():
                raise UserError("Invalid tool arguments")
            for key, value in args.items():
                kind = schema["properties"][key]["type"]
                if (kind == "string" and not isinstance(value, str)) or (kind == "integer" and (type(value) is not int or value < 0)):
                    raise UserError("Invalid argument type")
                if kind == "object" and (not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())):
                    raise UserError("Invalid headers")
            if name == "shell":
                result = await self.shell(**args)
            elif name == "read_file":
                result = self.read_file(**args)
            elif name == "list_skills":
                result = self.skills.list()
            elif name == "read_skill":
                result = self.skills.read(**args)
            elif name == "http_request":
                result = await self.http_request(**args)
            elif name == "web_search":
                result = await self.web_search(**args)
        except asyncio.CancelledError:
            if self.audit:
                self.audit(name[:120], hashlib.sha256(arguments.encode()).hexdigest(), time.monotonic() - start, "cancelled", 0)
            raise
        except (UserError, OSError, UnicodeError, ValueError, TypeError, httpx.HTTPError) as exc:
            result = {"error": str(exc) if isinstance(exc, UserError) else "Tool failed (I/O, arguments, encoding or network); no credentials included"}
        # Only this bounded result is sent to the model. Full outputs are not persisted.
        text = self.redact(json.dumps(result, ensure_ascii=False))
        limit = self.cfg.max_tool_output_bytes
        if len(text.encode()) > limit:
            # Trim the large field while keeping metadata and JSON intact.
            for key in ("output", "body", "content"):
                if key in result and isinstance(result[key], str):
                    original = self.redact(result[key])
                    low, high = 0, len(original)
                    result["truncated"] = True
                    while low < high:
                        mid = (low + high + 1) // 2
                        result[key] = original[:mid]
                        if len(json.dumps(result, ensure_ascii=False).encode()) <= limit:
                            low = mid
                        else:
                            high = mid - 1
                    result[key] = original[:low]
                    text = self.redact(json.dumps(result, ensure_ascii=False))
                    break
        if len(text.encode()) > limit:
            # Preserve a valid JSON wrapper instead of cutting through JSON syntax.
            snippet = text.encode()[:max(0, (limit - 256) // 6)].decode("utf-8", errors="ignore")
            text = json.dumps({"truncated": True, "partial_result": snippet}, ensure_ascii=False)
        if self.audit:
            self.audit(name[:120], hashlib.sha256(arguments.encode()).hexdigest(), time.monotonic() - start,
                       "error" if "error" in result else "ok", len(text.encode()))
        return text

    def _path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.workspace / path).resolve()

    def read_file(self, path: str, offset: int = 0) -> dict:
        target = self._path(path)
        if not target.is_file():
            raise UserError("Not a regular file")
        # Convenience guard, not isolation: shell can still read any user-readable file.
        if target.name.startswith(".env") or target.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".keystore"}:
            raise UserError("Potential credential file; use existing authentication without returning its contents")
        with target.open("rb") as stream:
            stream.seek(offset)
            chunk_limit = max(1, (self.cfg.max_tool_output_bytes - 512) // 6)
            data = stream.read(chunk_limit + 1)
        more = len(data) > chunk_limit
        raw = data[:chunk_limit]
        if b"\x00" in raw:
            raise UserError("Binary file; use a suitable local script")
        decoder = codecs.getincrementaldecoder("utf-8")()
        text = decoder.decode(raw, final=not more)
        pending = decoder.getstate()[0]
        consumed = len(raw) - len(pending)
        return {"path": str(target), "offset": offset, "next_offset": offset + consumed,
                "truncated": more, "content": text,
                "chunk_sha256": hashlib.sha256(raw[:consumed]).hexdigest()}

    async def _execute(self, argv: list[str], cwd: Path) -> dict:
        options = {"start_new_session": True} if os.name == "posix" else {}
        proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **options)
        output = bytearray()
        truncated = False
        async def drain():
            nonlocal truncated
            while chunk := await proc.stdout.read(8192):
                available = max(0, self.cfg.max_tool_output_bytes - len(output))
                output.extend(chunk[:available])
                truncated |= len(chunk) > available
        reader = asyncio.create_task(drain())
        timed_out = False
        async def terminate():
            if os.name == "posix":
                with suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            elif proc.returncode is None:
                with suppress(OSError):
                    killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(proc.pid), "/T", "/F",
                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    await asyncio.wait_for(killer.wait(), 5)
                if proc.returncode is None:
                    with suppress(ProcessLookupError):
                        proc.kill()
            await proc.wait()
        try:
            async with asyncio.timeout(self.cfg.shell_timeout_seconds):
                await proc.wait()
                await reader
        except TimeoutError:
            timed_out = True
            await terminate()
        except asyncio.CancelledError:
            await terminate()
            raise
        finally:
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        return {"exit_code": proc.returncode, "timed_out": timed_out, "truncated": truncated,
                "output": bytes(output).decode("utf-8", errors="replace")}

    async def shell(self, command: str, cwd: str = "") -> dict:
        directory = self._path(cwd) if cwd else self.workspace
        if not directory.is_dir():
            raise UserError("Shell cwd does not exist")
        prefix = list(self.cfg.shell_command) or (["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"] if os.name == "nt" else ["/bin/sh", "-c"])
        return await self._execute(prefix + [command], directory)

    async def http_request(self, url: str, method: str = "GET", headers: dict | None = None, body: str = "") -> dict:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise UserError("Use an HTTP(S) URL without inline credentials")
        method = method.upper()
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
            raise UserError("Unsupported HTTP method")
        def expand(match):
            value = os.environ.get(match.group(1))
            if value is None:
                raise UserError("Required authentication environment variable is missing")
            if len(value) >= 8:
                self.secrets.append(value)
            return value
        resolved = {k: re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", expand, v) for k, v in (headers or {}).items()}
        if len(body.encode()) > self.cfg.max_tool_output_bytes:
            raise UserError("HTTP request body exceeds limit; use a local script for larger payloads")
        # No redirect following: credentials must not leak to a redirected origin.
        async with httpx.AsyncClient(transport=self.transport, trust_env=True, follow_redirects=False,
                                     timeout=self.cfg.shell_timeout_seconds) as client:
            async with client.stream(method, url, headers=resolved, content=body.encode() if body else None) as response:
                data = bytearray()
                truncated = False
                async for chunk in response.aiter_bytes():
                    available = max(0, self.cfg.max_tool_output_bytes - len(data))
                    data.extend(chunk[:available])
                    if len(chunk) > available:
                        truncated = True
                        break
                return {"url": str(response.url), "status": response.status_code,
                        "content_type": response.headers.get("content-type", ""), "truncated": truncated,
                        "body": bytes(data).decode("utf-8", errors="replace")}

    async def web_search(self, query: str) -> dict:
        if not query.strip() or len(query) > 2000:
            raise UserError("Search query must be 1..2000 characters")
        cfg = self.cfg
        if cfg.search_backend == "command":
            argv = [item.replace("{query}", query) for item in cfg.search_command]
            if not any("{query}" in item for item in cfg.search_command):
                argv.append(query)
            result = await self._execute(argv, self.workspace)
            if result["exit_code"] != 0 or result["timed_out"] or result["truncated"]:
                raise UserError("Search script failed/timed out/exceeded output limit")
            data = json.loads(result["output"])
            rows = data.get("results", []) if isinstance(data, dict) else data
        elif cfg.search_backend == "searxng":
            url = str(httpx.URL(cfg.search_url).copy_merge_params({"q": query, "format": "json"}))
            result = await self.http_request(url)
            if result["status"] != 200 or result["truncated"]:
                raise UserError("Search endpoint failed or exceeded output limit")
            rows = json.loads(result["body"]).get("results", [])
        else:
            url = str(httpx.URL("https://html.duckduckgo.com/html/").copy_merge_params({"q": query}))
            result = await self.http_request(url)
            if result["status"] != 200:
                raise UserError("Default web search unavailable; configure SearXNG/search_command or use a search skill")
            parser = SearchHTML()
            parser.feed(result["body"])
            rows = parser.results
            if not rows:
                raise UserError("No parseable search results (possibly rate limit/CAPTCHA); use a configured search backend or skill. Do not invent results")
        if not isinstance(rows, list):
            raise UserError("Search backend must return a list or {results: [...]} object")
        results = [{"title": str(r.get("title", ""))[:500], "url": r["url"],
                    "snippet": str(r.get("snippet", r.get("content", "")))[:1000]}
                   for r in rows if isinstance(r, dict) and isinstance(r.get("url"), str)
                   and urlparse(r["url"]).scheme in {"http", "https"}][:8]
        return {"query": query, "results": results, "source": cfg.search_backend}
