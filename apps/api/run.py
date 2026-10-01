"""Run the development API with actual FastAPI/Uvicorn dependencies."""

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "apps.api.app.main:create_app",
        factory=True,
        host=os.getenv("API_HOST", "127.0.0.1"),
        port=int(os.getenv("API_PORT", "8000")),
        workers=1,
        proxy_headers=False,  # Do not trust arbitrary X-Forwarded-For for login limits.
    )


if __name__ == "__main__":
    main()
