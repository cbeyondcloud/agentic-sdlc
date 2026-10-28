"""What an agent CLI process never inherits from the user's environment (M1-29 review I1,
M2-40, M2-57; DF-541).

Another provider's switch or credential would route the agent CLI around TokenLean, or
hand the agent a key: those are always withheld. So is any secret-named variable, unless
org policy lets it through (`sandbox.pass_env`: an SSH agent socket, an authenticated
proxy). The framework's own `AGENTIC_*` variables never pass. The provider prefixes are
data, kept inside `agentic.backends` (the engine names no provider).
"""

import os
from collections.abc import Collection

from agentic.sandbox.process import FRAMEWORK_ENV_PREFIX, SECRET_NAME

#: Provider switches and credentials: never inherited, whatever policy's pass_env says.
PROVIDER_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "GOOGLE_",
    "GCLOUD_",
    "CLOUDSDK_",
    "VERTEX_",
    "BEDROCK_",
    "OPENAI_",
    "GEMINI_",
    "CLAUDE_CODE_USE_",
    # internal overrides a parent agent session exports (one treats any base URL as the
    # vendor's own, so the CLI reaches past TokenLean to the vendor's API)
    "_CLAUDE_",
)
PROVIDER_NAMES = frozenset({"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS"})


def provider_switch(name: str) -> bool:
    """Whether `name` switches or authenticates a provider (never passed to an agent)."""
    upper = name.upper()
    return upper.startswith(PROVIDER_PREFIXES) or upper in PROVIDER_NAMES


def passes(name: str, pass_env: Collection[str]) -> bool:
    """Whether org policy's `pass_env` lets `name` through (case-blind on Windows, where
    names are): never a provider switch, never the framework's own variables."""
    if provider_switch(name) or name.upper().startswith(FRAMEWORK_ENV_PREFIX):
        return False
    fold = str.upper if os.name == "nt" else str
    return fold(name) in {fold(passed) for passed in pass_env}


def withheld(name: str, pass_env: Collection[str] = ()) -> bool:
    """Whether an agent CLI process must not inherit `name`: a provider switch always; a
    secret-named variable unless policy's `pass_env` lets it through."""
    if provider_switch(name):
        return True
    return bool(SECRET_NAME.search(name.upper())) and not passes(name, pass_env)
