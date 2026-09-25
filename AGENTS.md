# AGENTS.md

Instructions for coding agents working in this repository. Humans start at
[README.md](README.md); both read the same canonical docs linked below. Where
general industry practice and this file disagree, this file wins.

## Setup and checks

```bash
uv sync --extra test --extra dev --extra db --extra crypto-live
git config core.hooksPath .githooks          # pre-commit runs ruff
uv run pytest -q -m "not sdk_contract" tests/  # what core CI runs
uv run ruff check . && uv run ruff format --check .
```

Run the checks before every commit. Tests stay offline: mock external
boundaries (brokers, DB, network) unless a test is explicitly marked
otherwise. More in [getting-started](docs/getting-started.md#contributing-to-this-repository).

## Read before changing code

- [architecture.md](architecture.md) — current design contract, including the
  **compatibility policy** (break freely before 1.0, no shims or aliases, but
  announce every break) and the **failure handling policy** (trading
  correctness fails fast; optional paths may degrade but must log).
- [docs/README.md](docs/README.md) — which doc owns what, what wins when docs
  disagree, and the writing conventions that also bind docstrings and comments.
- Language rules are in the header of [architecture.md](architecture.md).

## Change discipline

- Fix the class of problem, not the one instance, and keep the diff minimal.
  Extend an existing mechanism before adding a new field, flag or path.
- A behavior or structure change updates `architecture.md` and any affected
  guide in the same change.
- Comments state why; the code states what. No drift-prone counts or lists.
- Never commit secrets or local state: `.env*` (except `*.example`),
  `.secrets/`, `.credentials/`, `logs/`, `.claude/`.

## Commits

Follow [Conventional Commits](https://www.conventionalcommits.org/), as
`git log` shows:

```
type(scope)[!]: short imperative summary in lowercase

Prose body, wrapped near 72 columns. Say what was wrong or missing, why it
matters, what the change does, and what it deliberately leaves alone.

BREAKING CHANGE: the contract that changed and what the consumer does now.

Refs #123
```

- **Types**: `feat`, `fix`, `refactor`, `docs`, `test`. Scope is the
  area touched (`live`, `data`, `db`, `deploy`, `brokers`, ...).
- **Subject** describes the behavior, not the edit: "keep the database password
  off the docker command line", not "update trade.sh". No trailing period.
- **Body** is short prose paragraphs, not a changelog of files. Omit it only
  when the subject says everything.
- **Breaking**: `!` plus a `BREAKING CHANGE:` footer whenever previously
  accepted input now fails or a consumer gets something different back — see
  the compatibility policy.
- **Issue footer**: `Refs #N`, or `Closes #N` when the commit resolves it.
- **No AI attribution.** No `Co-Authored-By:` for an AI or a tool, no
  "Generated with" line, no `Signed-off-by:`. Commit under the repo's
  configured identity; if `git config user.email` is empty, ask instead of
  committing as the machine default.

## Branches and pull requests

- Open or reference an issue first for non-trivial changes. Branch as
  `<type>/<issue>-<slug>`, e.g. `fix/251-futures-selector-at-config-time`.
- PR title is the commit subject plus the issue: `fix(config)!: ... (#251)`.
- PR body: `Closes #N`, then a summary, anything deliberately not covered, and
  the test plan actually run (with results).
- PRs are squash-merged. The squash message must read as one commit under the
  rules above: rewrite it at merge, drop every auto-added `Co-authored-by`,
  and confirm the `!`/`BREAKING CHANGE:` decision.
