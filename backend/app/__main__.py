from __future__ import annotations

import uvicorn

from app.infrastructure.config import AppSettings


def main() -> None:
    settings = AppSettings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        workers=1,
    )


if __name__ == "__main__":
    main()
