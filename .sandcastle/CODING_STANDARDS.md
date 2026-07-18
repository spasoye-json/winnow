# Coding Standards

Conventions an agent must follow when changing this repo. Read alongside `.sandcastle/PROJECT.md` and the docs it points to.

## Commits

- Atomic: one logical change per commit; if the message needs an "and", split it.
- Conventional Commits: `<type>(<scope>): <description>` (e.g. `fix(scoring): handle empty transcript`). Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`.
- Imperative mood, lowercase description, subject under 72 chars.
- Never add AI/Claude attribution or co-author trailers, anywhere: commits, PR descriptions, comments.

## Branching

- Branch off the base branch. Never merge the base branch into a feature branch; rebase onto it instead.
- Exception: the automated `agent-update-branch` conflict-resolution workflow merges the base branch into the PR branch, and only that flow may do so.

## Code

- Python, uv-managed. Follow the PRD's decisions; do not re-decide architecture inside a build ticket.
- No comments. Code must carry its own meaning through naming and structure. The single exception is a constraint the code cannot express (an external quirk, an ordering requirement); one short line then.
- No docstrings on internals; a public seam may carry a one-line docstring when its contract is not obvious from the signature.

## Prose

Applies to everything written on the maintainer's behalf: PR descriptions, issue and review comments, commit bodies.

- No em-dashes or en-dashes inside a sentence; use a period, comma, "and", or "or".
- No forward slash as an "or" separator; write the words out.

## Checks

- `uv run ruff check .` and `uv run pytest` from the repo root, green before every commit. See `.sandcastle/PROJECT.md`.

## Tests

- Follow each spec issue's Testing Decisions section; the specs pin the seam (HTTP through the framework test client against seeded SQLite) and forbid testing internals.
- Prefer red-green-refactor against existing test seams. Do not invent new seams (e.g. extracting a function solely to test it in isolation).
