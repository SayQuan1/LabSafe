import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from packages.persistence.bootstrap import PASSWORD_HASHER, ROLES, TenantInput, bootstrap_tenant
from packages.persistence.cli import main
from packages.persistence.database import DatabaseSafetyError

PASSWORD = "  Synthetic-Only-Password-中文  "


def inputs():
    return TenantInput(
        "test_" + uuid4().hex,
        "合成测试机构",
        "Asia/Shanghai",
        "  ＡＤＭＩＮ  ",
        "初始管理员",
        "I-01B synthetic verification",
    )


def test_bootstrap_hash_roles_and_audit_atomic(database):
    data = inputs()
    result = bootstrap_tenant(database, data, PASSWORD)
    with database.connect() as connection:
        user = (
            connection.execute(text("SELECT * FROM users WHERE id=:id"), {"id": result["admin_id"]})
            .mappings()
            .one()
        )
        assert user["username"] == "admin"
        assert PASSWORD_HASHER.verify(user["password_hash"], PASSWORD)
        assert not PASSWORD_HASHER.check_needs_rehash(user["password_hash"])
        assert set(connection.execute(text("SELECT code FROM roles")).scalars()) == set(ROLES)
        grants = connection.execute(
            text("SELECT scope_kind,laboratory_id FROM user_roles WHERE user_id=:id"),
            {"id": result["admin_id"]},
        ).all()
        assert grants == [("tenant", None)]
        audit = connection.execute(
            text("SELECT changes FROM audit_events WHERE tenant_id=:id"),
            {"id": result["tenant_id"]},
        ).scalar_one()
        assert json.loads(audit)["initial_admin_id"] == result["admin_id"]
        assert "password" not in audit.lower() and PASSWORD not in audit
    with pytest.raises(DatabaseSafetyError, match="already exists"):
        bootstrap_tenant(database, data, "Another-Password-Do-Not-Reset")
    with database.connect() as connection:
        unchanged = connection.execute(
            text("SELECT password_hash FROM users WHERE id=:id"), {"id": result["admin_id"]}
        ).scalar_one()
        assert unchanged == user["password_hash"]


def test_bootstrap_audit_failure_rolls_back_every_write(database):
    data = inputs()
    tables = ("tenants", "users", "roles", "user_roles", "audit_events")
    with database.connect() as connection:
        before = {
            table: connection.scalar(text(f"SELECT COUNT(*) FROM {table}")) for table in tables
        }

    def fail_audit(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO audit_events"):
            raise RuntimeError("synthetic audit failure")

    event.listen(database, "before_cursor_execute", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="synthetic audit failure"):
            bootstrap_tenant(database, data, PASSWORD)
    finally:
        event.remove(database, "before_cursor_execute", fail_audit)
    with database.connect() as connection:
        after = {
            table: connection.scalar(text(f"SELECT COUNT(*) FROM {table}")) for table in tables
        }
        assert before == after
        assert not connection.scalar(
            text("SELECT id FROM tenants WHERE code=:code"), {"code": data.code}
        )


def test_concurrent_bootstrap_creates_one_admin(database):
    data = inputs()

    def attempt(_):
        try:
            bootstrap_tenant(database, data, PASSWORD)
            return "created"
        except DatabaseSafetyError as error:
            assert "already exists" in str(error)
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(attempt, range(2))) == ["created", "duplicate"]
    with database.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM users u JOIN tenants t "
                    "ON u.tenant_id=t.id WHERE t.code=:code"
                ),
                {"code": data.code},
            )
            == 1
        )


def test_cli_password_file_and_readonly_verify(database, tmp_path, capsys):
    data = inputs()
    password_file = tmp_path / "one-time-password"
    password_file.write_bytes(PASSWORD.encode("utf-8"))
    assert (
        main(
            [
                "bootstrap-tenant",
                "--code",
                data.code,
                "--name",
                data.name,
                "--timezone",
                data.timezone,
                "--username",
                data.username,
                "--display-name",
                data.display_name,
                "--reason",
                data.reason,
                "--password-file",
                str(password_file),
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert PASSWORD not in output.out + output.err
    assert "password" not in output.out.lower()
    with database.connect() as connection:
        hashed = connection.scalar(
            text("SELECT password_hash FROM users WHERE id=:id"), {"id": result["admin_id"]}
        )
        assert PASSWORD_HASHER.verify(hashed, PASSWORD)
    assert main(["verify"]) == 0
    assert json.loads(capsys.readouterr().out)["errors"] == []
