# AGENTS.md

Instructions for coding agents. Agents follow the same rules as human
contributors; this file points to them and adds what only agents need. Where
general industry practice and these docs disagree, these docs win.

## Read first

- [CONTRIBUTING.md](CONTRIBUTING.md) — setup and checks, change discipline,
  doc and comment writing rules, commit and PR conventions.
- [architecture.md](architecture.md) — design contract, including the
  compatibility and failure handling policies.
- [docs/README.md](docs/README.md) — which doc owns what, and what wins when
  docs disagree.

## Where agent defaults go wrong here

- **No AI attribution** in commits or squash messages: no `Co-Authored-By`,
  no "Generated with", no `Signed-off-by`.
- **Commit identity**: if `git config user.email` is empty, ask instead of
  committing as the machine default.
- **No history in docs or comments**: no issue/PR numbers, "fixed in",
  "previously", or "as of <date>". State the reason; history goes in the
  commit message.
- **No new rule files**: change the canonical doc above instead of restating
  a rule elsewhere.
