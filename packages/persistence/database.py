"""Explicit, secret-file based access to disposable I-01B databases only."""

import os
import re
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

from packages.shared.environment import development_environment

STRICT_SQL_MODE = (
    "STRICT_ALL_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,"
    "NO_ENGINE_SUBSTITUTION,ONLY_FULL_GROUP_BY"
)


class DatabaseSafetyError(ValueError):
    """Safe-to-display configuration error; never include the connection URL."""


def database_engine() -> Engine:
    environment = development_environment()
    secret = os.environ.get("DATABASE_URL_FILE")
    schema = os.environ.get("DATABASE_SCHEMA", "")
    if not secret or not re.fullmatch(rf"labsafe_{environment}_[a-z0-9_]+", schema):
        raise DatabaseSafetyError(
            "Set DATABASE_URL_FILE and DATABASE_SCHEMA=labsafe_<APP_ENV>_<name>"
        )
    if len(schema) > 64:
        raise DatabaseSafetyError("DATABASE_SCHEMA must not exceed 64 characters")
    try:
        raw = Path(secret).read_text(encoding="utf-8").strip()
        url = make_url(raw)
    except (OSError, ValueError, UnicodeError, ArgumentError):
        raise DatabaseSafetyError("Cannot read a valid database URL from secret file") from None
    if (
        url.drivername != "mysql+pymysql"
        or url.database != schema
        or not url.username
        or not url.password
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or url.query
    ):
        raise DatabaseSafetyError(
            "Require authenticated loopback mysql+pymysql URL, confirmed schema, no query options"
        )
    engine = create_engine(
        url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"charset": "utf8mb4", "connect_timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def configure_session(connection, _record):
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION time_zone = '+00:00'")
            cursor.execute("SET SESSION sql_mode = %s", (STRICT_SQL_MODE,))
            cursor.execute("SET SESSION foreign_key_checks = 1")
            cursor.execute("SET SESSION unique_checks = 1")
            cursor.execute("SELECT VERSION()")
            version = cursor.fetchone()[0]
            match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
            if (
                "mariadb" in version.lower()
                or not match
                or tuple(map(int, match.groups())) < (8, 0, 16)
                or int(match.group(1)) != 8
            ):
                raise DatabaseSafetyError("I-01B requires MySQL 8.x, version >= 8.0.16")

    return engine
