from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

from .repository import ConflictError, NotFoundError


class AuthenticationBackend(Protocol):
    def register(self, *, email: str, password: str) -> dict[str, Any]: ...
    def create_session(self, *, email: str, password: str) -> tuple[dict[str, Any], str]: ...
    def get_session_user(self, token: str) -> dict[str, Any]: ...
    def delete_session(self, token: str) -> None: ...


def _database_settings(database_url: str) -> dict[str, Any]:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("Django authentication requires a PostgreSQL database URL")
    options: dict[str, str] = {}
    query = parse_qs(parsed.query)
    if query.get("sslmode"):
        options["sslmode"] = query["sslmode"][-1]
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
        "OPTIONS": options,
    }


def configure_django(database_url: str, secret_key: str | None = None) -> None:
    try:
        import django
        from django.conf import settings
    except ImportError as exc:
        raise RuntimeError("Authentication requires the 'django' service dependency") from exc

    if not settings.configured:
        settings.configure(
            AUTH_USER_MODEL="django_accounts.Account",
            DATABASES={"default": _database_settings(database_url)},
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django.contrib.sessions",
                "service.django_accounts",
            ],
            PASSWORD_HASHERS=["django.contrib.auth.hashers.PBKDF2PasswordHasher"],
            SECRET_KEY=secret_key or "video-editing-local-development-only",
            SESSION_ENGINE="django.contrib.sessions.backends.db",
            USE_TZ=True,
        )
        django.setup()


class DjangoAuthentication:
    """Django-backed accounts and database sessions behind the existing API."""

    def __init__(self, database_url: str, secret_key: str | None = None) -> None:
        configure_django(database_url, secret_key)

    @staticmethod
    def _public_user(user: Any) -> dict[str, str]:
        return {"id": str(user.pk), "email": user.email}

    def register(self, *, email: str, password: str) -> dict[str, str]:
        from django.db import IntegrityError
        from service.django_accounts.models import Account

        normalized = Account.objects.normalize_email(email).lower()
        try:
            user = Account.objects.create_user(email=normalized, password=password)
        except IntegrityError as exc:
            raise ConflictError("an account with that email already exists") from exc
        return self._public_user(user)

    def create_session(self, *, email: str, password: str) -> tuple[dict[str, str], str]:
        from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, authenticate
        from django.contrib.sessions.backends.db import SessionStore

        user = authenticate(username=email.strip().lower(), password=password)
        if user is None or not user.is_active:
            raise NotFoundError("invalid email or password")
        session = SessionStore()
        session[SESSION_KEY] = str(user.pk)
        session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
        session[HASH_SESSION_KEY] = user.get_session_auth_hash()
        session.set_expiry(timedelta(days=14))
        session.save()
        if not session.session_key:
            raise RuntimeError("Django did not create a session key")
        return self._public_user(user), session.session_key

    def get_session_user(self, token: str) -> dict[str, str]:
        from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY
        from django.contrib.auth.hashers import constant_time_compare
        from django.contrib.sessions.backends.db import SessionStore
        from service.django_accounts.models import Account

        session = SessionStore(session_key=token)
        user_id = session.get(SESSION_KEY)
        if not user_id:
            raise NotFoundError("session not found")
        try:
            user = Account.objects.get(pk=user_id, is_active=True)
        except Account.DoesNotExist as exc:
            session.delete(token)
            raise NotFoundError("session not found") from exc
        if not constant_time_compare(session.get(HASH_SESSION_KEY, ""), user.get_session_auth_hash()):
            session.delete(token)
            raise NotFoundError("session not found")
        return self._public_user(user)

    def delete_session(self, token: str) -> None:
        from django.contrib.sessions.backends.db import SessionStore

        SessionStore(session_key=token).delete(token)


def migrate_django(database_url: str, secret_key: str | None = None) -> None:
    configure_django(database_url, secret_key)
    from django.core.management import call_command

    call_command("migrate", interactive=False, verbosity=0)
