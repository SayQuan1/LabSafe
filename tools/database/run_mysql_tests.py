"""Run integration tests using a new local mysqld or a dedicated GitHub CI service.

Never discover, stop, reconfigure or use an existing local MySQL service.
"""

import argparse
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import pymysql
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[2]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_suite(connection, port, password, folder):
    schema = "labsafe_test_" + uuid4().hex
    # Identifier is internally generated, never taken from CLI/connection strings.
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE {schema} CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
    url = URL.create(
        "mysql+pymysql",
        username="root",
        password=password,
        host="127.0.0.1",
        port=port,
        database=schema,
    )
    secret = folder / "database-url"
    secret.write_text(url.render_as_string(hide_password=False), encoding="utf-8")
    secret.chmod(0o600)
    environment = os.environ.copy()
    environment.update(
        APP_ENV="test",
        DATABASE_SCHEMA=schema,
        DATABASE_URL_FILE=str(secret),
        LABSAFE_MYSQL_INTEGRATION="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
        LABSAFE_ALLOW_DESTRUCTIVE_DOWNGRADE=schema,
    )
    try:
        return subprocess.run(
            [sys.executable, "-m", "pytest", "tests/persistence", "-v", "-s", "--tb=short"],
            cwd=ROOT,
            env=environment,
            check=False,
        ).returncode
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE {schema}")
        secret.unlink(missing_ok=True)


def local_suite(mysqld, folder):
    executable = Path(mysqld).resolve(strict=True)
    data = folder / "data"
    data.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    subprocess.run(
        [
            str(executable),
            "--no-defaults",
            "--initialize-insecure",
            f"--datadir={data}",
            "--console",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        creationflags=NO_WINDOW,
    )
    server = subprocess.Popen(
        [
            str(executable),
            "--no-defaults",
            f"--datadir={data}",
            f"--port={port}",
            "--bind-address=127.0.0.1",
            "--mysqlx=0",
            "--skip-log-bin",
            f"--log-error={folder / 'server.log'}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )
    connection = None
    verified_own_server = False
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError("Disposable mysqld exited during startup")
            try:
                connection = pymysql.connect(
                    host="127.0.0.1", port=port, user="root", connect_timeout=2, autocommit=True
                )
                break
            except pymysql.OperationalError:
                time.sleep(0.2)
        if connection is None:
            raise RuntimeError("Disposable mysqld did not start within 60 seconds")
        with connection.cursor() as cursor:
            cursor.execute("SELECT @@datadir,@@port,VERSION()")
            actual_data, actual_port, version = cursor.fetchone()
            if Path(actual_data).resolve() != data.resolve() or actual_port != port:
                raise RuntimeError("Refusing connection: not the server created by this process")
            verified_own_server = True
            print(
                f"Isolated MySQL {version}; random loopback port; new temporary data directory",
                flush=True,
            )
            password = secrets.token_urlsafe(32)
            cursor.execute("ALTER USER 'root'@'localhost' IDENTIFIED BY %s", (password,))
        return run_suite(connection, port, password, folder)
    finally:
        if connection is not None:
            if verified_own_server:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("SHUTDOWN")
                except pymysql.Error:
                    pass
            connection.close()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.terminate()  # Only the child process created above, never a service/PID search.
            server.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mysqld", help="Path to installed MySQL server executable, not a data dir")
    mode.add_argument("--ci", action="store_true", help="Use this job's disposable CI service")
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="labsafe-i01b-") as directory:
        folder = Path(directory)
        if arguments.mysqld:
            return local_suite(arguments.mysqld, folder)
        if os.environ.get("CI") != "true":
            raise RuntimeError("--ci is restricted to a disposable CI job service")
        port = int(os.environ["LABSAFE_CI_MYSQL_PORT"])
        password = os.environ["LABSAFE_CI_MYSQL_PASSWORD"]
        with pymysql.connect(
            host="127.0.0.1", port=port, user="root", password=password, autocommit=True
        ) as connection:
            return run_suite(connection, port, password, folder)


if __name__ == "__main__":
    raise SystemExit(main())
