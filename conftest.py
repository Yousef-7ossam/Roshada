"""Test bootstrap.

The suite runs against **PostgreSQL**, the same engine as development and
production. It used to force SQLite for speed, which meant the tests never
exercised the database the application actually runs on — exactly the gap that
lets an engine-specific bug reach production green.

pytest-django creates and drops a dedicated ``test_<DB_NAME>`` database, so a
run can never touch development data. Settings are pinned here rather than read
from ``.env`` so a run does not depend on a developer's local configuration.

Requires a reachable PostgreSQL server; ``DB_*`` values below can be overridden
from the environment for CI.
"""
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ["DJANGO_DEBUG"] = "True"
os.environ["DJANGO_SECRET_KEY"] = "test-only-secret-key-not-used-in-production"

# Connection details: overridable so CI can point at its own service container.
os.environ.setdefault("DB_NAME", "roshada")
os.environ.setdefault("DB_USER", "postgres")
os.environ.setdefault("DB_PASSWORD", "postgres")
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_PORT", "5432")

# A statement timeout is right in production and wrong in a test run, where a
# debugger breakpoint or a slow fixture would trip it.
os.environ["DB_STATEMENT_TIMEOUT_MS"] = "0"

# Keep token-expiry behaviour deterministic regardless of the local .env.
os.environ["AUTH_TOKEN_TTL_HOURS"] = "24"


def pytest_configure():
    """Use a cheap password hasher for the test run only.

    The authorization suite registers an account of every role for many of its
    cases, and PBKDF2 — correctly, deliberately slow in production — dominated
    the runtime. Nothing under test depends on the hash algorithm; the
    credential path (validation, authentication, token issue) is identical.
    Production settings are untouched: this applies only inside pytest.
    """
    from django.conf import settings
    settings.PASSWORD_HASHERS = [
        "django.contrib.auth.hashers.MD5PasswordHasher",
    ]
