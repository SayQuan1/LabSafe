import pytest

from packages.persistence.schema import verify_schema


def test_full_schema(database):
    with database.connect() as connection:
        report = verify_schema(connection)
    assert report["errors"] == []
    assert report["tables"] == 43
    assert report["foreign_keys"] == 115


@pytest.mark.parametrize(
    "change,restore,expected_error",
    [
        (
            "ALTER TABLE tenants MODIFY name VARCHAR(199) NOT NULL",
            "ALTER TABLE tenants MODIFY name VARCHAR(200) NOT NULL",
            "tenants.name.type",
        ),
        (
            "ALTER TABLE tenants ALTER status SET DEFAULT 'suspended'",
            "ALTER TABLE tenants ALTER status SET DEFAULT 'active'",
            "tenants.status.default",
        ),
        (
            "ALTER TABLE roles DROP INDEX uq_roles_0",
            "ALTER TABLE roles ADD UNIQUE KEY uq_roles_0 (code)",
            "roles.index.uq_roles_0",
        ),
        (
            "ALTER TABLE colleges DROP FOREIGN KEY fk_colleges_0",
            "ALTER TABLE colleges ADD CONSTRAINT fk_colleges_0 FOREIGN KEY (tenant_id) "
            "REFERENCES tenants (id) ON DELETE RESTRICT ON UPDATE RESTRICT",
            "colleges.foreign_keys",
        ),
        (
            "ALTER TABLE task_runs ALTER CHECK ck_task_runs_0 NOT ENFORCED",
            "ALTER TABLE task_runs ALTER CHECK ck_task_runs_0 ENFORCED",
            "task_runs.ck_task_runs_0.enforced",
        ),
        (
            "ALTER TABLE roles ADD INDEX unexpected_index (name)",
            "ALTER TABLE roles DROP INDEX unexpected_index",
            "roles.index.unexpected_index",
        ),
    ],
)
def test_checker_detects_real_schema_drift(database, change, restore, expected_error):
    with database.connect() as connection:
        connection.exec_driver_sql(change)
        try:
            report = verify_schema(connection)
            assert any(expected_error in error for error in report["errors"])
        finally:
            connection.exec_driver_sql(restore)
        assert verify_schema(connection)["errors"] == []
