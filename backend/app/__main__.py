from __future__ import annotations

import uvicorn

from app.infrastructure.config import load_app_settings
from app.infrastructure.persistence.database import require_database_password
from app.infrastructure.runtime.event_loop import (
    configure_asyncio_event_loop,
    uvicorn_loop,
)


def main() -> None:
    configure_asyncio_event_loop()
    settings = load_app_settings()
    require_database_password(settings)
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level="debug" if settings.log_level == "trace" else settings.log_level,
        loop=uvicorn_loop(),
        workers=1,
        access_log=False,
        timeout_graceful_shutdown=2,
    )


if __name__ == "__main__":
    main()
