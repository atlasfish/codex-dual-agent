# Contributor instructions

Python 3.11+, SQLite, httpx and PyYAML. v0.2 intentionally permits Gemini shell,
local skills, HTTP/REST and live search. Advisor is a SOFT role, not read-only
isolation. Do not reintroduce command blacklists or advertise sandbox guarantees.
No dedicated direct file-edit tool is exposed by default. Patch proposals retain
baseline validation, but shell can write as the user's account.

Preserve native GPT execution, peer provenance and independent-opinion ordering.
Never automatically replay shell/API side effects or silently downgrade tool calls.
Keep resource budgets, cancel handling, original tool call ids and vendor signature
fields. Responses reasoning items must be replayed with tool results.

Do not persist raw tool output/commands, credentials or HTTP error bodies. Audit
metadata only. Skill instructions do not confer missing host MCP or OAuth access.
Tests must use fake/local services and temporary files, not real keys/servers.
Run python -m pytest -q and python -m compileall -q dual_agent scripts.
Do not claim Desktop, Windows, public search or a real relay was tested unless it was.
