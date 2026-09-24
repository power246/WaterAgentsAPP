import argparse
import asyncio
from collections.abc import Sequence

from app.infrastructure.config.settings import (
    SettingsConfigurationError,
    load_app_settings,
)
from app.infrastructure.persistence.database import (
    DatabaseConfigurationError,
    DatabaseConnectionError,
    SchemaVersionError,
    check_database_schema,
    upgrade_database,
)
from app.infrastructure.runtime.event_loop import configure_asyncio_event_loop


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wateragents-db")
    parser.add_argument("command", choices=("upgrade", "check"))
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    configure_asyncio_event_loop()
    try:
        settings = load_app_settings()
        if arguments.command == "upgrade":
            revision = upgrade_database(settings)
            print(f"database schema upgraded to {revision}")
        else:
            revision = asyncio.run(check_database_schema(settings))
            print(f"database schema is current at {revision}")
    except (
        SettingsConfigurationError,
        DatabaseConfigurationError,
        DatabaseConnectionError,
        SchemaVersionError,
    ) as error:
        print(f"wateragents-db: {error}")
        return 1
    except Exception:
        # The CLI boundary must not print driver exception details because they can
        # contain connection fields. Operators get a stable, credential-safe failure.
        print("wateragents-db: database command failed")
        return 1
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
