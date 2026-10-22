"""The OpenCode profile for the ACP backend (M2-57, D32 revised; spike S7, DF-124).

OpenCode 1.18.35 (MIT; signed on Windows) runs as `opencode acp --pure`. What S7 proved,
plus what the 1.18.35 source shows, applied to every run:

  * routing: one custom provider, `tokenlean` (OpenAI-compatible, `{gateway}/v1`), with
    the user's key and our `X-Agentic-*` headers, is the only provider enabled; the
    resolved model is the model AND the small model (its first call is a title call);
  * policy: every permission key is set, after a deny-all - a key no capability maps to
    is denied, which also hides its tool from the model; our shell policy becomes
    OpenCode's `bash` rules (last match wins: deny-all, allow, ask, deny); the same
    object is passed again as `OPENCODE_PERMISSION`, which OpenCode merges last;
  * nothing from the repository: `OPENCODE_DISABLE_PROJECT_CONFIG` skips the project's
    `opencode.json`, `.opencode/` folders (agents, commands, plugins) and instruction
    files - a planted file can add no MCP server, plugin or instructions (DF-124) - and
    `--pure` loads no external plugins;
  * nothing that runs code or phones out on its own: no formatter, LSP, snapshot,
    sharing, auto-update, model-list fetch or skills from other tools. One request
    cannot be switched off: OpenCode installs its plugin package from
    `registry.npmjs.org` into its config folder (detached; a failure is ignored), so
    that host is listed in `egress_hosts` and the sandbox blocks it;
  * cost: policy's prices are written as the model's cost, so the session cost OpenCode
    reports over ACP is every model call at our prices (its per-turn usage covers only
    the last call).

OpenCode has no sandbox of its own: `srt` (D38) is the boundary, and the ACP permission
callback is a second gate.
"""

import json
from collections.abc import Sequence

from agentic.backends.acp.profile import AcpProfile, Launch, ShellRules, StepSetup, spelled_out
from agentic.backends.capabilities import CAPABILITIES

PROVIDER = "tokenlean"
#: Every permission key OpenCode 1.18.35 asks about, in the order they are written.
PERMISSION_KEYS = (
    "read",
    "list",
    "glob",
    "grep",
    "edit",
    "bash",
    "task",
    "external_directory",
    "todowrite",
    "question",
    "webfetch",
    "websearch",
    "lsp",
    "doom_loop",
    "skill",
    "plan_enter",
    "plan_exit",
)
#: OpenCode's own defaults for reading secrets files, refused instead of asked.
_READ = {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"}
#: The context window OpenCode is told: it reports usage (and so cost) only with one.
CONTEXT_TOKENS = 200_000
QUIET = {
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
    "OPENCODE_DISABLE_SHARE": "1",
}


def _paths(glob: str) -> list[str]:
    """A deny-path glob as OpenCode edit patterns, matched against the path relative to
    the tree, where `*` also crosses `/`: `**/x` also covers `x` at the top."""
    found = [glob[3:], "*/" + glob[3:]] if glob.startswith("**/") else [glob]
    return [pattern.replace("**", "*") for pattern in found]


def opencode_permissions(tools: Sequence[str], rules: ShellRules) -> dict:
    """OpenCode's `permission` object for an agent with these capabilities and rules."""
    mapped = CAPABILITIES["opencode"].tools
    allowed = {key for capability in tools for key in mapped.get(capability, ())}
    perms: dict = {"*": "deny"}
    for key in PERMISSION_KEYS:
        if key not in allowed:
            perms[key] = "deny"
        elif key == "read":
            perms[key] = dict(_READ)
        elif key == "edit":
            perms[key] = {"*": "allow"}
            for glob in rules.deny_paths:
                perms[key].update({pattern: "deny" for pattern in _paths(glob)})
        elif key == "bash":
            bash = {"*": "deny"}
            for action, prefixes in (("allow", rules.allow), ("ask", rules.ask)):
                for rule in prefixes:
                    bash.update({f"{p} *": action for p in spelled_out(rule)})
            for rule in rules.deny:  # last, so a deny wins over an allow it covers
                for prefix in spelled_out(rule):
                    bash.pop(f"{prefix} *", None)
                    bash[f"{prefix} *"] = "deny"
            perms[key] = bash
        else:
            perms[key] = "allow"
    return perms


def model_name(model: str) -> str:
    """The name TokenLean routes on: what follows the provider (`openai/gpt-5` -> `gpt-5`)."""
    return model.partition("/")[2] or model


def opencode_config(setup: StepSetup) -> dict:
    """The inline config (`OPENCODE_CONFIG_CONTENT`): routing, model, policy, lock-down."""
    agent = setup.agent
    name = model_name(agent.model)
    model: dict = {
        "name": name,
        "tool_call": True,
        "temperature": True,
        "limit": {"context": CONTEXT_TOKENS, "output": 0},  # 0: OpenCode's own output cap
    }
    if setup.price is not None:
        price = setup.price
        model["cost"] = {
            "input": float(price.input_per_mtok),
            "output": float(price.output_per_mtok),
            "cache_read": float(_or(price.cache_read_per_mtok, price.input_per_mtok)),
            "cache_write": float(_or(price.cache_write_per_mtok, price.input_per_mtok)),
        }
    build: dict = {}
    if agent.limits.max_turns is not None:
        build["steps"] = agent.limits.max_turns
    if agent.temperature is not None:
        build["temperature"] = agent.temperature
    return {
        "provider": {
            PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "TokenLean",
                "options": {
                    "baseURL": setup.gateway_url.rstrip("/") + "/v1",
                    "apiKey": setup.api_key,
                    "headers": dict(setup.headers),
                },
                "models": {name: model},
            }
        },
        "enabled_providers": [PROVIDER],
        "model": f"{PROVIDER}/{name}",
        "small_model": f"{PROVIDER}/{name}",
        "default_agent": "build",
        "agent": {
            "build": build,
            "plan": {"disable": True},
            "general": {"disable": True},
            "explore": {"disable": True},
        },
        "permission": opencode_permissions(agent.tools, setup.rules),
        "plugin": [],
        "instructions": [],
        "mcp": {},
        "formatter": False,
        "lsp": False,
        "snapshot": False,
        "share": "disabled",
        "autoupdate": False,
        # an ask our callback refuses ends the turn otherwise; a rule deny never does
        "experimental": {"continue_loop_on_deny": True},
    }


def _or(value, default):
    return value if value is not None else default


def configure(setup: StepSetup) -> Launch:
    config = opencode_config(setup)
    return Launch(
        env={
            "OPENCODE_CONFIG_CONTENT": json.dumps(config),
            "OPENCODE_PERMISSION": json.dumps(config["permission"]),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        }
    )


OPENCODE = AcpProfile(
    name="opencode",
    command=("opencode", "acp", "--pure"),
    version="1.18.35",
    configure=configure,
    quiet=QUIET,
    egress_hosts=("registry.npmjs.org",),
    inherit=(
        "OPENCODE_GIT_BASH_PATH",  # Windows: the bash tool needs Git for Windows' bash
        "NODE_EXTRA_CA_CERTS",  # a corporate CA, to reach TokenLean
        "SSL_CERT_FILE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    ),
    session_cost=True,
)
