"""The control plane's wire models (SP15): one definition shared by the server and the CLI.

Pure pydantic. No HTTP client, no server library and no rule engine is imported here, so
the CLI can depend on it without pulling the server in (import contract)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

#: Every route lives under this prefix.
API_PREFIX = "/v1"
#: Control-plane bearer tokens start with this (opaque; only their sha256 is stored).
TOKEN_PREFIX = "acp_"
#: Policy-repo webhook tokens start with this (slice 1b).
WEBHOOK_TOKEN_PREFIX = "acw_"
#: The CLI sends its version in this header on every call (M2-08 reads it).
CLI_VERSION_HEADER = "X-Agentic-CLI-Version"
#: Errors are RFC 9457 problem documents with this media type.
PROBLEM_MEDIA_TYPE = "application/problem+json"


class RequestModel(BaseModel):
    """Base of every request body: an unknown field is refused, never ignored (a body can
    never smuggle a tenant or a role in under a name the server does not read)."""

    model_config = ConfigDict(extra="forbid")


class Problem(BaseModel):
    """An RFC 9457 problem document. `code` is a stable machine name
    (`auth.invalid_token`, ...); `hint` is a next step the CLI prints as is. A problem never
    echoes a submitted secret and never names another tenant."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str
    code: str
    hint: str | None = None
