"""Every environment variable librae or its deploy scripts read, declared once.

Which file a variable belongs in, whether it is a secret, and how a configured
machine is validated all live here. The env templates, the deploy scripts'
guards, and ``librae doctor`` are checked against this module by tests instead
of restating it — restated knowledge is how a bot token sat in a synced .env
for weeks while every template was correct.
"""

from __future__ import annotations

import dataclasses
import difflib
import functools
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Self, get_type_hints
from urllib.parse import urlsplit

_MASK = "<redacted>"


class Secret:
    """A string that will not print itself.

    ``str``, ``repr`` and f-strings give a mask; the value is only reachable
    through :meth:`reveal`, so the one call site that needs it shows up in a
    grep. Empty means "not configured" and is falsy, like the str it replaces.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str | Secret = "") -> None:
        self._value = value._value if isinstance(value, Secret) else str(value)

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        if isinstance(other, str):
            return self._value == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __str__(self) -> str:
        return _MASK if self._value else ""

    def __repr__(self) -> str:
        return f"Secret({_MASK!r})" if self._value else "Secret('')"

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)


@functools.cache
def _secret_fields(cls: type) -> tuple[str, ...]:
    return tuple(name for name, kind in get_type_hints(cls).items() if kind is Secret)


@dataclass
class CredentialConfig:
    """Base for per-venue credential dataclasses.

    Fields annotated ``Secret`` are wrapped on construction, so a str from the
    environment or a test is accepted and still never prints. Env-var
    convention: ``{PREFIX}_{FIELD_UPPER}``, e.g. ``BINANCE_API_KEY``.
    """

    def __post_init__(self) -> None:
        for name in _secret_fields(type(self)):
            value = getattr(self, name)
            if not isinstance(value, Secret):
                setattr(self, name, Secret(value))

    @classmethod
    def from_env(cls, prefix: str, **overrides: object) -> Self:
        """Build from ``{prefix}_{FIELD_UPPER}`` env vars; overrides win."""
        kwargs: dict[str, object] = {}
        for f in dataclasses.fields(cls):
            if f.name in overrides:
                kwargs[f.name] = overrides[f.name]
            else:
                env_val = os.environ.get(f"{prefix}_{f.name.upper()}")
                if env_val is not None:
                    kwargs[f.name] = env_val
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class Where(Enum):
    """Which file a variable belongs in."""

    ENV = ".env"  # synced to every host by cloud_deploy.sh
    SECRETS = ".env.secrets"  # written by hand on each host, never synced


@dataclass(frozen=True, slots=True)
class EnvVar:
    name: str
    where: Where
    secret: bool = False


ENV_VARS: tuple[EnvVar, ...] = (
    # .env — deployment settings that are safe on every host
    EnvVar("GF_BIND", Where.ENV),
    EnvVar("GRAFANA_URL", Where.ENV),
    EnvVar("VPS_DB_HOST", Where.ENV),
    EnvVar("TELEGRAM_CHAT_ID", Where.ENV),
    EnvVar("BINANCE_EXCHANGE_ID", Where.ENV),
    EnvVar("TSDB_BIND", Where.ENV),
    EnvVar("TSDB_PORT", Where.ENV),
    EnvVar("TRADE_STRATEGY_PATH", Where.ENV),
    EnvVar("TRADE_IMAGE", Where.ENV),
    EnvVar("TRADE_IMAGE_REF", Where.ENV),
    # .env.secrets — trading credentials and the venue settings that travel with them
    EnvVar("BINANCE_API_KEY", Where.SECRETS, secret=True),
    EnvVar("BINANCE_API_SECRET", Where.SECRETS, secret=True),
    EnvVar("BINANCE_SANDBOX", Where.SECRETS),
    EnvVar("SHIOAJI_API_KEY", Where.SECRETS, secret=True),
    EnvVar("SHIOAJI_SECRET_KEY", Where.SECRETS, secret=True),
    EnvVar("SHIOAJI_PERSON_ID", Where.SECRETS, secret=True),  # national ID number
    EnvVar("SHIOAJI_CA_PATH", Where.SECRETS),
    EnvVar("SHIOAJI_CA_PASSWORD", Where.SECRETS, secret=True),
    EnvVar("SHIOAJI_SANDBOX", Where.SECRETS),
    EnvVar("IBKR_HOST", Where.SECRETS),
    EnvVar("IBKR_PORT", Where.SECRETS),
    EnvVar("IBKR_CLIENT_ID", Where.SECRETS),
    # .env.secrets — shared infra secrets, one set per deployment
    EnvVar("POSTGRES_PASSWORD", Where.SECRETS, secret=True),
    EnvVar("POSTGRES_APP_PASSWORD", Where.SECRETS, secret=True),
    EnvVar("POSTGRES_GRAFANA_PASSWORD", Where.SECRETS, secret=True),
    EnvVar("GF_SECURITY_ADMIN_PASSWORD", Where.SECRETS, secret=True),
    EnvVar("TELEGRAM_BOT_TOKEN", Where.SECRETS, secret=True),
    EnvVar("TS_AUTHKEY", Where.SECRETS, secret=True),
    EnvVar("TIMESCALE_DSN", Where.SECRETS, secret=True),
    EnvVar("TRADE_TIMESCALE_DSN", Where.SECRETS, secret=True),
)

DECLARED: Mapping[str, EnvVar] = {v.name: v for v in ENV_VARS}
SECRET_NAMES: frozenset[str] = frozenset(v.name for v in ENV_VARS if v.secret)

# Name suffixes that mark a credential. The registry is the authority for
# declared names; this is the fallback for names librae does not know (another
# tool's key parked in the synced .env). deploy/cloud_deploy.sh carries the
# same alternation because a shell cannot import this module — a test keeps
# the two identical.
SECRET_NAME_SUFFIXES: tuple[str, ...] = ("PASSWORD", "SECRET", "TOKEN", "DSN", "KEY", "PERSON_ID")
SECRET_NAME_PATTERN = re.compile(rf"^[A-Za-z_][A-Za-z0-9_]*({'|'.join(SECRET_NAME_SUFFIXES)})$")

# Halves of an authentication pair: one without the other is a typo'd name.
PAIRED_NAMES: tuple[tuple[str, str], ...] = (
    ("BINANCE_API_KEY", "BINANCE_API_SECRET"),
    ("SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY"),
)

# DSNs must name the application role and carry its password.
DSN_NAMES: tuple[str, ...] = ("TIMESCALE_DSN", "TRADE_TIMESCALE_DSN")
DSN_ROLE = "quant_app"
DSN_PASSWORD_NAME = "POSTGRES_APP_PASSWORD"


# ---------------------------------------------------------------------------
# Env files
# ---------------------------------------------------------------------------

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def parse_env_file(path: Path, context: Mapping[str, str] | None = None) -> dict[str, str]:
    """``KEY=VALUE`` lines as a sourcing shell sees them.

    Comments and blanks are skipped, matching outer quotes are stripped, and
    ``${NAME}`` is expanded against earlier lines then *context* — which is
    how the DSNs reference the app password without repeating it.
    """
    values: dict[str, str] = {}
    scope: dict[str, str] = dict(context or {})
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if match is None:
            continue
        name, raw = match.groups()
        quoted = len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'"
        value = raw[1:-1] if quoted else raw
        if not (quoted and raw[0] == "'"):
            value = _REFERENCE.sub(lambda ref: scope.get(ref.group(1), ""), value)
        values[name] = value
        scope[name] = value
    return values


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    level: str  # "error" | "warning"
    message: str


def doctor(project_root: Path) -> list[Finding]:
    """Validate a machine's .env / .env.secrets against the registry.

    Reports names only, never values. Errors are misconfigurations that would
    fail late or leak; warnings are placements worth tidying.
    """
    findings: list[Finding] = []
    env_path = project_root / Where.ENV.value
    secrets_path = project_root / Where.SECRETS.value
    env = parse_env_file(env_path) if env_path.is_file() else {}
    secrets = parse_env_file(secrets_path, context=env) if secrets_path.is_file() else {}
    if not env_path.is_file():
        findings.append(Finding("warning", f"{Where.ENV.value} not found"))
    if not secrets_path.is_file():
        findings.append(Finding("warning", f"{Where.SECRETS.value} not found"))

    for where, values in ((Where.ENV, env), (Where.SECRETS, secrets)):
        for name in values:
            declared = DECLARED.get(name)
            if declared is None:
                # Undeclared names are usually another tool's variables sharing
                # the file; only a lookalike (a typo) or a credential-shaped
                # name in the synced file is worth reporting.
                hint = difflib.get_close_matches(name, DECLARED, n=1, cutoff=0.8)
                if hint:
                    findings.append(
                        Finding(
                            "error",
                            f"{where.value}: unknown variable {name} (did you mean {hint[0]}?)",
                        )
                    )
                elif where is Where.ENV and values[name] and SECRET_NAME_PATTERN.match(name):
                    findings.append(
                        Finding(
                            "error",
                            f"{where.value}: {name} looks like a credential; "
                            f"{Where.SECRETS.value} is the file that is never synced",
                        )
                    )
            elif declared.secret and where is Where.ENV and values[name]:
                findings.append(
                    Finding(
                        "error",
                        f"{where.value}: {name} is a secret; it belongs in "
                        f"{Where.SECRETS.value}, which is never synced",
                    )
                )
            elif declared.where is not where and values[name]:
                findings.append(
                    Finding("warning", f"{where.value}: {name} belongs in {declared.where.value}")
                )

    merged = {**env, **secrets}
    for first, second in PAIRED_NAMES:
        if bool(merged.get(first)) != bool(merged.get(second)):
            findings.append(
                Finding("error", f"{first} and {second} must be set together or both left empty")
            )

    app_password = merged.get(DSN_PASSWORD_NAME, "")
    for name in DSN_NAMES:
        dsn = merged.get(name)
        if not dsn:
            continue
        parts = urlsplit(dsn)
        if parts.username != DSN_ROLE:
            findings.append(
                Finding(
                    "error",
                    f"{name} connects as {parts.username or '<none>'}; the application "
                    f"role is {DSN_ROLE} (the admin role is for migrations only)",
                )
            )
        if app_password and parts.password != app_password:
            findings.append(
                Finding("error", f"{name} carries a password that differs from {DSN_PASSWORD_NAME}")
            )
    return findings


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_MIN_SCRUB_LEN = 8


class RedactSecrets(logging.Filter):
    """Scrub configured secret values from every log record on a handler.

    ``Secret`` keeps librae's own code from printing a credential; this catches
    third-party loggers that were handed the revealed value (httpx logged the
    Telegram bot token as part of the request URL). Values shorter than eight
    characters are left alone so a placeholder like ``test`` cannot blank out
    ordinary words.
    """

    def __init__(self, values: Iterable[str] | None = None) -> None:
        super().__init__()
        if values is None:
            values = (os.environ.get(name, "") for name in SECRET_NAMES)
        self._values = tuple(
            sorted({v for v in values if len(v) >= _MIN_SCRUB_LEN}, key=len, reverse=True)
        )

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._values:
            return True
        message = record.getMessage()
        scrubbed = message
        for value in self._values:
            scrubbed = scrubbed.replace(value, _MASK)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True
