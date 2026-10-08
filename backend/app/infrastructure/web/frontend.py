"""Serve the Vite build without changing API or SSE routing."""

import re
from pathlib import Path

from fastapi import HTTPException, Request
from starlette.responses import FileResponse, Response


DEFAULT_FRONTEND_DIST = Path(__file__).resolve().parents[4] / "frontend" / "dist"


class FrontendResponder:
    def __init__(self, dist: Path = DEFAULT_FRONTEND_DIST) -> None:
        self.dist = dist.resolve()

    @property
    def available(self) -> bool:
        return (self.dist / "index.html").is_file()

    async def respond(self, request: Request, path: str) -> Response:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404)
        if any(part in {".", ".."} or part.startswith(".") for part in path.split("/")):
            raise HTTPException(status_code=404)
        target = (self.dist / path).resolve()
        if not target.is_relative_to(self.dist):
            raise HTTPException(status_code=404)
        if path and target.is_file() and target != self.dist / "index.html":
            if target.suffix == ".map":
                raise HTTPException(status_code=404)
            hashed_asset = path.startswith("assets/") and re.search(
                r"-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$", target.name,
            ) is not None
            cache = "public, max-age=31536000, immutable" if hashed_asset else "no-cache"
            return FileResponse(target, headers={"Cache-Control": cache})
        if not self.available:
            raise HTTPException(status_code=503, detail="Frontend unavailable")
        if path and ("text/html" not in request.headers.get("accept", "").lower() or "." in Path(path).name):
            raise HTTPException(status_code=404)
        return FileResponse(self.dist / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})
