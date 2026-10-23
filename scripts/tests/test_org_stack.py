"""The local org stack (M2-39, deploy/org-local): control plane, its Postgres, Grafana and the
gateway's LITE stack. These checks are static; the live up / smoke / down run is recorded
in the track's output (it needs a container engine and the gateway's checkout).

Review Focus 6: images pinned by tag and digest, engine-neutral scripts in bash and
PowerShell that always name the project, LF shell scripts, no secret on a command line,
SELinux labels on bind mounts, and the gateway's checkout only ever read."""

import importlib.util
import re
import sys
from pathlib import Path

from agentic.yamlio import load_yaml

ROOT = Path(__file__).resolve().parents[2]
STACK = ROOT / "deploy" / "org-local"
COMPOSE = STACK / "docker-compose.yml"
SCRIPTS = ("up", "down", "smoke")
DIGEST = re.compile(r"^[a-z0-9./_-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")
#: The gateway's LITE dashboard set (spec 25): Per Call needs Langfuse, Tenant Overview is
#: replaced by the framework's Team view, Pitch Comparison is commercial.
LITE_SET = {
    "live",
    "requests",
    "billing",
    "latency-breakup",
    "sla",
    "trust-safety",
    "trends",
    "quarterly",
}


def _check_module():
    spec = importlib.util.spec_from_file_location("_org_check", ROOT / "scripts" / "check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_org_check", module)
    spec.loader.exec_module(module)
    return module


def _compose() -> dict:
    data, _ = load_yaml(COMPOSE.read_text(encoding="utf-8"))
    return data


def _text(name: str) -> str:
    return (STACK / name).read_text(encoding="utf-8")


def test_compose_services_and_networks():
    compose = _compose()
    assert compose["name"] == "agentic-org"
    assert set(compose["services"]) == {"cp-postgres", "cp-init", "control-plane", "grafana"}
    assert compose["networks"]["gateway"] == {"external": True, "name": "tokenlean-lite_default"}
    # only the services that must reach the gateway's database join its network
    joined = {n for n, s in compose["services"].items() if "gateway" in s.get("networks", [])}
    assert joined == {"cp-postgres", "grafana"}


def test_images_pinned_by_tag_and_digest():
    services = _compose()["services"]
    for name, service in services.items():
        if service["image"] == "agentic-control-plane:local":  # built here, never pulled
            assert "build" in service or service.get("pull_policy") == "never", name
            continue
        assert DIGEST.match(service["image"]), (name, service["image"])
    assert services["cp-postgres"]["image"] == _check_module().POSTGRES_IMAGE
    for line in (ROOT / "control-plane" / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if line.startswith("FROM "):
            assert DIGEST.match(line.split()[1]), line


def test_published_ports_bind_to_this_machine_by_default():
    for name, service in _compose()["services"].items():
        for port in service.get("ports", []):
            assert port.startswith("${ORG_BIND:-127.0.0.1}:"), (name, port)


def test_scripts_exist_in_bash_and_powershell():
    for script in SCRIPTS:
        assert (STACK / f"{script}.sh").is_file(), script
        assert (STACK / f"{script}.ps1").is_file(), script


def test_scripts_name_the_project_and_engine():
    lib_sh, lib_ps1 = _text("_lib.sh"), _text("_lib.ps1")
    assert 'compose -p "$ORG_PROJECT"' in lib_sh and "ORG_PROJECT=agentic-org" in lib_sh
    assert "compose -p $script:OrgProject" in lib_ps1 and "'agentic-org'" in lib_ps1
    assert "CONTAINER_ENGINE" in lib_sh and "CONTAINER_ENGINE" in lib_ps1
    for script in SCRIPTS:
        for suffix in ("sh", "ps1"):
            text = _text(f"{script}.{suffix}")
            assert "_lib." in text, (script, suffix)
            # every compose call goes through the library, which names the project
            assert not re.search(r"\b(docker|podman)\s+compose\b", text), (script, suffix)


def test_shell_scripts_are_lf():
    for path in [*STACK.rglob("*.sh"), STACK / ".env.org.template"]:
        assert b"\r" not in path.read_bytes(), path


def test_no_secret_on_a_command_line():
    for name, service in _compose()["services"].items():
        for key in ("command", "entrypoint"):
            assert "PASSWORD" not in str(service.get(key, "")), (name, key)
    for path in [*STACK.glob("*.sh"), *STACK.glob("*.ps1"), *STACK.rglob("init.sh")]:
        text = path.read_text(encoding="utf-8")
        # credentials reach curl only through header files, never as arguments
        assert not re.search(r"curl[^\n]*\s-u\s", text), path
        assert "-Credential" not in text, path
        assert not re.search(r"PGPASSWORD=\S", text), path


def test_secrets_are_generated_into_a_git_ignored_file():
    template = _text(".env.org.template")
    secrets = [line for line in template.splitlines() if "PASSWORD=" in line]
    assert secrets and all(line.endswith("=") for line in secrets), secrets
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "deploy/org-local/.env.org" in ignored
    assert "deploy/org-local/.state/" in ignored


def test_bind_mounts_carry_selinux_label():
    """DF-162 M11: Podman on an SELinux host refuses an unlabelled bind mount."""
    for name, service in _compose()["services"].items():
        for volume in service.get("volumes", []):
            source = volume.split(":", 1)[0]
            if source.startswith((".", "/", "$")):
                assert volume.rsplit(":", 1)[1] in ("ro,z", "z"), (name, volume)


def test_tokenlean_repo_only_read():
    """The gateway's checkout is read, never written: compose mounts nothing from it (its
    dashboards are copied into our own state folder) and no script writes into it. Its own
    `up` writes only its git-ignored .env.lite and .state (ruling S1)."""
    assert "TOKENLEAN_DIR" not in COMPOSE.read_text(encoding="utf-8")
    for suffix in ("sh", "ps1"):
        for script in (*SCRIPTS, "_lib"):
            for line in _text(f"{script}.{suffix}").splitlines():
                if "TOKENLEAN_DIR" in line or "TlDir" in line or "LITE_DIR" in line:
                    assert not re.search(r">\s*\"?\$", line), line
                    assert "Set-Content" not in line and "Out-File" not in line, line


def test_gateway_dashboards_are_the_lite_set():
    found_sh = re.search(r"ORG_LITE_DASHBOARDS=\"([^\"]+)\"", _text("_lib.sh"))
    found_ps1 = re.search(r"\$script:OrgLiteDashboards = @\(([^)]+)\)", _text("_lib.ps1"))
    assert found_sh and set(found_sh[1].split()) == LITE_SET
    assert found_ps1 and {n.strip(" '") for n in found_ps1[1].split(",")} == LITE_SET
