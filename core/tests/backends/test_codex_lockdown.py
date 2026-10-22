"""Tests for the codex backend's lock-down (M2-40, spike S3 decision 2): what one step's
`codex exec` is started with - its CODEX_HOME config, the same keys again as `-c`
overrides, the untrusted worktree, the environment and the command line. Pure: no Codex
binary is run, so these run on every OS."""

import json
import tomllib
from pathlib import Path

import pytest
from conformance import FakeContract, slice_

from agentic.backends.codex import (
    CODEX_VERSION,
    CodexError,
    CodexSettings,
    check_version,
    lock_down,
)
from agentic.backends.codex_rules import ShellRules
from agentic.engine.engine import StepContext
from agentic.transport import CallIdentity

KEY = "tl-" + "test-" + "key"  # built from pieces: not a real key shape
IDENTITY = CallIdentity(
    team="payments",
    user="dev@acme.example",
    repo="acme/app",
    pipeline="feature-delivery",
    run_id="r-1",
    step="develop",
    agent="developer@1.2.0",
    policy_version="v1",
    tier="balanced",
)
RULES = ShellRules(allow=("uv run pytest",), ask=("npm publish",), deny=("git push",))


def settings_for(tmp_path, **changes) -> CodexSettings:
    return CodexSettings(
        gateway_url="https://tokenlean.example",
        api_key=KEY,
        home_root=tmp_path / "homes",
        **changes,
    )


def launch_for(resolved, tmp_path, *, contract=None, srt=True, environ=None, **agent_changes):
    agent = resolved.model_copy(
        update={"backend": "codex", "temperature": 0.2, "effort": None, **agent_changes}
    )
    tree = tmp_path / "tree"
    tree.mkdir(exist_ok=True)
    step = StepContext("r-1", "develop", 2, agent, slice_(), contract)
    return lock_down(
        step,
        settings=settings_for(tmp_path),
        identity=IDENTITY,
        rules=RULES,
        cwd=tree,
        srt_boundary=srt,
        environ=environ if environ is not None else {"PATH": "/usr/bin", "HOME": "/home/dev"},
    )


def written(launch, instructions="Be the developer.") -> dict:
    launch.write(instructions)
    return tomllib.loads((launch.home / "config.toml").read_text(encoding="utf-8"))


def overrides(launch) -> dict[str, str]:
    argv, found = launch.argv, {}
    for i, arg in enumerate(argv):
        if arg == "-c":
            key, _, value = argv[i + 1].partition("=")
            found[key] = value
    return found


def test_untrusted_project_entry_is_always_written(resolved, tmp_path):  # Review Focus 1
    launch = launch_for(resolved, tmp_path)
    config = written(launch)
    tree = (tmp_path / "tree").resolve().as_posix()
    assert config["projects"] == {tree: {"trust_level": "untrusted"}}
    assert overrides(launch)[f"projects.{json.dumps(tree)}.trust_level"] == '"untrusted"'


def test_lockdown_keys_are_written_and_also_passed_as_overrides(resolved, tmp_path):
    launch = launch_for(resolved, tmp_path)
    config = written(launch)
    assert config["approval_policy"] == "never"
    assert config["sandbox_mode"] == "danger-full-access"  # srt is the boundary (S3 3)
    assert config["project_doc_max_bytes"] == 0
    assert config["web_search"] == "disabled"
    assert config["check_for_update_on_startup"] is False
    assert config["history"] == {"persistence": "none"}
    assert config["analytics"] == {"enabled": False}
    assert config["feedback"] == {"enabled": False}
    assert set(config["otel"].values()) == {"none"}
    assert config["shell_environment_policy"] == {"inherit": "core"}
    features = config["features"]
    assert not any(features[name] for name in ("plugins", "apps", "multi_agent", "memories"))
    passed = overrides(launch)
    for key in ("approval_policy", "sandbox_mode", "features.plugins", "analytics.enabled"):
        assert key in passed, key
    assert passed["model_provider"] == json.dumps(config["model_provider"])
    assert "developer_instructions" not in passed  # large; the untrusted worktree keeps it ours


def test_identity_headers_go_through_env_http_headers(resolved, tmp_path):
    launch = launch_for(resolved, tmp_path)
    provider = written(launch)["model_providers"][launch.provider]
    assert provider["base_url"] == "https://tokenlean.example/v1"
    assert provider["wire_api"] == "responses"
    assert launch.env[provider["env_key"]] == KEY
    headers = provider["env_http_headers"]
    for name, value in IDENTITY.headers().items():
        assert launch.env[headers[name]] == value
    trace = launch.env[headers["traceparent"]]
    assert trace.startswith("00-") and len(trace.split("-")[1]) == 32
    assert provider["request_max_retries"] == 1 and provider["stream_max_retries"] == 1


def test_provider_switches_and_secrets_are_blanked(resolved, tmp_path):
    hostile = {
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "sk-" + "hostile",
        "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
        "CODEX_API_KEY": "x",
        "CODEX_HOME": "/home/dev/.codex",
        "ANTHROPIC_BASE_URL": "http://elsewhere",
        "GITHUB_TOKEN": "ghp_" + "x" * 8,
        "LANG": "C.UTF-8",
    }
    env = launch_for(resolved, tmp_path, environ=hostile).env
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "ANTHROPIC_BASE_URL"):
        assert name not in env, name
    assert "GITHUB_TOKEN" not in env
    assert env["CODEX_HOME"] != "/home/dev/.codex"
    assert env["LANG"] == "C.UTF-8"


def test_command_line_matches_s3(resolved, tmp_path):
    launch = launch_for(resolved, tmp_path, contract=FakeContract())
    argv = launch.argv
    assert argv[:2] == ["codex", "exec"]
    for flag in ("--json", "--ephemeral", "--strict-config", "--skip-git-repo-check"):
        assert flag in argv
    assert argv[argv.index("-C") + 1] == (tmp_path / "tree").resolve().as_posix()
    assert argv[argv.index("-m") + 1] == resolved.model
    assert argv[argv.index("--output-schema") + 1] == (launch.home / "schema.json").as_posix()
    assert argv[argv.index("-o") + 1] == launch.last_message.as_posix()
    assert argv[-1] == "-"  # the prompt goes on stdin
    for forbidden in ("--ignore-rules", "--ignore-user-config", "--dangerously-bypass-approvals"):
        assert not any(arg.startswith(forbidden) for arg in argv)


def test_without_a_contract_there_is_no_output_schema(resolved, tmp_path):
    launch = launch_for(resolved, tmp_path, contract=None)
    assert "--output-schema" not in launch.argv and launch.schema is None


def test_the_schema_is_a_strict_document_envelope(resolved, tmp_path):
    launch = launch_for(resolved, tmp_path, contract=FakeContract())
    launch.write("x")
    schema = json.loads((launch.home / "schema.json").read_text(encoding="utf-8"))
    assert schema["required"] == ["document"] and schema["additionalProperties"] is False


def test_effort_is_sent_and_temperature_never(resolved, tmp_path):
    launch = launch_for(resolved, tmp_path, effort="high")
    config = written(launch)
    assert config["model_reasoning_effort"] == "high"
    assert "temperature" not in json.dumps(config)
    assert "model_reasoning_effort" not in written(launch_for(resolved, tmp_path, effort=None))


def test_catalog_written_and_referenced(resolved, tmp_path):
    launch = launch_for(resolved, tmp_path, tools=["read_files", "edit_files"])
    config = written(launch)
    catalog_path = Path(config["model_catalog_json"])
    assert catalog_path == launch.home / "models.json"
    [model] = json.loads(catalog_path.read_text(encoding="utf-8"))["models"]
    assert (model["slug"], model["apply_patch_tool_type"]) == (resolved.model, "freeform")


def test_rules_compiled_into_codex_home(resolved, tmp_path):
    launch = launch_for(resolved, tmp_path)
    launch.write("x")
    text = (launch.home / "rules" / "agentic.rules").read_text(encoding="utf-8")
    assert '"forbidden"' in text and '"prompt"' in text and '"allow"' in text
    launch_no_srt = launch_for(resolved, tmp_path, srt=False)
    launch_no_srt.write("x")
    text = (launch_no_srt.home / "rules" / "agentic.rules").read_text(encoding="utf-8")
    assert '"allow"' not in text


def test_developer_instructions_written_to_the_config_file(resolved, tmp_path):
    launch = launch_for(resolved, tmp_path)
    config = written(launch, instructions='Line one\nLine "two"')
    assert config["developer_instructions"] == 'Line one\nLine "two"'


def test_each_step_iteration_gets_its_own_home(resolved, tmp_path):
    launch = launch_for(resolved, tmp_path)
    assert launch.home == tmp_path / "homes" / "r-1" / "develop-2" / resolved.definition.name


def test_files_are_written_with_lf_line_endings(resolved, tmp_path):
    launch = launch_for(resolved, tmp_path, contract=FakeContract())
    launch.write("a\nb")
    for name in ("config.toml", "models.json", "schema.json", "rules/agentic.rules"):
        assert b"\r\n" not in (launch.home / name).read_bytes(), name


def test_version_check_accepts_the_pin_and_refuses_another(tmp_path):
    def runner(output):
        return lambda argv: output

    assert check_version(("codex",), run=runner(f"codex-cli {CODEX_VERSION}\n")) == CODEX_VERSION
    with pytest.raises(CodexError, match="0.159.0") as found:
        check_version(("codex",), run=runner("codex-cli 0.159.0\n"))
    assert CODEX_VERSION in str(found.value)
    with pytest.raises(CodexError, match="not found"):
        check_version(("codex",), run=lambda argv: (_ for _ in ()).throw(FileNotFoundError()))
