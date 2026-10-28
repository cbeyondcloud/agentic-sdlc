"""The container engine for the Linux stage of scripts/check.py (spike S12): Docker or
Podman, never tied to Docker Desktop."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check import container_engine  # noqa: E402


def _which(*present):
    return lambda name: f"/bin/{name}" if name in present else None


def test_the_named_engine_wins():
    assert container_engine({"CONTAINER_ENGINE": "podman"}, _which("docker", "podman")) == "podman"


def test_docker_is_found_first_then_podman():
    assert container_engine({}, _which("docker", "podman")) == "docker"
    assert container_engine({}, _which("podman")) == "podman"


def test_no_engine_is_none():
    assert container_engine({}, _which()) is None


def test_a_named_engine_that_is_missing_is_an_error():
    with pytest.raises(SystemExit, match="CONTAINER_ENGINE=podman"):
        container_engine({"CONTAINER_ENGINE": "podman"}, _which("docker"))


def test_an_unknown_engine_name_is_an_error():
    with pytest.raises(SystemExit, match="docker or podman"):
        container_engine({"CONTAINER_ENGINE": "lxc"}, _which("lxc"))


def test_offline_leaves_out_only_the_checks_that_need_the_network():  # DF-97
    from check import CHECKS, checks_for

    assert checks_for(offline=False) == CHECKS
    names = [name for name, _ in checks_for(offline=True)]
    assert "known vulnerabilities" not in names
    assert names == [name for name, _ in CHECKS if name != "known vulnerabilities"]


def test_a_failed_vulnerability_check_points_at_offline():
    from check import failure_hint

    assert "--offline" in failure_hint("known vulnerabilities")
    assert failure_hint("tests") == ""


def test_an_offline_run_says_it_is_incomplete(monkeypatch, capsys):
    import check

    ran = []

    def fake_run(cmd, cwd=None, env=None):
        ran.append((cmd, (env or {}).get("UV_OFFLINE")))
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(check.subprocess, "run", fake_run)
    assert check.run_native(offline=True) == check.INCOMPLETE
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out and "known vulnerabilities" in out
    assert all(flag == "1" for _, flag in ran)
    assert not any("pip-audit" in cmd for cmd, _ in ran)


def test_the_secret_scan_runs_before_anything_writes_into_the_tree():
    # Tests leave compiled files whose folded constants look like keys (P0-03 follow-up).
    from check import linux_steps

    names = [name for name, _ in linux_steps()]
    assert names.index("secret scan") < names.index("tests")
    assert names.index("secret scan") < names.index("lock file up to date")


def test_the_linux_stage_checks_the_alert_rules_with_promtool():
    # M2-12: promtool is a native binary, so it runs only in the Linux check image
    from check import REPO_ROOT, checks_for, linux_steps

    steps = dict(linux_steps())
    command = steps["alert rules"]
    assert command[:3] == ["promtool", "check", "rules"]
    assert (REPO_ROOT / command[3]).is_file()
    assert "alert rules" not in dict(checks_for(offline=False))
    dockerfile = (REPO_ROOT / "scripts" / "linux-check.Dockerfile").read_text(encoding="utf-8")
    assert "promtool" in dockerfile and "sha256sum -c" in dockerfile


def test_the_linux_stage_lints_the_shipped_workflows_with_actionlint():
    # M2-44: actionlint is a native binary, so it runs only in the Linux check image,
    # pinned by version and sha256; every shipped workflow template is linted
    from check import REPO_ROOT, WORKFLOW_TEMPLATES, checks_for, linux_steps

    command = dict(linux_steps())["workflow lint"]
    assert command[0] == "actionlint" and "workflow lint" not in dict(checks_for(offline=False))
    assert "cli/src/agentic_cli/templates/agentic-index-export.yml" in WORKFLOW_TEMPLATES
    assert all((REPO_ROOT / path).is_file() for path in WORKFLOW_TEMPLATES)
    dockerfile = (REPO_ROOT / "scripts" / "linux-check.Dockerfile").read_text(encoding="utf-8")
    assert "ACTIONLINT_VERSION=1.7.12" in dockerfile
    assert dockerfile.count("sha256sum -c") >= 4  # gitleaks, promtool, codex and actionlint


def _ran(returncode):
    return lambda *args, **kwargs: type("Done", (), {"returncode": returncode})()


def test_an_offline_run_exits_with_its_own_code(monkeypatch):  # DF-332
    # `check.py --offline && git push` must not look like a full pass
    import check

    monkeypatch.setattr(check.subprocess, "run", _ran(0))
    monkeypatch.setattr(check, "stale_exceptions", lambda *args: [])
    monkeypatch.setattr(sys, "argv", ["check.py", "--offline"])
    assert check.main() == check.EXIT_INCOMPLETE != 0


def test_a_failed_offline_run_is_still_a_failure(monkeypatch):
    import check

    monkeypatch.setattr(check.subprocess, "run", _ran(1))
    monkeypatch.setattr(check, "stale_exceptions", lambda *args: [])
    monkeypatch.setattr(sys, "argv", ["check.py", "--offline"])
    assert check.main() == 1


def test_a_release_age_exception_older_than_the_window_is_reported():  # DF-330
    from datetime import date

    from check import stale_exceptions

    settings = {
        "tool": {
            "uv": {
                "exclude-newer": "14 days",
                "exclude-newer-package": {
                    "certifi": "2026-10-21T00:00:00Z",
                    "fresh": "2026-11-01T00:00:00Z",
                },
            }
        }
    }
    assert stale_exceptions(settings, date(2026, 11, 3)) == []
    stale = stale_exceptions(settings, date(2026, 11, 4))
    assert len(stale) == 1 and "certifi" in stale[0] and "remove" in stale[0]


def test_no_exceptions_means_nothing_to_report():
    from datetime import date

    from check import stale_exceptions

    assert stale_exceptions({"tool": {"uv": {"exclude-newer": "14 days"}}}, date.today()) == []
