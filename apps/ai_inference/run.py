"""One HTTP process; the real computation child is an I-03 deliverable."""

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "apps.ai_inference.app.main:create_app",
        factory=True,
        host=os.getenv("AI_INFERENCE_HOST", "127.0.0.1"),
        port=int(os.getenv("AI_INFERENCE_PORT", "8001")),
        workers=1,
    )


if __name__ == "__main__":
    main()
