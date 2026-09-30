"""Development database CLI; connection/password secrets never appear in arguments."""

import argparse
import getpass
import json
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.exc import SQLAlchemyError

from packages.persistence.database import DatabaseSafetyError, database_engine
from packages.persistence.schema import verify_schema


def migration_config():
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("upgrade", help="Apply head to a dedicated dev/test schema")
    commands.add_parser("downgrade", help="Destroy tables only in explicitly confirmed test schema")
    commands.add_parser("verify", help="Compare actual schema to frozen baseline; exit 1 on drift")
    bootstrap = commands.add_parser("bootstrap-tenant", help="Create tenant, six roles and admin")
    for field in ("code", "name", "timezone", "username", "display-name", "reason"):
        bootstrap.add_argument("--" + field, required=True)
    bootstrap.add_argument(
        "--password-file",
        type=Path,
        help="Read exact UTF-8 password bytes; no stripping or normalization",
    )
    arguments = parser.parse_args(argv)
    engine = None
    try:
        if arguments.command in {"upgrade", "downgrade"}:
            if arguments.command == "upgrade":
                command.upgrade(migration_config(), "head")
            else:
                command.downgrade(migration_config(), "base")
            print(json.dumps({"command": arguments.command, "status": "pass"}))
            return 0
        engine = database_engine()
        if arguments.command == "verify":
            with engine.connect() as connection:
                report = verify_schema(connection)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 1 if report["errors"] else 0
        from packages.persistence.bootstrap import TenantInput, bootstrap_tenant

        if arguments.password_file:
            # read_bytes avoids newline translation; passwords must remain unchanged.
            password = arguments.password_file.read_bytes().decode("utf-8")
        elif sys.stdin.isatty():
            password = getpass.getpass("Initial administrator password: ")
            if password != getpass.getpass("Confirm password: "):
                raise DatabaseSafetyError("Password confirmation does not match")
        else:
            raise DatabaseSafetyError("Interactive terminal or --password-file is required")
        inputs = TenantInput(
            arguments.code,
            arguments.name,
            arguments.timezone,
            arguments.username,
            arguments.display_name,
            arguments.reason,
        )
        print(json.dumps(bootstrap_tenant(engine, inputs, password)))
        return 0
    except DatabaseSafetyError as error:
        print(f"Database safety check failed: {error}", file=sys.stderr)
        return 2
    except (SQLAlchemyError, OSError, ValueError, RuntimeError):
        # Driver exceptions may carry connection credentials or bound values.
        print(
            "Database command failed. Check configuration/schema and private server logs; "
            "no partial DDL recovery or automatic account reset was attempted.",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
