"""Test-only HTTP host with file-based graceful shutdown/fault injection."""

import asyncio
import json
import os
from pathlib import Path

import uvicorn

from apps.ai_inference.app.main import create_app


async def run():
    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=int(os.environ["AI_INFERENCE_PORT"]),
            log_level="error",
            timeout_graceful_shutdown=220,
        )
    )
    directory = Path(os.environ["SMOKE_CONTROL_DIRECTORY"])

    async def control():
        try:
            while not server.should_exit:
                runtime = app.state.supervisor
                if runtime.process is not None and runtime.process.pid:
                    (directory / "pid.json").write_text(
                        json.dumps({"pid": runtime.process.pid, "ready": runtime.ready}),
                        encoding="ascii",
                    )
                if (directory / "crash").exists():
                    (directory / "crash").unlink()
                    if runtime.process:
                        runtime.process.kill()
                if (directory / "stop").exists():
                    runtime.closing = True
                    server.should_exit = True
                await asyncio.sleep(0.05)
        finally:
            pass

    task = asyncio.create_task(control())
    try:
        await server.serve()
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        (directory / "closed.json").write_text(
            json.dumps(
                {
                    "child_reaped": app.state.supervisor.process is None,
                    "receiver_reaped": app.state.supervisor.receiver is None,
                }
            ),
            encoding="ascii",
        )


if __name__ == "__main__":
    asyncio.run(run())
