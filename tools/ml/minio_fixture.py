"""Isolated real MinIO test server, random credentials, explicit scoped IAM policies."""

import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from apps.ai_inference.analysis import AnalysisSettings
from packages.storage.s3 import BUCKET, S3Settings


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def temporary_store():
    root = Path(tempfile.mkdtemp(prefix="labsafe-minio-test-")).resolve()
    try:
        yield root
    finally:
        # Versioned object namespaces exceed legacy Windows 260-char deletion paths.
        # Verify our exact newly-created temp directory before recursive cleanup.
        if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith(
            "labsafe-minio-test-"
        ):
            raise RuntimeError("Unsafe MinIO test cleanup path")
        path = "\\\\?\\" + str(root) if os.name == "nt" else str(root)
        shutil.rmtree(path)


@contextmanager
def minio_store(binary, tenant, laboratory):
    """Uses ONLY a newly created temporary directory, never the installed data directory."""
    binary = Path(binary).resolve()
    mc = binary.with_name("mc.exe" if os.name == "nt" else "mc")
    if not binary.is_file() or not mc.is_file():
        raise ValueError("MinIO server and mc executable required")
    with temporary_store() as root:
        port, console = free_port(), free_port()
        endpoint = f"http://127.0.0.1:{port}"
        admin, password = "test" + secrets.token_hex(8), secrets.token_urlsafe(32)
        env = {**os.environ, "MINIO_ROOT_USER": admin, "MINIO_ROOT_PASSWORD": password}
        process = None
        clients = []

        def client(access, secret):
            result = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access,
                aws_secret_access_key=secret,
                region_name="us-east-1",
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    connect_timeout=1,
                    read_timeout=3,
                    retries={"max_attempts": 0},
                ),
            )
            clients.append(result)
            return result

        def command(*args):
            result = subprocess.run(
                [str(mc), "--config-dir", str(root / "mc"), *args],
                capture_output=True,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode:
                raise RuntimeError("Isolated MinIO administration command failed")

        with (root / "server.log").open("wb") as log:
            try:
                process = subprocess.Popen(
                    [
                        str(binary),
                        "server",
                        str(root / "data"),
                        "--address",
                        f"127.0.0.1:{port}",
                        "--console-address",
                        f"127.0.0.1:{console}",
                    ],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                root_client = client(admin, password)
                end = time.monotonic() + 20
                while time.monotonic() < end:
                    if process.poll() is not None:
                        raise RuntimeError("Isolated MinIO server failed to start")
                    try:
                        root_client.list_buckets()
                        break
                    except Exception:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Isolated MinIO startup timed out")
                root_client.create_bucket(Bucket=BUCKET)
                root_client.put_bucket_versioning(
                    Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
                )
                command("alias", "set", "test", endpoint, admin, password)
                prefix = f"tenant/{tenant}/lab/{laboratory}"
                analysis = f"arn:aws:s3:::{BUCKET}/{prefix}/analysis/*"
                derived = f"arn:aws:s3:::{BUCKET}/{prefix}/derivatives/*"
                users = {}
                for role, statements in {
                    "ai": [
                        {
                            "Effect": "Allow",
                            "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                            "Resource": [analysis],
                        }
                    ],
                    "worker": [
                        {
                            "Effect": "Allow",
                            "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                            "Resource": [analysis, derived],
                        },
                        {"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": [derived]},
                        {
                            "Effect": "Allow",
                            "Action": ["s3:ListBucket"],
                            "Resource": [f"arn:aws:s3:::{BUCKET}"],
                            "Condition": {"StringLike": {"s3:prefix": [prefix + "/derivatives/*"]}},
                        },
                    ],
                }.items():
                    access, secret = role + secrets.token_hex(8), secrets.token_urlsafe(32)
                    policy = root / f"{role}.json"
                    policy.write_text(
                        json.dumps({"Version": "2012-10-17", "Statement": statements})
                    )
                    command("admin", "user", "add", "test", access, secret)
                    command("admin", "policy", "create", "test", role, str(policy))
                    command("admin", "policy", "attach", "test", role, "--user", access)
                    users[role] = (access, secret, client(access, secret))
                checks = []

                def denied(role, method, **kwargs):
                    try:
                        response = getattr(users[role][2], method)(Bucket=BUCKET, **kwargs)
                        if "Body" in response:
                            response["Body"].close()
                    except ClientError as error:
                        if error.response["Error"]["Code"] != "AccessDenied":
                            raise
                        checks.append(f"{role}:{method}:denied")
                    else:
                        raise AssertionError("Scoped MinIO policy allowed forbidden operation")

                forbidden = "tenant/other/lab/other/analysis/test.png"
                root_client.put_object(Bucket=BUCKET, Key=forbidden, Body=b"synthetic")
                for role in users:
                    denied(role, "get_object", Key=forbidden)
                    denied(role, "list_objects_v2", Prefix=prefix)
                    denied(
                        role,
                        "put_object",
                        Key=prefix + "/analysis/forbidden.png",
                        Body=b"synthetic",
                    )
                    denied(role, "delete_object", Key=prefix + "/derivatives/forbidden.png")
                denied(
                    "ai", "put_object", Key=prefix + "/derivatives/forbidden.png", Body=b"synthetic"
                )
                existing = prefix + "/derivatives/test.png"
                version = root_client.put_object(Bucket=BUCKET, Key=existing, Body=b"synthetic")[
                    "VersionId"
                ]
                denied("ai", "get_object", Key=existing, VersionId=version)
                denied("worker", "delete_object", Key=existing, VersionId=version)
                ai = AnalysisSettings(
                    endpoint, (endpoint,), frozenset({tenant}), users["ai"][0], users["ai"][1]
                )
                worker = S3Settings(
                    internal_endpoint=endpoint,
                    public_endpoint="https://labsafe.test",
                    public_origin="https://labsafe.test",
                    access_key=users["worker"][0],
                    secret_key=users["worker"][1],
                )
                yield root_client, ai, worker, checks
            finally:
                for connection in clients:
                    connection.close()
                if process is not None:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
