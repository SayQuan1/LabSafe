"""One HTTP supervisor with an optional resident, pinned development CPU child."""

import os

import uvicorn


def main() -> None:
    from apps.ai_inference.app.main import create_app

    app = create_app()

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            if app.state.supervisor:
                app.state.supervisor.closing = True
            super().handle_exit(sig, frame)

    config = uvicorn.Config(
        app,
        host=os.getenv("AI_INFERENCE_HOST", "127.0.0.1"),
        port=int(os.getenv("AI_INFERENCE_PORT", "8001")),
        workers=1,
        timeout_graceful_shutdown=220,
    )
    Server(config).run()


if __name__ == "__main__":
    main()
