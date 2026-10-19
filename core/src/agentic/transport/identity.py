"""Who a model call is for: the ten `X-Agentic-*` identity headers (spec §14.2).

TokenLean stores allow-listed identity headers as usage tags, so every call can
be attributed to a team, user, repo, pipeline, run, step and agent. Values are
checked when the identity is built: a header value with a newline would let
text pick its own headers (header injection), and a non-ASCII one cannot be
sent at all.
"""

import re
from dataclasses import dataclass, fields

#: Header name for each identity field, in the order the spec lists them.
HEADER_NAMES = {
    "team": "X-Agentic-Team",
    "user": "X-Agentic-User",
    "repo": "X-Agentic-Repo",
    "pipeline": "X-Agentic-Pipeline",
    "run_id": "X-Agentic-Run-Id",
    "step": "X-Agentic-Step",
    "agent": "X-Agentic-Agent",
    "parent_agent": "X-Agentic-Parent-Agent",
    "tier": "X-Agentic-Tier",
    "policy_version": "X-Agentic-Policy-Version",
}
#: Printable ASCII, spaces allowed inside but not at either end, at most 256.
_VALUE = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]{0,254}[\x21-\x7e])?")


class IdentityError(ValueError):
    """An identity value that cannot be sent as a header. Names the field, never the value."""


@dataclass(frozen=True)
class CallIdentity:
    """The identity stamped on every model call of one agent in one run."""

    team: str
    user: str
    repo: str
    pipeline: str
    run_id: str
    step: str
    agent: str  # "<name>@<version>"
    policy_version: str
    tier: str | None = None
    parent_agent: str | None = None  # "<name>@<version>" of the spawning agent

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if value is None and field.default is None:
                continue
            if not isinstance(value, str) or not _VALUE.fullmatch(value):
                raise IdentityError(
                    f"identity field {field.name!r} must be 1-256 printable ASCII "
                    f"characters with no line breaks"
                )

    def headers(self) -> dict[str, str]:
        """The identity headers; the optional tier and parent agent only when set."""
        return {
            HEADER_NAMES[field.name]: getattr(self, field.name)
            for field in fields(self)
            if getattr(self, field.name) is not None
        }
