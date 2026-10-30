"""Deployment settings come from AGENTIC_CP_* variables only; typos and retired names are
refused, and secrets never show in a repr (Review Focus 5)."""

import pytest

from agentic_cp.db import make_engine
from agentic_cp.settings import Settings, SettingsError

# Built from pieces so no secret scanner mistakes it for a real credential.
PASSWORD = "pw-" + "not-a-real-one"
DSN = f"postgresql+asyncpg://cp_app:{PASSWORD}@db.example:5432/agentic"


def _env(**extra: str) -> dict[str, str]:
    return {"AGENTIC_CP_DATABASE_URL": DSN, "PATH": "/bin", **extra}


def test_settings_from_env_reads_prefixed_variables():
    settings = Settings.from_env(_env(AGENTIC_CP_POLICY_POLL_SECONDS="30"))
    assert settings.policy_poll_seconds == 30
    assert settings.database_url.get_secret_value() == DSN
    assert settings.db_schema == "agentic"
    assert settings.user_token_days == 90
    assert settings.service_token_max_days == 365
    assert settings.require_db_enforcement is True


def test_settings_refuse_unknown_prefixed_variable():
    with pytest.raises(SettingsError, match="AGENTIC_CP_POLICY_POL_SECONDS"):
        Settings.from_env(_env(AGENTIC_CP_POLICY_POL_SECONDS="30"))


def test_settings_refuse_the_retired_tenant_variable_with_a_hint():
    with pytest.raises(SettingsError, match="AGENTIC_CP_TENANT") as caught:
        Settings.from_env(_env(AGENTIC_CP_TENANT="acme"))
    assert "admin tenant create" in str(caught.value)


@pytest.mark.parametrize(
    ("name", "value", "field"),
    [
        ("AGENTIC_CP_POLICY_POLL_SECONDS", "5", "policy_poll_seconds"),
        ("AGENTIC_CP_MAX_INGEST_ENTRIES", "0", "max_ingest_entries"),
        ("AGENTIC_CP_DB_SCHEMA", "agentic; drop", "db_schema"),
        ("AGENTIC_CP_REQUIRE_DB_ENFORCEMENT", "perhaps", "require_db_enforcement"),
        ("AGENTIC_CP_MIN_CLI_VERSION", "1.2.x", "min_cli_version"),  # review F4
        ("AGENTIC_CP_MIN_CLI_VERSION", "latest", "min_cli_version"),
    ],
)
def test_settings_refuse_bad_values(name, value, field):
    with pytest.raises(SettingsError, match=field):
        Settings.from_env(_env(**{name: value}))


def test_settings_need_a_database_url():
    with pytest.raises(SettingsError, match="AGENTIC_CP_DATABASE_URL"):
        Settings.from_env({})


def test_settings_repr_hides_secrets():
    settings = Settings.from_env(_env(AGENTIC_CP_OWNER_DATABASE_URL=DSN.replace("cp_app", "own")))
    for text in (repr(settings), str(settings)):
        assert PASSWORD not in text


def test_settings_error_never_echoes_a_secret_value():
    with pytest.raises(SettingsError) as caught:
        Settings.from_env(_env(AGENTIC_CP_MAX_BODY_BYTES=PASSWORD))
    assert PASSWORD not in str(caught.value)


def test_mail_settings_default_to_starttls_and_no_relay():
    settings = Settings.from_env(_env())
    assert settings.smtp_host is None
    assert settings.smtp_port == 587
    assert settings.smtp_security == "starttls"
    assert settings.invite_hours == 72  # ruling P8
    assert settings.notify_max_attempts == 8
    assert settings.public_url is None
    assert settings.gateway_admin_url is None and settings.gateway_admin_key is None
    assert settings.gateway_team_rate_limits is False and settings.routing_max_attempts == 8


def test_mail_and_gateway_settings_are_read():
    settings = Settings.from_env(
        _env(
            AGENTIC_CP_SMTP_HOST="relay.acme.example",
            AGENTIC_CP_SMTP_PORT="465",
            AGENTIC_CP_SMTP_SECURITY="tls",
            AGENTIC_CP_SMTP_USERNAME="agentic",
            AGENTIC_CP_SMTP_PASSWORD=PASSWORD,
            AGENTIC_CP_SMTP_FROM="agentic@acme.example",
            AGENTIC_CP_PUBLIC_URL="https://cp.acme.example/",
            AGENTIC_CP_GATEWAY_ADMIN_URL="https://gateway.acme.example",
            AGENTIC_CP_GATEWAY_ADMIN_KEY=PASSWORD,
            AGENTIC_CP_GATEWAY_URL="https://gateway.acme.example",
            AGENTIC_CP_INVITE_HOURS="24",
        )
    )
    assert settings.smtp_security == "tls" and settings.smtp_port == 465
    assert settings.smtp_password.get_secret_value() == PASSWORD
    assert settings.public_url == "https://cp.acme.example"  # no trailing slash
    assert settings.gateway_admin_key.get_secret_value() == PASSWORD
    assert settings.invite_hours == 24
    for text in (repr(settings), str(settings)):
        assert PASSWORD not in text


@pytest.mark.parametrize(
    ("name", "value", "field"),
    [
        ("AGENTIC_CP_SMTP_SECURITY", "maybe", "smtp_security"),
        ("AGENTIC_CP_SMTP_PORT", "0", "smtp_port"),
        ("AGENTIC_CP_INVITE_HOURS", "0", "invite_hours"),
        ("AGENTIC_CP_INVITE_HOURS", "337", "invite_hours"),
        ("AGENTIC_CP_PUBLIC_URL", "ftp://cp.example", "public_url"),
        ("AGENTIC_CP_GATEWAY_ADMIN_URL", "gateway.example", "gateway_admin_url"),
        ("AGENTIC_CP_SMTP_FROM", "not-an-address", "smtp_from"),
    ],
)
def test_mail_settings_refuse_bad_values(name, value, field):
    with pytest.raises(SettingsError, match=field):
        Settings.from_env(_env(**{name: value}))


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_a_blank_metrics_token_is_no_token(value):
    """A blank secret (an empty Helm or Kubernetes value) must not open `/metrics` to
    `Authorization: Bearer` with nothing after it (review F1): it is unset, so 404."""
    settings = Settings.from_env(_env(AGENTIC_CP_METRICS_TOKEN=value))
    assert settings.metrics_token is None


def test_a_short_metrics_token_is_refused():
    with pytest.raises(SettingsError, match="metrics_token") as caught:
        Settings.from_env(_env(AGENTIC_CP_METRICS_TOKEN="short-1"))
    assert "short-1" not in str(caught.value)


def test_the_pool_keeps_its_connections_open_by_default():
    """M3-22: a pool of 5 with 10 overflow connections opened and closed connections all
    through the load test (each a password exchange and a new Postgres backend). The same
    15 now stay open; both numbers are settings."""
    settings = Settings.from_env(_env())
    assert (settings.db_pool_size, settings.db_max_overflow) == (15, 0)
    pool = make_engine(settings).sync_engine.pool
    assert pool.size() == 15 and pool._max_overflow == 0
    tuned = Settings.from_env(_env(AGENTIC_CP_DB_POOL_SIZE="40", AGENTIC_CP_DB_MAX_OVERFLOW="5"))
    pool = make_engine(tuned).sync_engine.pool
    assert pool.size() == 40 and pool._max_overflow == 5


@pytest.mark.parametrize(
    ("name", "value"), [("AGENTIC_CP_DB_POOL_SIZE", "0"), ("AGENTIC_CP_DB_MAX_OVERFLOW", "-1")]
)
def test_pool_settings_refuse_bad_values(name, value):
    with pytest.raises(SettingsError):
        Settings.from_env(_env(**{name: value}))
