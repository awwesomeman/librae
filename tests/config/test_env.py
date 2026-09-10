"""librae.config.env — the one declaration every env template and guard is checked against."""

from __future__ import annotations

import dataclasses
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from librae.config.env import (
    ENV_VARS,
    SECRET_NAMES,
    CredentialConfig,
    RedactSecrets,
    Secret,
    Where,
    doctor,
    parse_env_file,
)


class TestSecret:
    def test_never_prints_itself(self):
        secret = Secret("hunter2-hunter2")

        assert "hunter2" not in str(secret)
        assert "hunter2" not in repr(secret)
        assert "hunter2" not in f"{secret}"
        assert "hunter2" not in f"{secret:>30}"

    def test_reveal_is_the_only_way_out(self):
        assert Secret("hunter2").reveal() == "hunter2"

    def test_empty_is_falsy_and_prints_empty(self):
        assert not Secret()
        assert not Secret("")
        assert str(Secret()) == ""
        assert Secret("x")

    def test_compares_by_value_with_str_and_secret(self):
        assert Secret("a") == "a"
        assert Secret("a") == Secret("a")
        assert Secret("a") != "b"
        assert hash(Secret("a")) == hash(Secret("a"))

    def test_wrapping_a_secret_does_not_double_wrap(self):
        assert Secret(Secret("a")).reveal() == "a"

    def test_dataclass_repr_masks_secret_fields(self):
        @dataclass
        class Creds(CredentialConfig):
            token: Secret = field(default_factory=Secret)
            chat: str = ""

        creds = Creds(token="tok-1234", chat="chat-1")

        assert "tok-1234" not in repr(creds)
        assert "chat-1" in repr(creds)
        assert "tok-1234" not in str(dataclasses.asdict(creds))


class TestCredentialConfig:
    @dataclass
    class Creds(CredentialConfig):
        api_key: Secret = field(default_factory=Secret)
        region: str = "eu"

    def test_str_is_wrapped_on_construction(self):
        creds = self.Creds(api_key="k")

        assert isinstance(creds.api_key, Secret)
        assert creds.api_key.reveal() == "k"
        assert creds.region == "eu"

    def test_from_env_reads_prefixed_names_and_overrides_win(self, monkeypatch):
        monkeypatch.setenv("ACME_API_KEY", "from-env")
        monkeypatch.setenv("ACME_REGION", "us")

        creds = self.Creds.from_env("ACME", region="ap")

        assert creds.api_key == "from-env"
        assert isinstance(creds.api_key, Secret)
        assert creds.region == "ap"

    def test_from_env_leaves_unset_fields_at_their_defaults(self, monkeypatch):
        monkeypatch.delenv("ACME_API_KEY", raising=False)
        monkeypatch.delenv("ACME_REGION", raising=False)

        creds = self.Creds.from_env("ACME")

        assert not creds.api_key
        assert creds.region == "eu"


class TestParseEnvFile:
    def test_reads_like_a_sourcing_shell(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text(
            "\n".join(
                (
                    "# comment",
                    "",
                    "PLAIN=value",
                    "export EXPORTED=yes",
                    'QUOTED="with spaces"',
                    "APP_PW=s3cret",
                    "DSN=postgresql://quant_app:${APP_PW}@db/quant",
                    "LITERAL='${APP_PW}'",
                    "MISSING=${NOPE}x",
                )
            )
        )

        values = parse_env_file(env)

        assert values["PLAIN"] == "value"
        assert values["EXPORTED"] == "yes"
        assert values["QUOTED"] == "with spaces"
        assert values["DSN"] == "postgresql://quant_app:s3cret@db/quant"
        assert values["LITERAL"] == "${APP_PW}"
        assert values["MISSING"] == "x"

    def test_context_supplies_names_from_an_earlier_file(self, tmp_path: Path):
        secrets = tmp_path / ".env.secrets"
        secrets.write_text("DSN=postgresql://u:${PW}@h/d\n")

        assert parse_env_file(secrets, context={"PW": "p"})["DSN"] == "postgresql://u:p@h/d"


def _project(tmp_path: Path, env: str, secrets: str) -> Path:
    (tmp_path / ".env").write_text(env)
    (tmp_path / ".env.secrets").write_text(secrets)
    return tmp_path


_CLEAN_SECRETS = "\n".join(
    (
        "POSTGRES_APP_PASSWORD=app-pw",
        "TIMESCALE_DSN=postgresql://quant_app:${POSTGRES_APP_PASSWORD}@localhost:5432/quant",
        "TRADE_TIMESCALE_DSN=postgresql://quant_app:${POSTGRES_APP_PASSWORD}@db:5432/quant",
        "BINANCE_API_KEY=k",
        "BINANCE_API_SECRET=s",
    )
)


class TestDoctor:
    def test_clean_project_has_no_findings(self, tmp_path: Path):
        project = _project(tmp_path, "GF_BIND=127.0.0.1\nTELEGRAM_CHAT_ID=1\n", _CLEAN_SECRETS)

        assert doctor(project) == []

    def test_misspelled_name_is_an_error_with_a_hint(self, tmp_path: Path):
        project = _project(tmp_path, "", "BINANCE_API_KEY=k\nBINANCE_API_SECRE=s\n")

        messages = [f.message for f in doctor(project) if f.level == "error"]

        assert any("unknown variable BINANCE_API_SECRE" in m for m in messages)
        assert any("did you mean BINANCE_API_SECRET" in m for m in messages)

    def test_secret_in_the_synced_env_is_an_error_naming_only_the_key(self, tmp_path: Path):
        project = _project(tmp_path, "TELEGRAM_BOT_TOKEN=8000:AAsecretvalue\n", _CLEAN_SECRETS)

        errors = [f for f in doctor(project) if f.level == "error"]

        assert len(errors) == 1
        assert "TELEGRAM_BOT_TOKEN" in errors[0].message
        assert ".env.secrets" in errors[0].message
        assert "AAsecretvalue" not in errors[0].message

    def test_half_a_key_pair_is_an_error(self, tmp_path: Path):
        project = _project(tmp_path, "", "SHIOAJI_API_KEY=k\n")

        messages = [f.message for f in doctor(project) if f.level == "error"]

        assert any("SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY" in m for m in messages)

    def test_dsn_with_the_admin_role_is_an_error(self, tmp_path: Path):
        secrets = "POSTGRES_APP_PASSWORD=app-pw\nTIMESCALE_DSN=postgresql://quant:app-pw@h/quant\n"
        project = _project(tmp_path, "", secrets)

        messages = [f.message for f in doctor(project) if f.level == "error"]

        assert any("TIMESCALE_DSN connects as quant" in m for m in messages)
        assert not any("app-pw" in m for m in messages)

    def test_dsn_password_drift_is_an_error(self, tmp_path: Path):
        secrets = (
            "POSTGRES_APP_PASSWORD=new-pw\nTIMESCALE_DSN=postgresql://quant_app:old-pw@h/quant\n"
        )
        project = _project(tmp_path, "", secrets)

        messages = [f.message for f in doctor(project) if f.level == "error"]

        assert any("differs from POSTGRES_APP_PASSWORD" in m for m in messages)
        assert not any("old-pw" in m or "new-pw" in m for m in messages)

    def test_non_secret_in_the_wrong_file_is_only_a_warning(self, tmp_path: Path):
        project = _project(tmp_path, "", _CLEAN_SECRETS + "\nTELEGRAM_CHAT_ID=1\n")

        findings = doctor(project)

        assert [f.level for f in findings] == ["warning"]
        assert "TELEGRAM_CHAT_ID belongs in .env" in findings[0].message

    def test_missing_files_are_warnings_not_crashes(self, tmp_path: Path):
        assert {f.level for f in doctor(tmp_path)} == {"warning"}


class TestRedactSecrets:
    def _record(self, message: str) -> logging.LogRecord:
        return logging.LogRecord("httpx", logging.INFO, __file__, 1, message, (), None)

    def test_scrubs_configured_values_from_third_party_records(self):
        redact = RedactSecrets(values=["8000:AAlongtokenvalue"])
        record = self._record(
            "HTTP Request: POST https://api.telegram.org/bot8000:AAlongtokenvalue/sendMessage"
        )

        assert redact.filter(record) is True
        assert "AAlongtokenvalue" not in record.getMessage()
        assert "<redacted>" in record.getMessage()

    def test_scrubs_the_traceback_a_third_party_exception_carried(self):
        """The formatter renders exc_info separately, so the message is only half.

        httpx puts the bot token in the request URL it echoes back in the
        exception, which is why ``logger.exception`` around a broker or
        notifier call is the realistic leak path.
        """
        redact = RedactSecrets(values=["8000:AAlongtokenvalue"])
        try:
            raise RuntimeError(
                "POST https://api.telegram.org/bot8000:AAlongtokenvalue/sendMessage failed"
            )
        except RuntimeError:
            record = logging.LogRecord(
                "httpx", logging.ERROR, __file__, 1, "send failed", (), sys.exc_info()
            )

        assert redact.filter(record) is True

        rendered = logging.Formatter().format(record)
        assert "AAlongtokenvalue" not in rendered
        assert "<redacted>" in rendered

    def test_short_placeholders_are_left_alone(self):
        redact = RedactSecrets(values=["test"])
        record = self._record("running the test suite")

        redact.filter(record)

        assert record.getMessage() == "running the test suite"

    def test_defaults_to_the_registry_secrets_in_the_environment(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "8000:AAfromenvironment")
        record = self._record("token=8000:AAfromenvironment")

        RedactSecrets().filter(record)

        assert "AAfromenvironment" not in record.getMessage()


class TestRegistry:
    def test_every_secret_lives_in_the_unsynced_file(self):
        assert all(v.where is Where.SECRETS for v in ENV_VARS if v.secret)

    def test_names_are_unique(self):
        names = [v.name for v in ENV_VARS]

        assert len(names) == len(set(names))

    def test_secret_names_is_the_declared_subset(self):
        assert {v.name for v in ENV_VARS if v.secret} == SECRET_NAMES
        assert "TELEGRAM_BOT_TOKEN" in SECRET_NAMES
        assert "TELEGRAM_CHAT_ID" not in SECRET_NAMES


@pytest.mark.parametrize("name", ["TS_AUTHKEY", "SHIOAJI_PERSON_ID"])
def test_easily_overlooked_credentials_are_declared_secret(name: str):
    assert name in SECRET_NAMES


class TestDoctorUnknownNames:
    """Other tools share these files; only typos and misplaced credentials are ours to flag."""

    def test_another_tools_variable_in_the_secrets_file_is_silent(self, tmp_path: Path):
        project = _project(tmp_path, "", _CLEAN_SECRETS + "\nFRED_API_KEY=abc\n")

        assert doctor(project) == []

    def test_another_tools_non_credential_in_env_is_silent(self, tmp_path: Path):
        project = _project(tmp_path, "SOME_OTHER_FLAG=1\n", _CLEAN_SECRETS)

        assert doctor(project) == []

    def test_undeclared_credential_shaped_name_in_the_synced_env_is_an_error(self, tmp_path: Path):
        project = _project(tmp_path, "FRED_API_KEY=abc\n", _CLEAN_SECRETS)

        errors = [f for f in doctor(project) if f.level == "error"]

        assert len(errors) == 1
        assert "FRED_API_KEY looks like a credential" in errors[0].message
        assert "abc" not in errors[0].message

    def test_lookalike_of_a_declared_name_is_a_typo_wherever_it_sits(self, tmp_path: Path):
        project = _project(tmp_path, "BINANCE_KEY=k\n", "")

        messages = [f.message for f in doctor(project) if f.level == "error"]

        assert any("did you mean BINANCE_API_KEY" in m for m in messages)


class TestDoctorSingleFileLayout:
    """A `librae init` user keeps one .env; split rules apply only once .env.secrets exists."""

    def test_secrets_in_a_lone_env_are_not_placement_errors(self, tmp_path: Path):
        (tmp_path / ".env").write_text(
            "TIMESCALE_DSN=postgresql://me:pw@db/mine\nBINANCE_API_KEY=k\n"
            "BINANCE_API_SECRET=s\nTELEGRAM_BOT_TOKEN=t\nIBKR_HOST=127.0.0.1\n"
        )

        findings = doctor(tmp_path)

        assert [f.level for f in findings] == ["warning"]
        assert ".env.secrets not found" in findings[0].message

    def test_own_database_role_is_not_checked_without_the_compose_layout(self, tmp_path: Path):
        (tmp_path / ".env").write_text("TIMESCALE_DSN=postgresql://me:pw@db/mine\n")

        assert not [f for f in doctor(tmp_path) if f.level == "error"]

    def test_typos_and_pairs_are_still_checked(self, tmp_path: Path):
        (tmp_path / ".env").write_text("BINANCE_API_KEY=k\nTELEGRAM_BOT_TOKN=t\n")

        messages = [f.message for f in doctor(tmp_path) if f.level == "error"]

        assert any("did you mean TELEGRAM_BOT_TOKEN" in m for m in messages)
        assert any("BINANCE_API_KEY and BINANCE_API_SECRET" in m for m in messages)


class TestDsnRoleChecksAreGatedOnTheComposeLayout:
    """Gating each check on its own password let a blank one skip it silently."""

    def test_a_blank_admin_password_still_catches_the_wrong_role(self, tmp_path: Path):
        # POSTGRES_PASSWORD empty, so the password comparison cannot run — but
        # an admin DSN pointing at the application role is still a real error.
        project = _project(
            tmp_path,
            "",
            "\n".join(
                (
                    "POSTGRES_APP_PASSWORD=app-pw",
                    "POSTGRES_PASSWORD=",
                    "TIMESCALE_ADMIN_DSN=postgresql://quant_app:app-pw@h:5432/quant",
                )
            ),
        )

        messages = [f.message for f in doctor(project) if f.level == "error"]

        assert any("TIMESCALE_ADMIN_DSN connects as quant_app, not quant" in m for m in messages)

    def test_a_correct_admin_dsn_passes(self, tmp_path: Path):
        project = _project(
            tmp_path,
            "",
            _CLEAN_SECRETS
            + "\nPOSTGRES_PASSWORD=admin-pw"
            + "\nTIMESCALE_ADMIN_DSN=postgresql://quant:admin-pw@h:5432/quant",
        )

        assert doctor(project) == []

    def test_without_the_compose_marker_no_role_is_assumed(self, tmp_path: Path):
        # A pip user may set the generic POSTGRES_PASSWORD for their own
        # database whose owner role is not called "quant".
        project = _project(
            tmp_path,
            "",
            "POSTGRES_PASSWORD=pw\nTIMESCALE_ADMIN_DSN=postgresql://me:pw@h:5432/mine\n",
        )

        assert [f for f in doctor(project) if f.level == "error"] == []
