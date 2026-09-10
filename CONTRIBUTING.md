# Contributing

## Setup

```bash
pip install -r requirements-dev.txt && playwright install chromium
python3 -m pytest -q                    # 112 tests, ~50 s
python3 scripts/gate_ci.py              # the scripted day + safety gate
ruff check src tests eval scripts demo
```

Run the app against a scratch data directory: `MUNSHI_DATA_DIR=/tmp/m PYTHONPATH=src python3 -m munshi.cli serve --port 8765`.

## Where things go

- A new **business rule** → `domain/repository/<context>.py`, with a test in `tests/test_money.py` or `tests/test_repository.py`.
- A new **tool** → `tools/core.py` (framework-free) + a wrapper in `tools/langchain_tools.py` + a tier in `safety/risk.py` (the registry test fails until you do) + the role lists in `agents/specialists.py` + a stub rule so it works offline + a step in `eval/scenario.py`. If it writes stock or money, add it to `ALWAYS_GATED` in `eval/run_eval.py` and `ACTION_TOOL`.
- A new **screen** → `web/static/views.js` (register on `V`), a route in `web/routes/`, strings in `i18n.js` (both languages), a step in `scripts/screenshots.py`.
- A new **setting** → `DEFAULT_SETTINGS` in `domain/repository/base.py` and `SettingsIn` in `web/routes/setup.py`.
- A **schema change** → a new entry in `domain/migrations.py`; never edit an applied one.

## Rules

- Nothing writes SQL outside `domain/repository/` (seed and the opening-balance import are the documented exceptions).
- Every state-changing tool is gated; every gated write is audited with actor and approver.
- Every route declares a permission. Field roles get redacted payloads, not hidden buttons.
- No free text to customers. Templates only.
- Tests assert on database state, not on what the model said.
- Commits: one topic each; CI must be green; add a CHANGELOG line for anything a user would notice.
