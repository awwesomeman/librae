"""Every ADR pointed at from code or docs must exist.

Error messages and guides cite decision records by path so a caller who hits
a rule can read why it exists. A renamed or deleted ADR turns that into a
dead reference that nothing else would catch.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DECISIONS = ROOT / "docs/decisions"
_REFERENCE = re.compile(r"docs/decisions/([0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9-]+\.md)")
_SEARCHED = ("librae", "docs", "architecture.md", "README.md", "SECURITY.md")
# docs/README.md exempts these from the writing conventions: they are records
# of what was thought at the time, not rewritten. An ADR rename should not
# force an edit to a historical document.
_HISTORICAL = ("docs/plans", "docs/research", "docs/spikes")


def _referenced() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for target in _SEARCHED:
        path = ROOT / target
        files = sorted(path.rglob("*.md")) + sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for file in files:
            if str(file.relative_to(ROOT)).startswith(_HISTORICAL):
                continue
            for name in _REFERENCE.findall(file.read_text(encoding="utf-8")):
                found.setdefault(name, []).append(str(file.relative_to(ROOT)))
    return found


def test_the_scan_finds_the_references_this_repo_has() -> None:
    # Guards the regex: a pattern that matched nothing would make the
    # assertion below vacuous.
    assert _referenced()


def test_every_referenced_decision_record_exists() -> None:
    missing = {
        name: sorted(set(sources))
        for name, sources in _referenced().items()
        if not (DECISIONS / name).is_file()
    }

    assert missing == {}
