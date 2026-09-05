from __future__ import annotations

from .config import Settings
from .django_auth import migrate_django
from .repository import PostgresRepository


def main() -> int:
    settings = Settings.from_env()
    settings.validate()
    PostgresRepository(settings.database_url).migrate()
    migrate_django(settings.database_url, settings.django_secret_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
