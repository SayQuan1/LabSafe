"""Async HTTP supervisor of a resident spawn child with bounded JSON IPC."""

import asyncio
import json
import multiprocessing
import queue
import threading
import time
from datetime import datetime, timezone

from apps.ai_inference.app.fixture import ProtocolError
from apps.ai_inference.runtime_cpu import MAX_REQUEST, MAX_RESULT, child_main
from packages.inference_protocol.contract import DOCUMENT, validate
from packages.inference_protocol.evidence import validate_closure


class CPUSupervisor:
    startup_seconds = 120
    reload_seconds = 30
    shutdown_seconds = 220

    def __init__(self, bundle, analysis, target=child_main):
        self.bundle, self.analysis, self.target = bundle, analysis, target
        self.process = self.channel = self.receiver = self.messages = None
        self.identity = None
        self.closing = self.busy = False
        self.failures = 0
        self.next_load = 0
        self.maintenance = None

    @property
    def ready(self):
        return bool(not self.closing and self.identity and self.process and self.process.is_alive())

    def start(self):
        self.maintenance = asyncio.create_task(self._maintain())

    def _receive(self, channel, messages):
        try:
            while True:
                value = json.loads(channel.recv_bytes(MAX_RESULT))
                messages.put_nowait(value)
        except (EOFError, OSError, ValueError, queue.Full):
            try:
                messages.put_nowait({"kind": "error", "code": "MODEL_ERROR"})
            except queue.Full:
                pass

    async def _message(self, deadline):
        while time.monotonic() < deadline:
            try:
                return self.messages.get_nowait()
            except queue.Empty:
                if not self.process or not self.process.is_alive():
                    raise ProtocolError("MODEL_ERROR", "CPU process exited")
                await asyncio.sleep(min(0.01, max(0, deadline - time.monotonic())))
        raise ProtocolError("AI_TIMEOUT", "CPU phase exceeded its deadline")

    async def _dispose(self):
        self.identity = None
        process, channel, receiver = self.process, self.channel, self.receiver
        self.process = self.channel = self.receiver = self.messages = None
        if process is not None:
            if process.is_alive():
                process.kill()
            await asyncio.to_thread(process.join, 2)
            if process.is_alive():
                raise RuntimeError("CPU child failed to exit")
            process.close()
        if channel is not None:
            channel.close()
        if receiver is not None:
            await asyncio.to_thread(receiver.join, 2)
            if receiver.is_alive():
                raise RuntimeError("CPU IPC receiver failed to exit")

    async def _launch(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self.messages = queue.Queue(maxsize=2)
        self.channel = parent
        self.process = context.Process(target=self.target, args=(child, self.bundle, self.analysis))
        self.process.daemon = True
        deadline = time.monotonic() + self.startup_seconds
        try:
            self.process.start()
        except BaseException:
            self.process.close()
            self.process = None
            raise
        finally:
            child.close()
        self.receiver = threading.Thread(
            target=self._receive, args=(parent, self.messages), name="cpu-ipc-receiver", daemon=True
        )
        self.receiver.start()
        message = await self._message(deadline)
        if message.get("kind") != "loaded" or message.get("identity") != self.bundle.identity:
            raise ProtocolError("MODEL_VERSION_UNAVAILABLE", "Loaded CPU identity mismatch")
        validate("Version", message["identity"])
        self.identity = message["identity"]

    async def _maintain(self):
        try:
            while not self.closing:
                if not self.busy and self.identity and self.process and not self.process.is_alive():
                    self.next_load = max(self.next_load, time.monotonic() + self.reload_seconds)
                    await self._dispose()
                if (
                    not self.busy
                    and not self.ready
                    and self.failures < 3
                    and time.monotonic() >= self.next_load
                ):
                    await self._dispose()
                    self.next_load = time.monotonic() + self.reload_seconds
                    try:
                        await self._launch()
                    except (
                        ProtocolError,
                        ValueError,
                        OSError,
                        TypeError,
                        KeyError,
                        AttributeError,
                    ):
                        self.failures += 1
                        await self._dispose()
                        self.next_load = max(self.next_load, time.monotonic() + self.reload_seconds)
                    else:
                        self.failures = 0
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise

    async def infer(self, payload, stage):
        if self.busy:
            raise ProtocolError("AI_BUSY", "CPU instance already has an active request")
        if not self.ready:
            raise ProtocolError("MODEL_NOT_READY", "CPU model is not ready")
        remaining = (
            datetime.fromisoformat(payload["deadline_at"].upper().replace("Z", "+00:00"))
            - datetime.now(timezone.utc)
        ).total_seconds()
        budget = min(remaining, 10 if stage == "quality" else 180)
        if budget <= 0:
            raise ProtocolError("AI_TIMEOUT", "Request deadline expired")
        deadline = time.monotonic() + budget
        raw = json.dumps(
            {"stage": stage, "payload": payload, "deadline": deadline},
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if len(raw) > MAX_REQUEST:
            raise ProtocolError("VALIDATION_ERROR", "Inference request exceeds capacity")
        self.busy = True
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self.channel.send_bytes, raw),
                timeout=max(0, deadline - time.monotonic()),
            )
            message = await self._message(deadline)
            if message.get("kind") != "result":
                code = message.get("code", "MODEL_ERROR")
                if code not in DOCUMENT["x-error-codes"]:
                    code = "MODEL_ERROR"
                raise ProtocolError(code, "CPU computation failed")
            result = message["value"]
            validate("InferenceResult", result)
            validate_closure(result, [r["image_id"] for r in payload["image_refs"]])
            if (
                result.get("execution_identity") != self.identity
                or any(
                    result[key] != payload[key]
                    for key in (
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
                )
                or result["input_hashes"]
                != [
                    {"image_id": r["image_id"], "sha256": r["sha256"]}
                    for r in payload["image_refs"]
                ]
            ):
                raise ValueError("Result identity changed")
            if time.monotonic() >= deadline:
                raise ProtocolError("AI_TIMEOUT", "Response deadline exceeded")
            return result
        except BaseException as error:
            await self._dispose()
            self.next_load = max(self.next_load, time.monotonic() + self.reload_seconds)
            if isinstance(error, (asyncio.CancelledError, ProtocolError)):
                raise
            code = "AI_TIMEOUT" if isinstance(error, TimeoutError) else "MODEL_ERROR"
            raise ProtocolError(code, "CPU computation failed") from None
        finally:
            self.busy = False

    async def close(self):
        self.closing = True
        end = time.monotonic() + self.shutdown_seconds
        while self.busy and time.monotonic() < end:
            await asyncio.sleep(0.02)
        if self.maintenance:
            self.maintenance.cancel()
            try:
                await self.maintenance
            except asyncio.CancelledError:
                pass
        await self._dispose()
