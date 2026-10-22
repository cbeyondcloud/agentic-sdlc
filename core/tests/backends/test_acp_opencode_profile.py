"""The OpenCode profile (M2-57; spike S7, DF-124): our routing, model, shell policy and
lock-down compiled into OpenCode 1.18.35's own config and permission format. Pure data -
the contract tests run the real binary against what this produces."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from agentic.backends.acp.opencode import (
    OPENCODE,
    PERMISSION_KEYS,
    code_in_tree,
    configure,
    opencode_config,
    opencode_permissions,
)
from agentic.backends.acp.profile import ShellRules, StepSetup
from agentic.policy.model import ModelPrice

CODING = ["read_files", "search", "edit_files", "run_shell"]
RULES = ShellRules(
    allow=("pytest", "ruff"),
    ask=("npm publish",),
    deny=("git push", "git rebase -x|--exec"),
    deny_paths=("infra/**", "**/.env", "secrets.txt"),
)
HEADERS = {"X-Agentic-Run-Id": "r-1", "X-Agentic-Team": "payments"}
PRICE = ModelPrice(
    input_per_mtok=Decimal("3"),
    output_per_mtok=Decimal("15"),
    cache_read_per_mtok=Decimal("0.3"),
)


def setup_for(agent, *, price=PRICE, rules=RULES) -> StepSetup:
    return StepSetup(
        agent=agent,
        gateway_url="https://tokenlean.acme.internal/",
        api_key="tl-key",
        headers=HEADERS,
        rules=rules,
        price=price,
        home=Path("/state/home"),
        tree=Path("/tree"),
    )


@pytest.fixture
def coder(resolved):
    return resolved.model_copy(
        update={
            "backend": "opencode",
            "model": "openai/gpt-5-codex",
            "tools": CODING,
            "temperature": 0.2,
            "effort": None,
        }
    )


# ── permissions ───────────────────────────────────────────────────────────────


def test_every_permission_key_is_set_after_a_deny_all():
    perms = opencode_permissions(CODING, RULES)
    assert list(perms)[0] == "*" and perms["*"] == "deny"
    assert set(PERMISSION_KEYS) <= set(perms)


def test_keys_no_capability_maps_to_are_denied():
    perms = opencode_permissions(["read_files"], ShellRules())
    for key in ("bash", "edit", "glob", "grep", "task", "webfetch", "websearch", "skill"):
        assert perms[key] == "deny", key
    for key in ("lsp", "todowrite", "question", "doom_loop", "external_directory"):
        assert perms[key] == "deny", key
    assert perms["list"] == "allow"


def test_reads_are_allowed_except_env_files():
    read = opencode_permissions(["read_files"], ShellRules())["read"]
    assert list(read.items()) == [
        ("*", "allow"),
        ("*.env", "deny"),
        ("*.env.*", "deny"),
        ("*.env.example", "allow"),
    ]


def test_shell_rules_deny_all_then_allow_then_ask_then_deny_last():
    bash = opencode_permissions(CODING, RULES)["bash"]
    assert list(bash.items()) == [
        ("*", "deny"),
        ("pytest *", "allow"),
        ("ruff *", "allow"),
        ("npm publish *", "ask"),
        ("git push *", "deny"),
        ("git rebase -x *", "deny"),
        ("git rebase --exec *", "deny"),
    ]


def test_a_deny_overrides_an_allow_that_covers_it():
    bash = opencode_permissions(CODING, ShellRules(allow=("git",), deny=("git",)))["bash"]
    assert list(bash.items())[-1] == ("git *", "deny")


def test_denied_paths_become_edit_denies_matched_from_the_tree():
    edit = opencode_permissions(CODING, RULES)["edit"]
    assert list(edit.items())[:5] == [
        ("*", "allow"),
        ("infra/*", "deny"),
        (".env", "deny"),
        ("*/.env", "deny"),
        ("secrets.txt", "deny"),
    ]


def test_edit_is_denied_without_the_capability():
    assert opencode_permissions(["read_files", "run_shell"], RULES)["edit"] == "deny"


def test_the_agent_can_never_edit_what_opencode_would_load_as_code():
    edit = opencode_permissions(CODING, ShellRules())["edit"]
    for pattern in (".opencode/*", "*/.opencode/*", "opencode.json", "*/opencode.jsonc"):
        assert edit[pattern] == "deny", pattern


# ── code a tree would make OpenCode load (1.18.35 ignores the flag for these) ──


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("name", ["plugins/evil.js", "plugin/evil.ts"])
def test_a_plugin_file_in_the_tree_is_found(tmp_path, name):
    root = repo(tmp_path)
    write(root / ".opencode" / name, "export default {}")
    (found,) = code_in_tree(root)
    assert name.split("/")[1] in found


@pytest.mark.parametrize(
    "config",
    [
        "opencode.json",
        "opencode.jsonc",
        ".opencode/opencode.json",
    ],
)
def test_a_plugin_entry_in_a_project_config_is_found(tmp_path, config):
    root = repo(tmp_path)
    write(root / config, '{\n  // our plugins\n  "plugin": ["opencode-evil@1.0.0"]\n}\n')
    (found,) = code_in_tree(root)
    assert Path(config).name in found


def test_a_plugin_above_the_tree_inside_the_repository_is_found(tmp_path):
    root = repo(tmp_path)
    write(root / ".opencode" / "plugins" / "evil.js", "x")
    tree = root / "services" / "api"
    tree.mkdir(parents=True)
    assert len(code_in_tree(tree)) == 1


def test_nothing_above_the_repository_is_looked_at(tmp_path):
    write(tmp_path / ".opencode" / "plugins" / "outside.js", "x")
    root = repo(tmp_path)
    assert code_in_tree(root) == []


def test_a_project_config_without_plugins_is_fine(tmp_path):
    root = repo(tmp_path)
    write(root / "opencode.json", '{"model": "x/y", "mcp": {}, "instructions": ["A.md"]}')
    write(root / ".opencode" / "agents" / "build.md", "---\n---\nhi\n")
    assert code_in_tree(root) == []


def test_the_profile_refuses_such_a_tree():
    assert OPENCODE.refuse_tree is code_in_tree


# ── config ────────────────────────────────────────────────────────────────────


def test_the_only_provider_is_tokenlean_with_our_key_and_identity(coder):
    config = opencode_config(setup_for(coder))
    assert config["enabled_providers"] == ["tokenlean"]
    provider = config["provider"]["tokenlean"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "https://tokenlean.acme.internal/v1"
    assert provider["options"]["apiKey"] == "tl-key"
    assert provider["options"]["headers"] == HEADERS


def test_the_resolved_model_is_forced_for_the_small_model_too(coder):
    config = opencode_config(setup_for(coder))
    assert config["model"] == config["small_model"] == "tokenlean/gpt-5-codex"
    model = config["provider"]["tokenlean"]["models"]["gpt-5-codex"]
    assert model["tool_call"] is True and model["temperature"] is True


def test_policy_prices_are_the_model_cost_so_its_session_cost_is_ours(coder):
    model = opencode_config(setup_for(coder))["provider"]["tokenlean"]["models"]["gpt-5-codex"]
    assert model["cost"] == {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.0}
    assert model["limit"]["context"] > 0  # OpenCode reports session cost only with a limit


def test_an_unpriced_model_carries_no_cost(coder):
    model = opencode_config(setup_for(coder, price=None))["provider"]["tokenlean"]["models"]
    assert "cost" not in model["gpt-5-codex"]


def test_nothing_from_outside_can_add_code_or_text(coder):
    config = opencode_config(setup_for(coder))
    assert config["plugin"] == [] and config["instructions"] == [] and config["mcp"] == {}
    assert config["formatter"] is False and config["lsp"] is False
    assert config["snapshot"] is False and config["share"] == "disabled"
    assert config["autoupdate"] is False
    for name in ("plan", "general", "explore"):
        assert config["agent"][name] == {"disable": True}


def test_a_refused_ask_lets_the_agent_carry_on(coder):
    assert opencode_config(setup_for(coder))["experimental"]["continue_loop_on_deny"] is True


def test_turns_and_temperature_go_on_the_build_agent(coder):
    agent = coder.model_copy(update={"limits": coder.limits.model_copy(update={"max_turns": 7})})
    build = opencode_config(setup_for(agent))["agent"]["build"]
    assert build["steps"] == 7 and build["temperature"] == 0.2
    assert opencode_config(setup_for(agent))["default_agent"] == "build"


def test_no_temperature_and_no_turn_limit_set_nothing(coder):
    agent = coder.model_copy(
        update={"temperature": None, "limits": coder.limits.model_copy(update={"max_turns": None})}
    )
    assert opencode_config(setup_for(agent))["agent"]["build"] == {}


def test_the_permissions_go_in_the_config_and_on_top_of_everything(coder):
    launch = configure(setup_for(coder))
    config = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert config["permission"] == opencode_permissions(CODING, RULES)
    assert json.loads(launch.env["OPENCODE_PERMISSION"]) == config["permission"]
    assert list(json.loads(launch.env["OPENCODE_PERMISSION"])["bash"])[0] == "*"  # order kept


def test_project_config_is_never_loaded(coder):  # DF-124
    assert configure(setup_for(coder)).env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"


def test_the_profile_is_pinned_and_quiet():
    assert OPENCODE.name == "opencode"
    # its own server, which it calls over HTTP, on loopback only (never mDNS/0.0.0.0)
    assert OPENCODE.command == (
        "opencode",
        "acp",
        "--pure",
        "--hostname",
        "127.0.0.1",
        "--mdns=false",
    )
    assert OPENCODE.own_server == ("--port", "127.0.0.1")
    assert OPENCODE.version == "1.18.35"
    for switch in (
        "OPENCODE_DISABLE_AUTOUPDATE",
        "OPENCODE_DISABLE_MODELS_FETCH",
        "OPENCODE_DISABLE_LSP_DOWNLOAD",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS",
        "OPENCODE_DISABLE_CLAUDE_CODE",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS",
        "OPENCODE_DISABLE_SHARE",
    ):
        assert OPENCODE.quiet[switch] == "1", switch
    assert OPENCODE.egress_hosts == ("registry.npmjs.org",)
    assert OPENCODE.session_cost is True
    assert OPENCODE.auth_method is None
