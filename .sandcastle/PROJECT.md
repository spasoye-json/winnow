# Project map

Winnow is a personal YouTube quality curator: a single-user, local-first Python app. FastAPI serves server-rendered Jinja2 templates (vendored Pico.css and htmx, no Node build step for the app), SQLite holds all state, and an LLM scores every ingested video against a fixed six-dimension rubric (default model gemini-3.1-flash-lite on the free tier).

## Layout

- The Python package does not exist yet. Issue #23 (Project scaffold and full DB schema) creates it; until that lands the repo is documentation only.
- `docs/prd-youtube-curator.md` — the PRD. Every architecture, data model, rubric, pacing, and milestone decision lives here. Read it before any build ticket.
- `docs/agents/` — issue tracker conventions and the triage label contract.
- `docs/design/Winnow.html` — interactive UI mock of all four screens; the visual reference for the Jinja2 templates.
- `.sandcastle/` and `.github/workflows/agent-*.yml` — this agent-runner machinery (TypeScript). Not app code.
- Build tickets are GitHub issues #23 to #39, chained with native issue dependencies in milestone order behind specs #19 to #22.

## Checks (feedback loops)

The app is a uv-managed Python project. Run before finishing:

- `uv run ruff check .`
- `uv run pytest`

These two commands are the objective gate (`.sandcastle/sandcastle.config.ts`). Issue #23 must establish exactly this contract: a uv-managed project where both commands pass on a fresh checkout. Until #23 lands they fail because no `pyproject.toml` exists; every other build ticket is blocked behind #23 by issue dependencies, so no agent should hit that state.

The root `package.json` holds only the sandcastle tooling. `npm ci && npm run typecheck` verifies that tooling, not the app.

## Known pre-existing failures

None.
