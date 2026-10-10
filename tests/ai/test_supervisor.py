"""Real spawn/IPC fault tests; synthetic child, no claim of numerical accuracy."""

import asyncio
import json
import multiprocessing
import os
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from apps.ai_inference.app.fixture import ProtocolError
from apps.ai_inference.runtime_cpu import send
from apps.ai_inference.supervisor import CPUSupervisor

ID = "11111111-1111-4111-8111-111111111111"
IDENTITY = {
    "service_commit": "test",
    "pipeline_version": "vision-v1",
    "model_bundle_id": ID,
    "dictionary_version_id": ID,
    "device_profile": "cpu",
    "purpose": "development",
    "is_simulated": False,
    "adapter_id": "dfine-coco80-rgb-stretch-v1",
    "runtime_profile": "dfine-cpu-fp32-ocrv6smallcpu-v1",
    "runtime_lock_sha256": "a" * 64,
    "model_checksum": "b" * 64,
    "dictionary_sha256": "c" * 64,
    "detector_device": "cpu",
    "ocr_device": "cpu",
}


def payload(seconds=5):
    return {
        "run_id": ID,
        "attempt_id": ID,
        "fencing_token": 1,
        "tenant_id": ID,
        "request_hash": "d" * 64,
        **{
            k: IDENTITY[k]
            for k in (
                "model_bundle_id",
                "model_checksum",
                "dictionary_version_id",
                "dictionary_sha256",
                "pipeline_version",
            )
        },
        "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
        "image_refs": [{"image_id": ID, "sha256": "e" * 64}],
    }


def synthetic_result(source, identity):
    value = {
        **{
            k: source[k]
            for k in (
                "run_id",
                "attempt_id",
                "fencing_token",
                "tenant_id",
                "request_hash",
                "model_bundle_id",
                "model_checksum",
                "dictionary_version_id",
                "dictionary_sha256",
                "pipeline_version",
            )
        },
        "execution_identity": identity,
        "input_hashes": [{"image_id": ID, "sha256": "e" * 64}],
        "outcome": "needs_review",
        "quality": [
            {
                "image_id": ID,
                "status": "pass",
                "reasons": [],
                "blur_score": 100,
                "brightness": 0.5,
                "glare_ratio": 0,
            }
        ],
        **{
            k: []
            for k in ("detections", "text_regions", "crops", "ocr_fields", "entities", "relations")
        },
        "timing_ms": {
            k: 0
            for k in (
                "download_ms",
                "quality_ms",
                "detection_ms",
                "ocr_ms",
                "normalization_ms",
                "total_ms",
            )
        },
    }
    value["input_hashes"] = [
        {"image_id": row["image_id"], "sha256": row["sha256"]} for row in source["image_refs"]
    ]
    value["quality"] = [
        dict(value["quality"][0], image_id=row["image_id"]) for row in source["image_refs"]
    ]
    return value


def fake_child(channel, bundle, scenario):
    try:
        if scenario == "startup_timeout":
            time.sleep(20)
        if scenario == "startup_fail":
            send(channel, {"kind": "error", "code": "MODEL_ERROR"})
            return
        identity = dict(bundle.identity)
        if scenario == "wrong_identity":
            identity["runtime_lock_sha256"] = "f" * 64
        send(channel, {"kind": "loaded", "identity": identity})
        while True:
            command = json.loads(channel.recv_bytes(65536))
            if scenario == "crash":
                os._exit(2)
            if scenario == "timeout":
                time.sleep(20)
            if scenario == "partial":
                channel.send_bytes(b'{"kind":"result","value":')
                time.sleep(20)
            if scenario == "oversized":
                channel.send_bytes(b"x" * (2 * 1024 * 1024 + 1))
                time.sleep(20)
            value = synthetic_result(command["payload"], identity)
            if scenario in {
                "association",
                "wrong_association",
                "fields",
                "wrong_fields",
                "chemical",
                "wrong_chemical",
            }:
                from tests.evidence_helpers import add_bottles, add_regions

                source = command["payload"]
                lease = SimpleNamespace(
                    input=SimpleNamespace(
                        run_id=source["run_id"],
                        images=(SimpleNamespace(image_id=source["image_refs"][0]["image_id"]),),
                    )
                )
                add_regions(
                    lease, value, 2 if "fields" in scenario or "chemical" in scenario else 1
                )
                add_bottles(lease, value, 1)
                if scenario == "wrong_association":
                    value["detections"][0]["bbox"] = [0, 0, 0.7, 1]
                if "fields" in scenario:
                    from packages.inference_protocol.fields import extract_fields

                    value["text_regions"][0].update(raw_text="NAME", confidence=0.9)
                    value["text_regions"][1].update(raw_text="Ethanol", confidence=0.3)
                    value["ocr_fields"] = extract_fields(value["text_regions"])
                    if scenario == "wrong_fields":
                        value["ocr_fields"][0]["source_lines"].pop()
                if "chemical" in scenario:
                    from tests.evidence_helpers import (
                        add_chemical_context,
                        synthetic_chemical_entries,
                    )

                    value["text_regions"][0]["raw_text"] = "名称:Alpha"
                    value["text_regions"][1]["raw_text"] = "名称:甲"
                    add_chemical_context(value, synthetic_chemical_entries())
                    if scenario == "wrong_chemical":
                        value["entities"][0]["candidates"][0]["confidence"] = 0.1
            if scenario == "stale":
                value["fencing_token"] += 1
            send(channel, {"kind": "result", "value": value})
    except (EOFError, OSError):
        pass
    finally:
        channel.close()


async def until(predicate, seconds=6):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Supervisor state did not converge")


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.baseline = {p.pid for p in multiprocessing.active_children()}
        self.runtimes = []

    def runtime(self, scenario="ok"):
        runtime = CPUSupervisor(SimpleNamespace(identity=IDENTITY), scenario, target=fake_child)
        runtime.startup_seconds = 3
        runtime.reload_seconds = 0.1
        self.runtimes.append(runtime)
        runtime.start()
        return runtime

    async def asyncTearDown(self):
        for runtime in self.runtimes:
            await runtime.close()
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, self.baseline)
        self.assertFalse(any(t.name == "cpu-ipc-receiver" for t in threading.enumerate()))

    async def test_loaded_identity_resident_pid_and_shutdown(self):
        runtime = self.runtime()
        self.assertFalse(runtime.ready)
        await until(lambda: runtime.ready)
        pid = runtime.process.pid
        for _ in range(2):
            result = await runtime.infer(payload(), "runs")
            self.assertEqual(result["execution_identity"], IDENTITY)
            self.assertEqual(runtime.process.pid, pid)
        runtime.closing = True
        self.assertFalse(runtime.ready)
        with self.assertRaises(ProtocolError):
            await runtime.infer(payload(), "runs")

    async def test_three_load_failures_lock_not_ready(self):
        for scenario in ("startup_fail", "wrong_identity", "startup_timeout"):
            runtime = self.runtime(scenario)
            if scenario == "startup_timeout":
                runtime.startup_seconds = 0.1
            await until(lambda: runtime.failures == 3, seconds=12)
            self.assertFalse(runtime.ready)
            self.assertIsNone(runtime.process)
            await asyncio.sleep(0.15)
            self.assertEqual(runtime.failures, 3)

    async def test_crash_timeout_partial_oversized_and_stale_dispose(self):
        for scenario in ("crash", "timeout", "partial", "oversized", "stale"):
            runtime = self.runtime(scenario)
            await until(lambda: runtime.ready)
            pid = runtime.process.pid
            with self.assertRaises(ProtocolError):
                await runtime.infer(payload(0.15), "runs")
            self.assertFalse(runtime.ready)
            runtime.analysis = "ok"
            await until(lambda: runtime.ready)
            self.assertNotEqual(pid, runtime.process.pid)
            await runtime.infer(payload(), "runs")
            await runtime.close()

    async def test_cancel_and_close_during_loading_reap_process(self):
        runtime = self.runtime("timeout")
        await until(lambda: runtime.ready)
        task = asyncio.create_task(runtime.infer(payload(), "runs"))
        await until(lambda: runtime.busy)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(runtime.process)
        loading = self.runtime("startup_timeout")
        await until(lambda: loading.process is not None)
        await loading.close()
        self.assertIsNone(loading.process)

    async def test_idle_child_exit_reloads_and_expired_request_keeps_process(self):
        runtime = self.runtime()
        await until(lambda: runtime.ready)
        pid = runtime.process.pid
        with self.assertRaises(ProtocolError) as error:
            await runtime.infer(payload(-1), "quality")
        self.assertEqual(error.exception.code, "AI_TIMEOUT")
        self.assertEqual(runtime.process.pid, pid)
        runtime.process.kill()
        await until(lambda: runtime.ready and runtime.process.pid != pid)


if __name__ == "__main__":
    unittest.main()
