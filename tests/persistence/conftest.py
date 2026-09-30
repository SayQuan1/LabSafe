import json
import os

import pytest
from alembic import command
from sqlalchemy import inspect, text

from packages.persistence.cli import migration_config
from packages.persistence.database import database_engine
from packages.persistence.schema import verify_schema


@pytest.fixture(scope="session")
def database():
    if os.environ.get("LABSAFE_MYSQL_INTEGRATION") != "1":
        pytest.skip("Run tools/database/run_mysql_tests.py for an isolated real MySQL database")
    if os.environ.get("LABSAFE_ALLOW_DESTRUCTIVE_DOWNGRADE") != os.environ.get("DATABASE_SCHEMA"):
        pytest.fail("Migration-cycle tests require explicit disposable schema confirmation")
    engine = database_engine()
    with engine.connect() as connection:
        assert inspect(connection).get_table_names() == [], "Integration tests require an empty DB"
    config = migration_config()
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE user_sentinel (id INT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO user_sentinel VALUES (7)"))
    with pytest.raises(RuntimeError, match="empty disposable"):
        command.upgrade(config, "head")
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT id FROM user_sentinel")) == 7
        connection.execute(text("DROP TABLE user_sentinel"))
    command.upgrade(config, "head")
    with engine.connect() as connection:
        report = verify_schema(connection)
        print(json.dumps(report, indent=2))
        assert report["errors"] == []
    confirmation = os.environ.pop("LABSAFE_ALLOW_DESTRUCTIVE_DOWNGRADE")
    try:
        with pytest.raises(RuntimeError, match="Destructive downgrade"):
            command.downgrade(config, "base")
    finally:
        os.environ["LABSAFE_ALLOW_DESTRUCTIVE_DOWNGRADE"] = confirmation
    with engine.connect() as connection:
        assert verify_schema(connection)["errors"] == []
    command.downgrade(config, "base")
    with engine.connect() as connection:
        assert inspect(connection).get_table_names() == ["alembic_version"]
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert verify_schema(connection)["errors"] == []
    yield engine
    engine.dispose()


@pytest.fixture
def transaction(database):
    with database.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()
