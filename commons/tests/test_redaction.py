"""The redactor for logs: Zyroflo's masking, composed (M1-47)."""

from sdlc_commons.redaction import redactor

KEY = "sk-live-0123456789abcdefABCD"  # gitleaks:allow (fake key)


def test_redactor_masks_env_values_token_shapes_and_flags():
    redact = redactor(env={"ACME_API_KEY": KEY, "PATH": "/usr/bin", "SHORT_TOKEN": "1"})
    assert KEY not in redact(f"using {KEY} now")
    assert "/usr/bin" in redact("PATH is /usr/bin")  # not secret-named: kept
    assert redact("retry 1 of 3") == "retry 1 of 3"  # a short value never mangles text
    assert "ghp_" not in redact("token ghp_abcdefghijklmnopqrstuvwxyz0123")
    assert "hunter2" not in redact("tool --password hunter2 --verbose")
    assert "<redacted>" in redact(
        "Authorization: Bearer eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.abcdefghijkl"
    )


def test_extra_secrets_are_masked_whatever_their_name():
    redact = redactor(env={}, extra=["tl-key-42-abcdef"])
    assert redact("key tl-key-42-abcdef") == "key <redacted>"


def test_the_default_environment_is_the_processes(monkeypatch):
    monkeypatch.setenv("ACME_SECRET", "s3cr3t-value-123456")
    assert "s3cr3t-value-123456" not in redactor()("x s3cr3t-value-123456 y")
