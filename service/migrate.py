from __future__ import annotations

from .config import Settings
from .repository import PostgresRepository


def main() -> int:
    settings = Settings.from_env()
    settings.validate()
    PostgresRepository(settings.database_url).migrate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
