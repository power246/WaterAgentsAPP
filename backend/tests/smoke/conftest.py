import asyncio

import httpx
import pytest

from scripts.session_sse_smoke import SmokeServer


@pytest.fixture(autouse=True)
def check_server_cleanup(monkeypatch):
    runs = []
    serve = SmokeServer.serve

    async def observe_serve(server, sockets=None):
        runs.append((server, asyncio.current_task(), tuple(sockets or ())))
        await serve(server, sockets=sockets)

    monkeypatch.setattr(SmokeServer, "serve", observe_serve)
    yield
    for server, task, sockets in runs:
        assert task.done() and not task.cancelled()
        assert not server.server_state.connections
        assert not server.server_state.tasks
        assert all(listener.fileno() == -1 for listener in sockets)
        for listener in getattr(server, "servers", ()):
            assert not listener.is_serving()
            assert not listener.sockets


@pytest.fixture(params=[False, True], ids=["native-disconnect", "connection-reset"])
def reset_stream_disconnect(request, monkeypatch):
    if not request.param:
        yield
        return

    read = httpx.Response.aiter_raw
    disconnected = []

    async def read_with_reset(response, *args, **kwargs):
        try:
            async for chunk in read(response, *args, **kwargs):
                yield chunk
        except (httpx.RemoteProtocolError, httpx.ReadError) as error:
            disconnected.append(response)
            raise httpx.ReadError("Connection reset by peer.", request=response.request) from error

    monkeypatch.setattr(httpx.Response, "aiter_raw", read_with_reset)
    yield
    assert disconnected
