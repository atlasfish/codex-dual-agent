#!/usr/bin/env python3
"""Create a NEW private personal GitHub repository using the user's own gh login.

No token input, no credential export, no existing-repository overwrite, no force push.
This script is a fallback for connectors that cannot create repositories.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PUBLISH_PATHS = ["dual_agent", "tests", "scripts", "docs", "skills", "hooks", ".codex-plugin", ".agents", ".github", "plugin.json", "mcp.json", ".mcp.json", "pyproject.toml", "config.example.toml", ".dual-agentignore.example", ".gitignore", "README.md", "LICENSE", "SECURITY.md", "CHANGELOG.md", "AGENTS.md", "TEST_REPORT.md"]


def run(*args: str, capture=False, check=True):
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=capture, check=check)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="OWNER/NAME, e.g. atlasfish/codex-dual-agent")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo must be OWNER/NAME")
    owner, _ = args.repo.split("/", 1)
    if args.dry_run:
        print(json.dumps({"repository": args.repo, "visibility": "private", "source": str(ROOT), "tests_before_push": True,
                          "actions": ["verify gh account", "refuse an existing repository", "initialize local main", "stage only project source paths", "gh repo create --private --source . --remote origin --push"]}, indent=2))
        return 0
    if not shutil.which("gh") or not shutil.which("git"):
        parser.error("Install Git and GitHub CLI (gh), then run gh auth login on your own machine")
    run("gh", "auth", "status")
    profile = json.loads(run("gh", "api", "user", capture=True).stdout)
    if profile["login"].lower() != owner.lower():
        parser.error("Authenticated gh user does not match repository owner; refusing to publish to another account")
    exists = run("gh", "repo", "view", args.repo, "--json", "nameWithOwner", capture=True, check=False)
    if exists.returncode == 0:
        parser.error("Repository already exists. This script never overwrites an existing repository")
    # Source archives must not accidentally publish a parent checkout or unrelated remotes.
    top = run("git", "rev-parse", "--show-toplevel", capture=True, check=False)
    if top.returncode == 0 and Path(top.stdout.strip()).resolve() != ROOT:
        parser.error("Project is inside another Git checkout; extract to a separate folder first")
    if (ROOT / ".git").exists() and run("git", "remote", capture=True).stdout.strip():
        parser.error("Local checkout has a remote already; refusing to change it or push elsewhere")
    run(sys.executable, "-m", "pytest", "-q")
    if not (ROOT / ".git").exists():
        run("git", "init", "-b", "main")
    if run("git", "branch", "--show-current", capture=True).stdout.strip() != "main":
        parser.error("Publish only from the new project's main branch; no branch switching is automatic")
    tracked = run("git", "ls-files", capture=True).stdout.splitlines()
    allowed = set(PUBLISH_PATHS)
    if any(p not in allowed and p.split("/")[0] not in allowed for p in tracked):
        parser.error("Unrelated tracked files found; review the checkout before publishing")
    run("git", "add", "--", *PUBLISH_PATHS)
    forbidden = [p for p in run("git", "ls-files", capture=True).stdout.splitlines()
                 if Path(p).name in {".env", "config.toml"} or p.endswith((".key", ".pem", ".sqlite3", ".db"))]
    if forbidden:
        parser.error("Sensitive local files were staged; remove them before publishing")
    dirty = run("git", "diff", "--cached", "--quiet", check=False)
    if dirty.returncode != 0:
        run("git", "-c", f"user.name={profile['login']}", "-c", f"user.email={profile['id']}+{profile['login']}@users.noreply.github.com",
            "commit", "-m", "feat: implement Codex native dual-agent router v0.1.0")
    run("gh", "repo", "create", args.repo, "--private", "--description", "Codex Desktop GPT + Gemini second-opinion router", "--source", str(ROOT), "--remote", "origin", "--push")
    print("Created and pushed private repository: https://github.com/" + args.repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
