"""Development-only API shell. Business routes arrive in I-01C/I-02."""

from fastapi import FastAPI

from packages.shared.environment import development_environment


def create_app() -> FastAPI:
    environment = development_environment()
    app = FastAPI(title="LabSafe API", version="0.1.0-dev")

    @app.get("/health")
    def health() -> dict[str, str | bool]:
        return {"status": "alive", "environment": environment, "is_simulated": True}

    @app.get("/ready")
    def ready() -> dict[str, str | bool]:
        return {
            "status": "ready",
            "environment": environment,
            "is_simulated": True,
            "scope": "I-01A process only; database and business readiness not implemented",
        }

    return app
