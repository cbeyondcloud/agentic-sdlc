"""The OpenCode profile for the ACP backend (M2-57, D32 revised; spike S7, DF-124).

OpenCode 1.18.35 (MIT; signed on Windows) runs as `opencode acp --pure`. What S7 proved,
plus what the 1.18.35 source shows, applied to every run:

  * routing: one custom provider, `tokenlean` (OpenAI-compatible, `{gateway}/v1`), with
    the user's key and our `X-Agentic-*` headers, is the only provider enabled; the
    resolved model is the model AND the small model (its first call is a title call);
    the key is a file in the step home, deleted once the session is open (`secret_files`);
  * policy: every permission key is set, after a deny-all - a key no capability maps to
    is denied, which also hides its tool from the model; our shell policy becomes
    OpenCode's `bash` rules (last match wins: deny-all, allow, ask, deny); the same
    object is passed again as `OPENCODE_PERMISSION`, which OpenCode merges last; its
    matcher is case-sensitive outside Windows, so on a case-blind host (macOS) denies and
    asks are also written in other cases (`case_blind`);
  * nothing from the repository: `OPENCODE_DISABLE_PROJECT_CONFIG` skips the project's
    `opencode.json`, `.opencode/` folders (agents, commands, plugins) and instruction
    files - a planted file can add no MCP server, agent or instructions (DF-124) - and
    `--pure` loads no external plugins. OpenCode's newer internal plugin loader ignores
    both and still imports `.opencode/plugin(s)/*.js|ts` and `plugin(s)` packages from
    project config (found by the contract tests), so a tree carrying either is refused
    before OpenCode starts (`code_in_tree`), and the agent may never edit those files;
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

import itertools
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from agentic.backends.acp.profile import (
    AcpProfile,
    Launch,
    ShellRules,
    StepSetup,
    folds_case,
    spelled_out,
)
from agentic.backends.capabilities import CAPABILITIES

PROVIDER = "tokenlean"
KEY_FILE = "tokenlean.key"  # in the step home
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


#: What OpenCode would load from the repository (edit patterns, matched from the tree):
#: never written by the agent, so it cannot plant code for the next step.
_OWN_FILES = (
    ".opencode/*",
    "*/.opencode/*",
    "opencode.json",
    "*/opencode.json",
    "opencode.jsonc",
    "*/opencode.jsonc",
)
_CONFIG_NAMES = ("opencode.json", "opencode.jsonc")


def code_in_tree(tree: Path) -> list[str]:
    """The files in `tree` that OpenCode 1.18.35 would run as code whatever its config
    says: its internal plugin loader reads every `.opencode/` folder and project config
    from the working folder up to the repository root even with project config disabled
    and `--pure` (found by the contract tests) - plugin files in `.opencode/plugin(s)/`
    and `plugin(s)` entries (npm packages) in an `opencode.json[c]`. One reason each."""
    found: list[str] = []
    for folder in _up_to_repository(Path(tree)):
        for sub in ("plugin", "plugins"):
            plugins = folder / ".opencode" / sub
            if plugins.is_dir():
                found += [
                    f"{path}: an OpenCode plugin"
                    for path in sorted(plugins.iterdir())
                    if path.suffix in (".js", ".ts") and path.is_file()
                ]
        for config in [folder / n for n in _CONFIG_NAMES] + [
            folder / ".opencode" / n for n in _CONFIG_NAMES
        ]:
            try:
                text = config.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if why := _plugins_in(text):
                found.append(f"{config}: {why}")
    return found


def _plugins_in(text: str) -> str | None:
    """Why a project config would make OpenCode load plugins, or None: a non-empty
    top-level `plugin` or `plugins` value (DF-446). A config that is not JSONC is refused
    too - what OpenCode would make of it cannot be known (fail closed)."""
    try:
        data = json.loads(strip_jsonc(text))
    except ValueError:
        return "cannot be read as JSONC, so whether it loads OpenCode plugins is unknown"
    if not isinstance(data, dict):
        return None
    if any(data.get(key) not in (None, [], "") for key in ("plugin", "plugins")):
        return "loads OpenCode plugins (npm packages)"
    return None


def strip_jsonc(text: str) -> str:
    """`text` (JSON with comments, as OpenCode reads its config) as plain JSON: `//` and
    `/* */` comments and trailing commas removed, string contents untouched."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == '"':  # a string, escapes included, copied as it is
            end = i + 1
            while end < n and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            out.append(text[i : end + 1])
            i = end + 1
        elif text.startswith("//", i):
            newline = text.find("\n", i)
            i = n if newline < 0 else newline
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = n if close < 0 else close + 2
        else:
            if char in "}]":  # a trailing comma before it goes
                last = len(out) - 1
                while last >= 0 and out[last].isspace():
                    last -= 1
                if last >= 0 and out[last] == ",":
                    del out[last]
            out.append(char)
            i += 1
    return "".join(out)


def _up_to_repository(tree: Path) -> list[Path]:
    """`tree` and its parents up to the repository root (the first holding `.git`);
    every parent when it is in no repository - OpenCode then searches up to `/`."""
    folders = []
    for folder in [tree, *tree.parents]:
        folders.append(folder)
        if (folder / ".git").exists():
            break
    return folders


def _paths(glob: str) -> list[str]:
    """A deny-path glob as OpenCode edit patterns, matched against the path relative to
    the tree, where `*` also crosses `/`: `**/x` also covers `x` at the top."""
    found = [glob[3:], "*/" + glob[3:]] if glob.startswith("**/") else [glob]
    return [pattern.replace("**", "*") for pattern in found]


def case_blind(tree: Path) -> bool:
    """Whether OpenCode's rules must also be written in other cases for `tree`: its
    matcher is case-sensitive except on Windows (where it folds case itself), while a
    case-insensitive file system (macOS by default) takes `Infra/x` for `infra/x` and
    runs `GIT` as `git`."""
    return sys.platform != "win32" and folds_case(tree)


#: Case variants are combined for up to this many words or path segments of one rule;
#: a longer rule gets each case for the whole rule only.
_CASE_PARTS = 3


def _capitalised(part: str) -> str:
    lower = part.lower()
    at = next((i for i, char in enumerate(lower) if char.isalpha()), None)
    return lower if at is None else lower[:at] + lower[at].upper() + lower[at + 1 :]


def _cased(text: str, sep: str, *, flags: bool = False) -> list[str]:
    """`text` as written, then with each of its parts (split on `sep`) also lower-case,
    upper-case and capitalised, in every combination. With `flags`, a part starting with
    `-` keeps its case: `-x` and `-X` are different options. Not every spelling of a
    long name (`gIt`): the matcher has no character classes, so this narrows the gap
    on a case-blind host but is no boundary - srt is (M2-41)."""
    parts = text.split(sep)
    cases = (str.lower, str.upper, _capitalised)
    forms = [
        [part, *(() if flags and part.startswith("-") else (case(part) for case in cases))]
        for part in parts
    ]
    if sum(len(set(form)) > 1 for form in forms) <= _CASE_PARTS:
        combinations = list(itertools.product(*forms))
    else:  # each case for the whole rule
        combinations = [[form[min(i, len(form) - 1)] for form in forms] for i in range(4)]
    return list(dict.fromkeys(sep.join(combination) for combination in combinations))


def opencode_permissions(
    tools: Sequence[str], rules: ShellRules, *, case_blind: bool = False
) -> dict:
    """OpenCode's `permission` object for an agent with these capabilities and rules.
    `case_blind` (see `case_blind()`): every deny and ask is also written in other cases;
    allows never are, so an unlisted spelling still falls to the deny-all."""

    def paths(patterns) -> list[str]:
        return [v for p in patterns for v in (_cased(p, "/") if case_blind else [p])]

    mapped = CAPABILITIES["opencode"].tools
    allowed = {key for capability in tools for key in mapped.get(capability, ())}
    perms: dict = {"*": "deny"}
    for key in PERMISSION_KEYS:
        if key not in allowed:
            perms[key] = "deny"
        elif key == "read":
            perms[key] = {
                variant: action
                for pattern, action in _READ.items()
                for variant in (paths([pattern]) if action == "deny" else [pattern])
            }
        elif key == "edit":
            perms[key] = {"*": "allow"}
            for glob in rules.deny_paths:
                perms[key].update({pattern: "deny" for pattern in paths(_paths(glob))})
            perms[key].update({pattern: "deny" for pattern in paths(_OWN_FILES)})
        elif key == "bash":
            perms[key] = _bash(rules, case_blind=case_blind)
        else:
            perms[key] = "allow"
    return perms


def _bash(rules: ShellRules, *, case_blind: bool = False) -> dict:
    """OpenCode's `bash` rules: the last match wins, so deny-all first, then allow, ask
    and deny, each moved to the end when written again - an ask or deny always lands
    after any broader allow it narrows. `x *` matches `x` and `x ...`; a deny-anywhere
    rule `p w` becomes `p *w*` (the words anywhere after the program)."""

    def cased(rule: str) -> list[str]:
        spelled = spelled_out(rule)
        if not case_blind:
            return spelled
        return [v for s in spelled for v in _cased(s, " ", flags=True)]

    ordered = [("*", "deny")]
    ordered += [(f"{p} *", "allow") for rule in rules.allow for p in spelled_out(rule)]
    ordered += [(f"{p} *", "ask") for rule in rules.ask for p in cased(rule)]
    ordered += [(f"{p} *", "deny") for rule in rules.deny for p in cased(rule)]
    for rule in rules.deny_anywhere:
        for spelled in cased(rule):
            program, _, words = spelled.partition(" ")
            ordered.append((f"{program} *{words}*" if words else f"{program} *", "deny"))
    bash: dict = {}
    for pattern, action in ordered:
        bash.pop(pattern, None)
        bash[pattern] = action
    return bash


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
                    # read from a file in the step home while OpenCode loads its config,
                    # never put in its environment (which its shell commands inherit);
                    # the runner deletes the file once the session is open (secret_files)
                    "apiKey": f"{{file:{(setup.home / KEY_FILE).as_posix()}}}",
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
        "permission": opencode_permissions(
            agent.tools, setup.rules, case_blind=case_blind(setup.tree)
        ),
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
        },
        files={KEY_FILE: setup.api_key},
    )


OPENCODE = AcpProfile(
    name="opencode",
    # In ACP mode OpenCode serves its own HTTP API and calls it: on loopback only (mDNS
    # or a config could bind every interface), on the port the runner gives it, called
    # directly - the sandbox's proxy takes everything else, a gateway on 127.0.0.1 too
    command=("opencode", "acp", "--pure", "--hostname", "127.0.0.1", "--mdns=false"),
    own_server=("--port", "127.0.0.1"),
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
        "https_proxy",  # what Linux and macOS users often set instead (DF-449)
        "http_proxy",
        "no_proxy",
    ),
    session_cost=True,
    refuse_tree=code_in_tree,
    secret_files=(KEY_FILE,),
)
