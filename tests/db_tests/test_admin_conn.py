"""The connection schema changes run under.

Separate from TIMESCALE_DSN because the application role holds DML only: it
cannot ALTER a table it does not own, nor write librae_schema_revision. The
whole privilege split rests on this helper, so its failure modes are held
here rather than assumed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from librae.db import admin_conn

ADMIN_DSN = "postgresql://quant:pw@localhost:5432/quant"


class TestItRefusesToGuess:
    def test_an_unset_admin_dsn_raises_and_names_the_variable(self, monkeypatch) -> None:
        monkeypatch.delenv("TIMESCALE_ADMIN_DSN", raising=False)
        monkeypatch.setenv("TIMESCALE_DSN", "postgresql://quant_app:pw@localhost:5432/quant")

        with pytest.raises(RuntimeError, match="TIMESCALE_ADMIN_DSN"), admin_conn():
            pass

    def test_it_never_falls_back_to_the_application_dsn(self, monkeypatch) -> None:
        """Falling back would hand the engine's own role a migration to run,
        which fails confusingly — or, on a deployment that pointed
        TIMESCALE_DSN at the owner, would quietly grant schema rights."""
        application = "postgresql://quant_app:pw@localhost:5432/quant"
        monkeypatch.delenv("TIMESCALE_ADMIN_DSN", raising=False)
        monkeypatch.setenv("TIMESCALE_DSN", application)

        with (
            patch("librae.db.psycopg2.connect") as connect,
            pytest.raises(RuntimeError),
            admin_conn(),
        ):
            pass

        connect.assert_not_called()


class TestItOwnsTheTransaction:
    def _connect(self, monkeypatch):
        monkeypatch.setenv("TIMESCALE_ADMIN_DSN", ADMIN_DSN)
        return patch("librae.db.psycopg2.connect", return_value=MagicMock())

    def test_success_commits_then_closes(self, monkeypatch) -> None:
        with self._connect(monkeypatch) as connect, admin_conn() as conn:
            pass

        connect.assert_called_once_with(ADMIN_DSN)
        conn.commit.assert_called_once()
        conn.rollback.assert_not_called()
        conn.close.assert_called_once()

    def test_failure_rolls_back_closes_and_reraises(self, monkeypatch) -> None:
        with (
            self._connect(monkeypatch) as connect,
            pytest.raises(ValueError, match="migration blew up"),
            admin_conn() as conn,
        ):
            raise ValueError("migration blew up")

        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()
        assert connect.call_count == 1

    def test_a_failing_rollback_does_not_mask_the_original_error(self, monkeypatch) -> None:
        with (
            self._connect(monkeypatch),
            pytest.raises(ValueError, match="original"),
            admin_conn() as conn,
        ):
            conn.rollback.side_effect = RuntimeError("connection already gone")
            raise ValueError("original")

        conn.close.assert_called_once()


class TestItStaysOutOfTheApplicationPool:
    def test_it_does_not_borrow_from_the_shared_pool(self, monkeypatch) -> None:
        """get_pool caches one pool per process and ignores the DSN once it
        exists, so borrowing would hand back an application connection under
        an admin-looking call."""
        monkeypatch.setenv("TIMESCALE_ADMIN_DSN", ADMIN_DSN)

        with (
            patch("librae.db.psycopg2.connect", return_value=MagicMock()),
            patch("librae.db.get_pool") as get_pool,
            admin_conn(),
        ):
            pass

        get_pool.assert_not_called()

    def test_the_schema_cli_uses_it_rather_than_the_pooled_connection(self, monkeypatch) -> None:
        monkeypatch.setenv("TIMESCALE_ADMIN_DSN", ADMIN_DSN)
        from librae.db import schema

        with (
            patch("librae.db.psycopg2.connect", return_value=MagicMock()) as connect,
            patch("librae.db.get_conn") as pooled,
            patch.object(schema, "inspect_schema") as inspect,
        ):
            inspect.return_value = MagicMock(current=True, state="current", revision=2)
            inspect.return_value.pending_revisions = ()
            schema._run_cli("preflight")

        connect.assert_called_once_with(ADMIN_DSN)
        pooled.assert_not_called()
