import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from uuid import uuid4

import pytest
from alembic import command
from redis import Redis
from redis.exceptions import ConnectionError
from sqlalchemy import inspect, text

from apps.api.app.main import create_app
from packages.application.identity import IdentityApplication
from packages.application.rate_limit import RateLimits
from packages.persistence.cli import migration_config
from packages.persistence.database import database_engine
from packages.persistence.schema import verify_schema
from packages.persistence.security import SessionService
from tests.persistence.factories import insert
from tests.persistence.test_security_mysql import create_tenant


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
    command.upgrade(config, "0001_initial")
    # Verify the additive report migration against a populated old schema.
    tenant_id, user_id, export_id = str(uuid4()), str(uuid4()), str(uuid4())
    with engine.begin() as connection:
        insert(connection, "tenants", id=tenant_id)
        insert(connection, "users", id=user_id, tenant_id=tenant_id)
        insert(
            connection,
            "report_exports",
            id=export_id,
            tenant_id=tenant_id,
            requested_by=user_id,
            snapshot='{"preserve":"frozen input"}',
        )
        original = dict(
            connection.execute(text("SELECT * FROM report_exports WHERE id=:id"), {"id": export_id})
            .mappings()
            .one()
        )
    command.upgrade(config, "head")
    with engine.begin() as connection:
        upgraded = dict(
            connection.execute(text("SELECT * FROM report_exports WHERE id=:id"), {"id": export_id})
            .mappings()
            .one()
        )
        assert {key: upgraded[key] for key in original} == original
        assert upgraded["object_version"] is upgraded["size_bytes"] is None
        connection.execute(text("DELETE FROM report_exports WHERE id=:id"), {"id": export_id})
        connection.execute(text("DELETE FROM users WHERE id=:id"), {"id": user_id})
        connection.execute(text("DELETE FROM tenants WHERE id=:id"), {"id": tenant_id})
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


@pytest.fixture(scope="module")
def isolated_redis(tmp_path_factory):
    if os.getenv("CI") == "true" and os.getenv("LABSAFE_CI_REDIS_PORT"):
        client = Redis(
            host="127.0.0.1", port=int(os.environ["LABSAFE_CI_REDIS_PORT"]), socket_timeout=2
        )
        assert client.ping()
        yield client
        client.close()
        return
    executable = os.getenv("LABSAFE_TEST_REDIS_SERVER") or shutil.which("redis-server")
    if not executable:
        pytest.fail("Set LABSAFE_TEST_REDIS_SERVER to start a disposable Redis child")
    folder = tmp_path_factory.mktemp("redis")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    child = subprocess.Popen(
        [
            executable,
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(folder),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    client = Redis(host="127.0.0.1", port=port, socket_timeout=2)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("Disposable Redis failed to start")
            try:
                if client.ping():
                    break
            except ConnectionError:
                time.sleep(0.05)
        assert client.info("server")["process_id"] == child.pid
        yield client
    finally:
        client.close()
        child.terminate()  # Only the child this fixture created; never a discovered service.
        child.wait(timeout=10)


@pytest.fixture
def identity(database, isolated_redis):
    tenant = create_tenant(database, "identity")
    service = IdentityApplication(
        database, SessionService(secrets.token_bytes(32)), RateLimits(isolated_redis), "test"
    )
    app = create_app(identity=service, public_origin="https://labsafe.test")
    return tenant, service, app
