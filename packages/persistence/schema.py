"""Read-only comparison of actual MySQL metadata with the frozen design."""

import json
import re
from collections import defaultdict
from pathlib import Path

from sqlalchemy import text

from packages.persistence.database import STRICT_SQL_MODE

MODEL_PATH = Path(__file__).parent / "migrations/snapshots/0001_initial.json"


def model():
    return json.loads(MODEL_PATH.read_text(encoding="utf-8"))


def expression_tokens(value):
    # MySQL adds identifier quotes, charset introducers and redundant parentheses.
    value = value.replace(chr(96), "").replace("\\'", "'")
    value = re.sub(r"_(?:utf8mb[34]|ascii|latin1)(?=')", "", value, flags=re.I)
    tokens = re.findall(r"'(?:''|[^'])*'|[a-zA-Z_][a-zA-Z_0-9]*|\d+|<=|>=|<>|[(),=<>]", value)
    if re.sub(r"\s+", "", "".join(tokens)) != re.sub(r"\s+", "", value):
        raise ValueError(f"Unsupported SQL schema expression token: {value!r}")
    return [t if t.startswith("'") else t.lower() for t in tokens]


def check_tree(value):
    """Parse this baseline's restricted CHECK grammar without discarding precedence."""
    tokens = expression_tokens(value)
    position = 0

    precedence = {"or": 1, "and": 2, "=": 3, "<=": 3, ">=": 3, "<": 3, ">": 3, "<>": 3, "is": 3}

    def parse(minimum=0):
        nonlocal position
        if tokens[position] == "(":
            position += 1
            node = parse()
            if tokens[position] != ")":
                raise ValueError("Unbalanced CHECK expression")
            position += 1
        else:
            node = tokens[position]
            position += 1
        while position < len(tokens) and precedence.get(tokens[position], -1) >= minimum:
            operator = tokens[position]
            priority = precedence[operator]
            position += 1
            if operator == "is" and tokens[position] == "not":
                operator += " not"
                position += 1
            node = (operator, node, parse(priority + 1))
        return node

    result = parse()
    if position != len(tokens):
        raise ValueError("Unexpected CHECK expression suffix")
    return result


def column_type(value):
    # ENUM values are case-sensitive under utf8mb4_bin; normalize SQL keywords only.
    parts = re.split(r"('(?:''|[^'])*')", value)
    value = "".join(part if i % 2 else part.lower() for i, part in enumerate(parts))
    if value == "boolean":
        return "tinyint(1)"
    # MySQL 8.0 omits deprecated integer display widths except BOOLEAN/tinyint(1).
    return re.sub(r"\b(int|bigint|smallint|mediumint)\(\d+\)", r"\1", value)


def default_value(value):
    if value is None:
        return None
    value = str(value)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value.upper() in {"TRUE", "FALSE"}:
        return "1" if value.upper() == "TRUE" else "0"
    if value.lower().startswith("current_timestamp"):
        return value.lower()
    return value


def verify_schema(connection) -> dict:
    expected = model()["tables"]
    errors = []

    def compare(path, actual, wanted):
        if actual != wanted:
            errors.append(f"{path}: actual={actual!r}; expected={wanted!r}")

    def rows(query):
        return connection.execute(text(query)).mappings().all()

    tables = rows(
        "SELECT TABLE_NAME,ENGINE,TABLE_COLLATION FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE()"
    )
    actual_names = {row["TABLE_NAME"] for row in tables} - {"alembic_version"}
    compare("tables", sorted(actual_names), sorted(expected))
    for row in tables:
        if row["TABLE_NAME"] in expected:
            compare(f"{row['TABLE_NAME']}.engine", row["ENGINE"], "InnoDB")
            compare(f"{row['TABLE_NAME']}.collation", row["TABLE_COLLATION"], "utf8mb4_bin")

    columns = defaultdict(dict)
    for row in rows(
        "SELECT * FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
        "ORDER BY TABLE_NAME,ORDINAL_POSITION"
    ):
        columns[row["TABLE_NAME"]][row["COLUMN_NAME"]] = row
    for table, definition in expected.items():
        compare(f"{table}.columns", list(columns[table]), list(definition["columns"]))
        for name, wanted in definition["columns"].items():
            actual = columns[table].get(name)
            if actual is None:
                continue
            path = f"{table}.{name}"
            compare(path + ".type", column_type(actual["COLUMN_TYPE"]), column_type(wanted["type"]))
            compare(path + ".nullable", actual["IS_NULLABLE"] == "YES", wanted["nullable"])
            compare(
                path + ".default",
                default_value(actual["COLUMN_DEFAULT"]),
                default_value(wanted["default_sql"]),
            )
            generated = wanted["generated_sql"]
            compare(
                path + ".generated",
                expression_tokens(actual["GENERATION_EXPRESSION"] or ""),
                expression_tokens(generated or ""),
            )
            extra = actual["EXTRA"].lower().split()
            wanted_extra = ["stored", "generated"] if generated else []
            if (wanted["default_sql"] or "").upper().startswith("CURRENT_TIMESTAMP"):
                wanted_extra = ["default_generated"]
            compare(path + ".extra", extra, wanted_extra)
            if re.match(r"^(char|varchar|enum|text)", actual["DATA_TYPE"]):
                compare(path + ".collation", actual["COLLATION_NAME"], "utf8mb4_bin")

    indexes = defaultdict(lambda: defaultdict(list))
    for row in rows(
        "SELECT * FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
        "ORDER BY TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX"
    ):
        indexes[row["TABLE_NAME"]][row["INDEX_NAME"]].append(row)
    for table, definition in expected.items():
        required = {"PRIMARY": (0, definition["primary_key"])}
        required.update(
            {f"uq_{table}_{i}": (0, fields) for i, fields in enumerate(definition["unique"])}
        )
        required.update(
            {f"ix_{table}_{i}": (1, fields) for i, fields in enumerate(definition["indexes"])}
        )
        implicit = {
            f"fk_{table}_{i}": (1, fk["columns"]) for i, fk in enumerate(definition["foreign_keys"])
        }
        for name in required.keys() - indexes[table].keys():
            errors.append(f"{table}.index.{name}: missing")
        for name, parts in indexes[table].items():
            wanted = required.get(name, implicit.get(name))
            actual = (parts[0]["NON_UNIQUE"], [part["COLUMN_NAME"] for part in parts])
            compare(f"{table}.index.{name}", actual, wanted)
            for part in parts:
                compare(
                    f"{table}.index.{name}.kind",
                    (part["SUB_PART"], part["INDEX_TYPE"], part["IS_VISIBLE"]),
                    (None, "BTREE", "YES"),
                )

    foreign_keys = defaultdict(lambda: defaultdict(list))
    for row in rows(
        "SELECT k.*,r.UPDATE_RULE,r.DELETE_RULE FROM information_schema.KEY_COLUMN_USAGE k "
        "JOIN information_schema.REFERENTIAL_CONSTRAINTS r "
        "ON k.CONSTRAINT_SCHEMA=r.CONSTRAINT_SCHEMA AND k.TABLE_NAME=r.TABLE_NAME "
        "AND k.CONSTRAINT_NAME=r.CONSTRAINT_NAME WHERE k.TABLE_SCHEMA=DATABASE() "
        "ORDER BY k.TABLE_NAME,k.CONSTRAINT_NAME,k.ORDINAL_POSITION"
    ):
        foreign_keys[row["TABLE_NAME"]][row["CONSTRAINT_NAME"]].append(row)
    schema = connection.scalar(text("SELECT DATABASE()"))
    for table, definition in expected.items():
        wanted_names = [f"fk_{table}_{i}" for i in range(len(definition["foreign_keys"]))]
        compare(f"{table}.foreign_keys", sorted(foreign_keys[table]), sorted(wanted_names))
        for i, wanted in enumerate(definition["foreign_keys"]):
            name = f"fk_{table}_{i}"
            parts = foreign_keys[table].get(name)
            if not parts:
                continue
            compare(
                f"{table}.{name}",
                (
                    [p["COLUMN_NAME"] for p in parts],
                    parts[0]["REFERENCED_TABLE_NAME"],
                    [p["REFERENCED_COLUMN_NAME"] for p in parts],
                    parts[0]["DELETE_RULE"],
                    parts[0]["UPDATE_RULE"],
                    parts[0]["REFERENCED_TABLE_SCHEMA"],
                ),
                (
                    wanted["columns"],
                    wanted["target"],
                    wanted["references"],
                    wanted["on_delete"],
                    wanted["on_update"],
                    schema,
                ),
            )

    checks = defaultdict(dict)
    for row in rows(
        "SELECT t.TABLE_NAME,t.CONSTRAINT_NAME,c.CHECK_CLAUSE,t.ENFORCED "
        "FROM information_schema.TABLE_CONSTRAINTS t JOIN information_schema.CHECK_CONSTRAINTS c "
        "ON t.CONSTRAINT_SCHEMA=c.CONSTRAINT_SCHEMA AND t.CONSTRAINT_NAME=c.CONSTRAINT_NAME "
        "WHERE t.TABLE_SCHEMA=DATABASE()"
    ):
        checks[row["TABLE_NAME"]][row["CONSTRAINT_NAME"]] = row
    for table, definition in expected.items():
        wanted_names = [f"ck_{table}_{i}" for i in range(len(definition["checks"]))]
        compare(f"{table}.checks", sorted(checks[table]), sorted(wanted_names))
        for i, expression in enumerate(definition["checks"]):
            name = f"ck_{table}_{i}"
            if name in checks[table]:
                compare(
                    f"{table}.{name}",
                    check_tree(checks[table][name]["CHECK_CLAUSE"]),
                    check_tree(expression),
                )
                compare(f"{table}.{name}.enforced", checks[table][name]["ENFORCED"], "YES")
    head = connection.scalar(text("SELECT version_num FROM alembic_version"))
    compare("migration_head", head, "0001_initial")
    compare("session.time_zone", connection.scalar(text("SELECT @@session.time_zone")), "+00:00")
    actual_modes = set(connection.scalar(text("SELECT @@session.sql_mode")).split(","))
    required_modes = set(STRICT_SQL_MODE.split(","))
    compare("session.sql_mode.missing", sorted(required_modes - actual_modes), [])
    compare(
        "session.foreign_key_checks",
        connection.scalar(text("SELECT @@session.foreign_key_checks")),
        1,
    )
    compare("session.unique_checks", connection.scalar(text("SELECT @@session.unique_checks")), 1)
    return {
        "mysql_version": connection.scalar(text("SELECT VERSION()")),
        "schema": schema,
        "migration_head": head,
        "tables": len(actual_names),
        "foreign_keys": sum(len(v) for v in foreign_keys.values()),
        "errors": errors,
        "status": "pass" if not errors else "fail",
    }
