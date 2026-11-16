"""Strict leading-prefix parser; never interprets code blocks or quoted mentions."""
from __future__ import annotations
from dataclasses import dataclass
import re
import shlex
from .config import UserError

PREFIX = re.compile(r"^@(both|gpt|gemini:edit|gemini|compare)(?=\s|$)")


@dataclass(frozen=True)
class Route:
    mode: str
    prompt: str
    files: tuple[str, ...] = ()
    explicit: bool = False


def parse_prompt(text: str) -> Route:
    if not isinstance(text, str) or len(text) > 200_000:
        raise UserError("Prompt must be text, up to 200,000 characters")
    stripped = text.lstrip()
    match = PREFIX.match(stripped)
    if not match:
        # A typo must not accidentally grant authority or trigger another model.
        if stripped.startswith(("@gemini", "@both", "@gpt", "@compare")):
            raise UserError("Unknown route. Use @gpt, @gemini, @both, @compare, or @gemini:edit")
        return Route("gpt", text)
    mode = match.group(1)
    body = stripped[match.end():].lstrip()
    files: tuple[str, ...] = ()
    if body.startswith("--files"):
        split = re.search(r"\s--\s", body)
        if not split:
            raise UserError('Use --files "src/a.ts" "src/b.ts" -- your instruction')
        try:
            args = shlex.split(body[:split.start()])
        except ValueError as exc:
            raise UserError("Invalid --files quoting") from exc
        if not args or args[0] != "--files" or len(args) < 2:
            raise UserError("--files needs one or more exact paths")
        files = tuple(dict.fromkeys(args[1:]))
        if any(p.startswith("--") for p in files):
            raise UserError("Only the --files option is supported")
        body = body[split.end():].strip()
    if mode == "gemini:edit" and not files:
        raise UserError('Editing needs exact paths: @gemini:edit --files "src/a.ts" -- instruction')
    if not body and mode != "compare":
        raise UserError("Provide a question or instruction after the prefix")
    return Route(mode, body or "Compare the most recent GPT and Gemini opinions; retain disagreements.", files, True)
