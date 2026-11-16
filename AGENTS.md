# Contributor instructions

Use Python 3.11+, stdlib SQLite and httpx. Keep Gemini unable to execute tools.
Do not add a persistent write-mode switch, automatic retries, automatic patch
application, or transcript scraping. Preserve host/peer message provenance.

Before proposing changes, run `python -m pytest -q` and
`python -m compileall -q dual_agent scripts`. Tests use fake/local HTTP endpoints;
never call a real model or require a real key in CI. Do not log credentials,
HTTP error bodies, the private ledger, or user files.

Test changes to Hooks with real subprocesses as well as unit tests. Bound Stop
continuations and prevent duplicate model billing under hook redelivery. Keep
snapshot and grant checks in code, not only in SKILL.md. Preserve unrelated local
marketplace entries. Never claim Desktop or relay integration was tested without
actually running that target environment.

Generated staging patches are untrusted. Main workspace writes require a separate
explicit native GPT request, base-hash verification, review and normal approvals.
