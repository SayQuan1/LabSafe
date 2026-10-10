import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic import command
from argon2 import Type, extract_parameters

from packages.persistence.bootstrap import PASSWORD_HASHER, TenantInput, bootstrap_tenant
from packages.persistence.cli import main, migration_config
from packages.persistence.database import DatabaseSafetyError, database_engine
from packages.persistence.schema import (
    MODEL_PATH,
    check_tree,
    column_type,
    expression_tokens,
    model,
)

ROOT = Path(__file__).resolve().parents[2]


def test_head_schema_matches_authoritative_design_and_preserves_initial_baseline():
    assert model() == json.loads((ROOT / "contracts/data-model.json").read_text(encoding="utf-8"))
    baseline = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
    head = model()
    for name in ("object_version", "size_bytes"):
        del head["tables"]["report_exports"]["columns"][name]
    del head["tables"]["run_images"]["columns"]["analysis_object_version"]
    columns = head["tables"]["image_derivatives"]["columns"]
    columns["detection_id"]["nullable"] = False
    del columns["line_id"], columns["size_bytes"]
    assert head == baseline
    snapshot = MODEL_PATH.with_suffix(".sql").read_text(encoding="utf-8").strip()
    head_ddl = (ROOT / "contracts/database-design.sql").read_text(encoding="utf-8").strip()
    additions = "  `object_version` VARCHAR(200) NULL,\n  `size_bytes` BIGINT UNSIGNED NULL,\n"
    assert head_ddl.count(additions) == 1
    version_column = "  `analysis_object_version` VARCHAR(200) NULL,\n"
    # One existing asset column and one new frozen run column.
    assert head_ddl.count(version_column) == 2
    index = head_ddl.index("CREATE TABLE `run_images`")
    without_version = head_ddl[:index] + head_ddl[index:].replace(version_column, "", 1)
    derivative_start = without_version.index("CREATE TABLE `image_derivatives`")
    derivative_end = without_version.index("CREATE TABLE", derivative_start + 1)
    derivative = without_version[derivative_start:derivative_end]
    original = derivative.replace(
        "`detection_id` CHAR(36) NULL", "`detection_id` CHAR(36) NOT NULL"
    )
    for name, sql_type in (("line_id", "CHAR(36)"), ("size_bytes", "BIGINT UNSIGNED")):
        original = original.replace(f"  `{name}` {sql_type} NULL,\n", "")
    without_version = (
        without_version[:derivative_start] + original + without_version[derivative_end:]
    )
    assert snapshot == without_version.replace(additions, "")


@pytest.mark.parametrize("environment", ["production", "staging", "", "prod"])
def test_no_production_database_access(monkeypatch, environment):
    monkeypatch.setenv("APP_ENV", environment)
    with pytest.raises(RuntimeError, match="production"):
        database_engine()


@pytest.mark.parametrize(
    "url,schema",
    [
        ("mysql+pymysql://u:p@localhost/mysql", "mysql"),
        ("mysql+pymysql://u:p@localhost/labsafe_test_other", "labsafe_test_confirmed"),
        ("sqlite:///labsafe_test_confirmed", "labsafe_test_confirmed"),
        ("mysql+pymysql://u@localhost/labsafe_test_confirmed", "labsafe_test_confirmed"),
        (
            "mysql+pymysql://u:p@localhost/labsafe_test_confirmed?local_infile=1",
            "labsafe_test_confirmed",
        ),
        ("malformed-SECRET", "labsafe_test_confirmed"),
        ("mysql+pymysql://u:p@remote.example/labsafe_test_confirmed", "labsafe_test_confirmed"),
    ],
)
def test_url_guards_do_not_connect_or_expose_secrets(monkeypatch, tmp_path, url, schema):
    secret = tmp_path / "url"
    secret.write_text(url, encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL_FILE", str(secret))
    monkeypatch.setenv("DATABASE_SCHEMA", schema)
    with pytest.raises(DatabaseSafetyError) as failure:
        database_engine()
    assert url not in str(failure.value)


def test_password_policy_parameters_and_random_salt():
    password = "  Synthetic password 中文  "
    first = PASSWORD_HASHER.hash(password)
    second = PASSWORD_HASHER.hash(password)
    assert first != second
    parameters = extract_parameters(first)
    assert (
        parameters.type,
        parameters.time_cost,
        parameters.memory_cost,
        parameters.parallelism,
        parameters.salt_len,
        parameters.hash_len,
    ) == (Type.ID, 3, 65536, 1, 16, 32)
    assert PASSWORD_HASHER.verify(first, password)


@pytest.mark.parametrize("password", ["", "x" * 11, "x" * 129])
def test_bad_password_rejected_before_connection(password):
    engine = Mock()
    data = TenantInput("synthetic", "Test", "UTC", "admin", "Admin", "Testing")
    with pytest.raises(DatabaseSafetyError, match="Password"):
        bootstrap_tenant(engine, data, password)
    engine.connect.assert_not_called()


def test_normalization_and_timezone_validation():
    data = TenantInput("synthetic", "Test", "UTC", "  ＡＤＭＩＮ  ", "Admin", "Testing")
    assert data.validated().username == "admin"
    with pytest.raises(DatabaseSafetyError, match="timezone"):
        TenantInput("synthetic", "Test", "Not/AZone", "admin", "Admin", "Testing").validated()


def test_expression_normalization_preserves_logic():
    assert check_tree("(a IS NULL) <> (b IS NULL)") == check_tree("((a is null) <> (b is null))")
    assert check_tree("a=1 OR b=2 AND c=3") != check_tree("(a=1 OR b=2) AND c=3")
    assert expression_tokens("coalesce(a,_utf8mb4\\'x\\')") == expression_tokens("COALESCE(a,'x')")


def test_type_normalization_preserves_case_sensitive_enum_values():
    assert column_type("ENUM('active')") == column_type("enum('active')")
    assert column_type("ENUM('ACTIVE')") != column_type("ENUM('active')")


def test_offline_migration_cannot_bypass_guards():
    with pytest.raises(RuntimeError, match="Offline"):
        command.upgrade(migration_config(), "head", sql=True)


def test_cli_sanitizes_driver_failures(monkeypatch, capsys):
    def fail():
        raise RuntimeError("mysql://credential-SENTINEL")

    monkeypatch.setattr("packages.persistence.cli.database_engine", fail)
    assert main(["verify"]) == 1
    assert "credential-SENTINEL" not in capsys.readouterr().err
