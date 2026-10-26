# Repository Guidelines

## Project Structure & Module Organization

This local API relay comparison tool uses Python 3.10+ and the standard library, with no application build step. `server.py` serves the loopback API; `fetch_prices.py` collects prices. `integrations/` contains contracts, providers, routing, pricing, configuration, backup/restore (`config_backup`), and CCS/Cindy exports. `中转站比价.html` holds the frontend markup, styles, and JavaScript. `tests/` contains backend, Node, and browser checks. `data/` stores generated snapshots; `logs/` stores runtime logs. Read `README.md` before changes.

## Build, Test, and Development Commands

Run commands from the repository root:

```powershell
$env:PYTHONUTF8 = "1"
python -X utf8 server.py --no-schedule  # Local development; default port 8765
python -X utf8 -m unittest discover -s tests -v  # Backend regression suite
node tests/test_stage_a_ui.js          # Pricing/UI regressions
node tests/test_site_framework_ui.js   # Site configuration regressions
python -X utf8 tests/check_page_syntax.py  # Inline JavaScript syntax
```

Use `启动服务.bat` for normal operation with scheduling. `python -X utf8 fetch_prices.py --only <site-id>` performs real collection and writes snapshots; use it deliberately.

## Deployment

Human manual: `DEPLOY.md` (Chinese). Machine-executable checklist for AI agents: `AGENT-DEPLOY.md`. Usage details: `USER-GUIDE.md`; development, tests and releases: `DEVELOP.md`.

Three targets: Windows local (double-click `启动服务.bat`), Docker Compose (recommended for Linux servers), systemd + source. All three share one state directory (`sites.json`, `settings.json`, `auth.json`, `data/`, `logs/`); moving that directory is the whole migration.

Hard constraints — do not violate these when deploying or editing deployment files:

1. Never publish the business port on `0.0.0.0`. It binds `127.0.0.1` behind a reverse proxy; going public requires `RELAY_ALLOWED_ORIGINS` and `RELAY_ALLOWED_HOSTS` to match the real domain and scheme.
2. Never skip `python server.py --init-auth`. On `server` mode the process now refuses to start (exit code 1) while auth is disabled; with auth enabled but no `auth.json` it also exits with code 1. Both make the container restart-loop.
3. Never set `RELAY_ALLOW_UNAUTHENTICATED_REMOTE=1` just to make the service boot. It disables both the startup guard and the non-loopback request guard; only valid when a reverse proxy in front already authenticates requests.
4. Never lower `stop_grace_period` / `TimeoutStopSec` below 45s — graceful shutdown needs that budget to finish writing state.
5. Never use `:latest` or `:dev-latest` in production. Pin a version tag or a digest.
6. Never edit `sites.json` inside a container; edit the host copy in the state directory.
7. Never commit credentials, `auth.json`, or a filled `sites.json`.

After any deployment change, run `python -X utf8 tests/smoke_deploy.py --mode process` (or `--mode docker` when Docker is available).

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions/variables, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Follow the frontend's two-space indentation and `camelCase` functions. Keep cross-module names and types consistent. Add concise Chinese comments for business rules and state transitions. Save files as UTF-8. No shared formatter configuration is present; preserve surrounding style.

## Testing Guidelines

Use `unittest`, synthetic responses, mocks, and temporary directories. Name Python files `test_*.py` and methods `test_*`. Add regressions for changed behavior, especially credential isolation, incomplete pagination, stale snapshots, and uncertain writes. UI changes should also pass `python -X utf8 tests/check_stage_d_browser.py` using installed Microsoft Edge. No numeric coverage threshold is configured. Report local fixtures and live-site verification separately.

## Commit & Pull Request Guidelines

History contains Chinese commit subjects only. Use concise Chinese commit subjects as well. Commit day-to-day work to `dev`; `main` only receives fast-forward merges from `dev` at release time, and formal images are built only from published GitHub Releases. On `dev`, bump `VERSION` with `python bump_version.py --dev` before committing; run `--release` when preparing a release. Verify the destination before pushing. PRs should describe behavior, relevant issues, checks performed, and screenshots for UI changes. Preserve unrelated working-tree edits.

## Security & Agent Instructions

Keep the server on `127.0.0.1`. Never commit credentials, `sites.json`, `settings.json`, their backups, snapshots, or logs; use `sites.example.json` for examples. Preserve snapshot-only page loading, throttled collection, and no write replay after uncertain outcomes. Keep design notes and personal working context out of version control; they belong to a local, unpublished `docs/` and `memory/` directory.
