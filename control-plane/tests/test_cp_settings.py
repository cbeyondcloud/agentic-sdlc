"""Deployment settings come from AGENTIC_CP_* variables only; typos and retired names are
refused, and secrets never show in a repr (Review Focus 5)."""

import pytest

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
