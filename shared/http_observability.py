"""HTTP 请求号与稳定错误码，不记录请求正文或敏感数据。"""

from __future__ import annotations

import contextvars
import re
import secrets


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "clawworker_request_id", default="",
)


def request_id_from_header(value: str = "") -> str:
    candidate = (value or "").strip()
    return candidate if _REQUEST_ID_RE.fullmatch(candidate) else secrets.token_hex(10)


def set_request_id(value: str):
    return _current_request_id.set(value)


def reset_request_id(token) -> None:
    _current_request_id.reset(token)


def current_request_id() -> str:
    return _current_request_id.get()


def error_code_for_status(status_code: int) -> str:
    if status_code < 400:
        return ""
    return {
        400: "bad_request", 401: "auth_required", 403: "forbidden",
        404: "not_found", 409: "conflict", 413: "payload_too_large",
        422: "validation_failed", 429: "rate_limited", 502: "upstream_unavailable",
        503: "service_unavailable", 504: "upstream_timeout",
    }.get(status_code, "internal_error" if status_code >= 500 else "request_failed")


def attach_response_headers(response, request_id: str):
    response.headers["X-Clawworker-Request-ID"] = request_id
    error_code = error_code_for_status(response.status_code)
    if error_code:
        response.headers["X-Clawworker-Error-Code"] = error_code
    return response
