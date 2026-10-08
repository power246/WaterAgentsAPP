"""Local Host and browser-origin boundary for API mutations."""

from ipaddress import ip_address
from urllib.parse import parse_qsl, urlsplit

from fastapi.exceptions import RequestValidationError
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.http.errors import UnknownQueryParametersError
from app.application.settings_admin.errors import LocalMutationRejectedError


def _loopback(host: str | None) -> bool:
    if host is None:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _authority(value: str) -> tuple[str, int | None] | None:
    try:
        parsed = urlsplit("//" + value)
        if (
            parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or not parsed.hostname
        ):
            return None
        return parsed.hostname.casefold(), parsed.port
    except ValueError:
        return None


def _origin(value: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
        ):
            return None
        return parsed.scheme, parsed.hostname.casefold(), parsed.port
    except ValueError:
        return None


def _json_error() -> RequestValidationError:
    return RequestValidationError([{
        "type": "model_type", "loc": ("body",),
        "msg": "Expected application/json.", "input": None,
    }], body=None)


class LocalMutationGuardMiddleware:
    def __init__(self, app: ASGIApp, *, bind_host: str, bind_port: int) -> None:
        if not _loopback(bind_host):
            raise ValueError("the API must bind to a loopback host")
        self.app = app
        self.bind_host = bind_host.casefold()
        self.bind_port = bind_port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not scope.get("path", "").startswith("/api/")
            or scope["method"].upper() not in {"POST", "PUT", "PATCH", "DELETE"}
        ):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        authority = _authority(headers.get("host", ""))
        server = scope.get("server")
        testserver = bool(
            server and server[0] == "testserver"
            and authority in {None, ("testserver", None)}
        )
        if testserver and authority is None:
            authority = ("testserver", None)
        if not testserver and (
            authority is None or not _loopback(authority[0])
            or authority[1] != self.bind_port
        ):
            raise LocalMutationRejectedError()
        method = scope["method"].upper()
        if scope.get("query_string"):
            fields = tuple(dict.fromkeys(
                key for key, _ in parse_qsl(
                    scope["query_string"].decode("latin-1"), keep_blank_values=True,
                )
            ))
            raise UnknownQueryParametersError(fields)
        if headers.get("forwarded") or headers.get("x-forwarded-host"):
            raise LocalMutationRejectedError()
        if headers.get("sec-fetch-site", "").casefold() == "cross-site":
            raise LocalMutationRejectedError()
        origin_header = headers.get("origin")
        if origin_header is not None:
            origin = _origin(origin_header)
            if origin is None or origin != (
                scope.get("scheme", "http"), authority[0], authority[1],
            ):
                raise LocalMutationRejectedError()
        else:
            client = scope.get("client")
            if client is None or not _loopback(client[0]):
                raise LocalMutationRejectedError()
        content_type = headers.get("content-type")
        if content_type is not None and content_type.split(";", 1)[0].strip().casefold() != "application/json":
            raise _json_error()
        body_expected = (
            method in {"PUT", "PATCH"}
            or scope["path"] == "/api/analysis/runs"
            or headers.get("transfer-encoding") is not None
            or headers.get("content-length", "0") != "0"
        )
        if body_expected and content_type is None:
            raise _json_error()
        await self.app(scope, receive, send)
