"""RFC 9457 problem documents for every error the API returns.

Every error, including a framework 404, a validation failure and an unhandled exception,
leaves as `application/problem+json` with a stable `code`. Nothing a caller submitted is
echoed (a body may hold a token), and a 500 carries no traceback."""

from __future__ import annotations

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentic.orgapi import PROBLEM_MEDIA_TYPE, Problem

log = logging.getLogger("agentic_cp")

#: The code for a framework HTTP error that no route raised as an ApiProblem.
_STATUS_CODES = {
    400: "bad_request",
    401: "auth.missing_token",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    422: "invalid_request",
    429: "rate.limited",
}


class ApiProblem(Exception):
    """Raised by routes and dependencies; rendered as a problem document."""

    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        *,
        hint: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"{status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail
        self.hint = hint
        self.headers = headers or {}


def not_found(what: str = "object") -> ApiProblem:
    """The one 404 for a missing object AND for another tenant's object (DR-5): the same
    status, code and detail, so existence never leaks across tenants."""
    return ApiProblem(404, "not_found", f"no such {what}")


def problem_response(
    status: int,
    code: str,
    detail: str,
    *,
    hint: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = Problem(
        title=HTTPStatus(status).phrase, status=status, detail=detail, code=code, hint=hint
    )
    return JSONResponse(
        body.model_dump(exclude_none=True),
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


def _field(loc: tuple) -> str:
    parts = [str(p) for p in loc if p not in ("body", "query", "path", "header")]
    return ".".join(parts) or "body"


async def _on_problem(request: Request, error: ApiProblem) -> JSONResponse:
    return problem_response(
        error.status, error.code, error.detail, hint=error.hint, headers=error.headers
    )


async def _on_http(request: Request, error: StarletteHTTPException) -> JSONResponse:
    code = _STATUS_CODES.get(error.status_code, "http_error")
    detail = error.detail if isinstance(error.detail, str) else HTTPStatus(error.status_code).phrase
    if error.status_code == 404:
        detail = "no such route or object"
    return problem_response(
        error.status_code, code, detail, headers=getattr(error, "headers", None)
    )


async def _on_validation(request: Request, error: RequestValidationError) -> JSONResponse:
    # Field names and rules only: `input` may be a secret the caller pasted.
    problems = []
    for item in error.errors():
        message = "unknown field" if item.get("type") == "extra_forbidden" else item.get("msg")
        problems.append(f"{_field(tuple(item.get('loc', ())))}: {message}")
    return problem_response(422, "invalid_request", "; ".join(problems) or "invalid request")


async def _on_unhandled(request: Request, error: Exception) -> JSONResponse:
    log.error("unhandled error on %s %s", request.method, request.url.path, exc_info=error)
    return problem_response(500, "internal", "internal error; the server log has the details")


def install_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiProblem, _on_problem)
    app.add_exception_handler(StarletteHTTPException, _on_http)
    app.add_exception_handler(RequestValidationError, _on_validation)
    app.add_exception_handler(Exception, _on_unhandled)
