from collections.abc import Callable
from uuid import UUID

from pydantic import TypeAdapter
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.http.errors import handle_error, log_http
from app.application.correlation import request_id_context

_UUID = TypeAdapter(UUID)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, id_factory: Callable[[], UUID]) -> None:
        self.app = app
        self.id_factory = id_factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _UUID.validate_python(self.id_factory())
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_context.set(request_id)
        request = Request(scope, receive)
        status_code = None

        async def send_with_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message = {**message, "headers": list(message.get("headers", []))}
                MutableHeaders(scope=message)["X-Request-ID"] = str(request_id)
            await send(message)

        try:
            try:
                await self.app(scope, receive, send_with_id)
            except Exception as error:
                if status_code is not None:
                    raise
                response = await handle_error(request, error)
                await response(scope, receive, send_with_id)
        finally:
            if status_code is not None:
                log_http(request, "http_request_completed", status_code)
            request_id_context.reset(token)
