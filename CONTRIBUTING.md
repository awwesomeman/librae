# Contributing

Rules for changing this repository, for humans and coding agents alike. The
design contract lives in [architecture.md](architecture.md); which doc owns
what lives in the [documentation index](docs/README.md).

## Setup and checks

Set up all development integrations you intend to test:

```bash
uv sync --extra test --extra dev --extra db --extra crypto-live
git config core.hooksPath .githooks
```

Add `--extra tw-live`, `--extra us-live`, or `--extra viz` only when needed.
Standard editable installation also works when the caller does not use the
repository's `uv` environment: `python -m pip install --editable .`

Run before every commit, through `uv run`:

```bash
uv run pytest -q -m "not sdk_contract" tests/
uv run ruff check .
uv run ruff format --check .
```

The pre-commit hook runs the Ruff checks. Tests stay offline: external
boundaries (brokers, DB, network) use mocks unless explicitly documented
otherwise.

A test that needs an optional broker SDK installed is marked
`sdk_contract`: core CI runs without the SDKs, and a separate job runs the
marked tests with them. Run those with
`uv run pytest -q -m sdk_contract tests/` when the SDK extras are installed.

## Changing code

- Place every new feature against the
  [product boundary](architecture.md#product-position-and-system-boundaries)
  first. If the caller, a strategy project, or an external tool owns it, don't
  add it to Librae: expose the smallest extension point (injected callable,
  adapter, public type) or document the caller's responsibility. Moving the
  boundary is a design decision: record it in `docs/decisions/` and
  `architecture.md` before writing the code.
- Build for a concrete need, not a speculative one. Extract shared code only
  for real duplication at two or more call sites.
- Follow the [compatibility policy](architecture.md#compatibility-policy-before-10)
  and the [failure handling policy](architecture.md#failure-handling-policy).
- Fix the class of problem, not the one instance, and keep the diff minimal.
  Extend an existing mechanism before adding a new field, flag or path.
- A behavior or structure change updates `architecture.md` and any affected
  guide in the same change.
- Never commit secrets or local state: `.env*` (except `*.example`),
  `.secrets/`, `.credentials/`, `logs/`, `.claude/`.

## Writing docs and comments

Applies to every doc except `docs/plans/`, `docs/research/`, and
`docs/spikes/`, which are historical by design:

1. **No drift-prone specifics.** Don't restate counts, names, or field lists
   that live in code — link to the file/symbol and describe the invariant
   instead, so the doc can't silently fall out of sync.
2. **No history in current-state text.** Don't cite issue or PR numbers, or
   write "added in", "fixed in", "previously", or "as of <date>". State the
   reason itself; the history belongs in the commit message. `decisions/`
   and `learnings/` record history and are exempt; elsewhere, a date that
   stamps evidence (rule 5) and a link to a dated record there are fine.
3. **Concise and scannable.** Short paragraphs/bullets, one point per line,
   no padding.
4. **One canonical home per concept.** If two docs would describe the same
   thing, pick one and link from the other instead of restating it.
5. **External facts carry a source.** A claim about what a venue, broker
   SDK, or third-party service accepts or rejects must cite one of: a live
   observation (date plus the verbatim error or response), the official
   document, or an explicit "unverified". A library exposing a name is not
   evidence the venue supports it. Unsourced claims are assumptions, and
   should read as such.
6. **Language.** English outside `docs/`; preserve each existing document's
   language.

Rules 1, 2 and 5 apply to docstrings and code comments too. A comment states
why the code is the way it is; what it currently does is stated by the code.

## Commits

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope)[!]: short imperative summary in lowercase

Prose body, wrapped near 72 columns. Say what was wrong or missing, why it
matters, what the change does, and what it deliberately leaves alone.

BREAKING CHANGE: the contract that changed and what the consumer does now.

Refs #123
```

- **Types**: `feat`, `fix`, `refactor`, `docs`, `test`. Scope is the area
  touched (`live`, `data`, `db`, `deploy`, `brokers`, ...).
- **Subject** describes the behavior, not the edit: "keep the database password
  off the docker command line", not "update trade.sh". No trailing period.
- **Body** is short prose paragraphs, not a changelog of files. Omit it only
  when the subject says everything.
- **Breaking**: `!` plus a `BREAKING CHANGE:` footer whenever previously
  accepted input now fails or a consumer gets something different back — see
  the compatibility policy.
- **Issue footer**: `Refs #N`, or `Closes #N` when the commit resolves it.
- **No attribution trailers**: no `Co-Authored-By:` for an AI or a tool, no
  "Generated with" line, no `Signed-off-by:`.

## Branches and pull requests

- Open or reference an issue first for non-trivial changes. Branch as
  `<type>/<issue>-<slug>`, e.g. `fix/251-futures-selector-at-config-time`.
- PR title is the commit subject plus the issue: `fix(config)!: ... (#251)`.
- PR body: `Closes #N`, then a summary, anything deliberately not covered, and
  the test plan actually run (with results).
- PRs are squash-merged. The squash message must read as one commit under the
  rules above: rewrite it at merge, drop every auto-added `Co-authored-by`,
  and confirm the `!`/`BREAKING CHANGE:` decision.
