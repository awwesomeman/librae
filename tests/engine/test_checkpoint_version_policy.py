"""The checkpoint version contract, and how it explains itself.

Rejecting an older checkpoint is deliberate: this document holds positions,
cash, in-flight orders and the halted flag, so a silently defaulted field
means the local book disagrees with the broker. Classifying the recent
version bumps showed the provably-additive case is a minority, and
misclassifying one produces exactly that divergence — so the rule stays exact
equality (#209).

What was worth fixing is the refusal. It reported two numbers and left the
operator to work out what they meant, which one of them was, and what to do.
"""

from __future__ import annotations

import pytest
from librae.live.state import _STATE_SCHEMA_VERSION, LiveRuntimeState


def _document(version: object) -> dict:
    return {
        "schema_version": version,
        "state_key": "sim:abc",
        "run_id": "r1",
        "config_hash": "abc",
        "mode": "sim",
        "account_id": "default",
        "cash": 1_000.0,
    }


class TestOlderCheckpointsAreStillRejected:
    """The compatibility rule itself is unchanged."""

    @pytest.mark.parametrize(
        "version",
        [_STATE_SCHEMA_VERSION - 1, _STATE_SCHEMA_VERSION + 1, None, "26"],
    )
    def test_anything_but_an_exact_match_is_refused(self, version: object) -> None:
        with pytest.raises(ValueError):
            LiveRuntimeState.from_dict(_document(version))


class TestTheRefusalIsActionable:
    @pytest.fixture
    def message(self) -> str:
        with pytest.raises(ValueError) as excinfo:
            LiveRuntimeState.from_dict(_document(_STATE_SCHEMA_VERSION - 1))
        return str(excinfo.value)

    def test_it_still_reports_both_versions(self, message: str) -> None:
        assert str(_STATE_SCHEMA_VERSION) in message
        assert str(_STATE_SCHEMA_VERSION - 1) in message

    def test_it_says_why_no_automatic_migration_happens(self, message: str) -> None:
        """Otherwise this reads as a missing feature rather than a decision."""
        assert "not migrated automatically" in message

    def test_it_names_the_operator_action(self, message: str) -> None:
        assert "stop flat" in message.lower()

    def test_it_points_at_the_documented_procedure(self, message: str) -> None:
        assert "optional-infrastructure" in message

    def test_it_distinguishes_this_from_the_database_revision(self, message: str) -> None:
        """Both were once called a "revision" in the same breath. A database
        schema upgrade is the wrong tool here: nothing migrates a stored
        checkpoint document."""
        assert "not the database schema revision" in message
        assert "does not transform a stored checkpoint" in message

    def test_it_does_not_enumerate_versions(self, message: str) -> None:
        """A per-version changelog in an error string is drift-prone: it would
        need editing on every bump and would silently go stale when it was
        not. The procedure is the same whatever the stored version is."""
        for version in range(_STATE_SCHEMA_VERSION - 6, _STATE_SCHEMA_VERSION - 1):
            assert str(version) not in message
