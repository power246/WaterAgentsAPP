"""Built frontend delivery stays below API and SSE routes."""

import asyncio

import httpx

from app.infrastructure.config import AppSettings
from app.main import create_app


def test_static_index_assets_navigation_api_and_head(tmp_path) -> None:
    (tmp_path / "index.html").write_text("<html>Water Agents</html>", encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index-123abcde.js").write_text("export const ready = true", encoding="utf-8")
    (assets / "plain.js").write_text("export const plain = true", encoding="utf-8")
    app = create_app(AppSettings(_env_file=None), frontend_dist=tmp_path)

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            return [
                await client.get("/", headers={"Accept": "text/html"}),
                await client.get("/runs/a/waterbodies/b", headers={"Accept": "text/html"}),
                await client.get("/assets/index-123abcde.js"),
                await client.get("/assets/plain.js"),
                await client.head("/assets/index-123abcde.js"),
                await client.head("/"),
                await client.get("/api/unknown", headers={"Accept": "text/html"}),
                await client.post("/api/analysis/runs/"),
                await client.get("/api/analysis/runs/not-a-uuid/events", headers={"Accept": "text/event-stream"}),
                await client.get("/missing.js", headers={"Accept": "text/html"}),
                await client.get("/health"),
            ]

    root, navigation, asset, plain_asset, asset_head, root_head, api_missing, api_post_missing, sse_route, missing, health = asyncio.run(request())
    assert root.status_code == navigation.status_code == 200
    assert root.headers["content-type"].startswith("text/html")
    assert root.headers["cache-control"] == "no-cache"
    assert navigation.text == root.text
    assert asset.status_code == asset_head.status_code == 200
    assert asset.headers["content-type"].split(";", 1)[0] in {"text/javascript", "application/javascript"}
    assert asset.headers["cache-control"].endswith("immutable")
    assert plain_asset.headers["cache-control"] == "no-cache"
    assert asset_head.content == root_head.content == b""
    assert root_head.status_code == 200
    assert api_missing.status_code == 404 and api_missing.headers["content-type"].startswith("application/json")
    assert api_post_missing.status_code == 404 and api_post_missing.json()["error"]["code"] == "not_found"
    assert sse_route.status_code == 422 and sse_route.headers["content-type"].startswith("application/json")
    assert missing.status_code == 404
    assert health.headers["x-frontend-available"] == "true"


def test_missing_build_and_traversal_are_bounded(tmp_path) -> None:
    app = create_app(AppSettings(_env_file=None), frontend_dist=tmp_path)

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            return (
                await client.get("/", headers={"Accept": "text/html"}),
                await client.get("/health"),
                await client.get("/api/unknown", headers={"Accept": "text/html"}),
                await client.get("/assets/%2e%2e/secret", headers={"Accept": "text/html"}),
                await client.get("/missing"),
                await client.get("/health/"),
                await client.get("/missing.js", headers={"Accept": "text/html"}),
                await client.get("/history", headers={"Accept": "text/html"}),
            )

    missing, health, api_missing, traversal, unknown, health_slash, asset_missing, navigation = asyncio.run(request())
    assert missing.status_code == 503
    assert health.json() == {"status": "healthy"}
    assert health.headers["x-frontend-available"] == "false"
    assert api_missing.status_code == traversal.status_code == 404
    assert unknown.status_code == health_slash.status_code == asset_missing.status_code == 404
    assert navigation.status_code == 503
