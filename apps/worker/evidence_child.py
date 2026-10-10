"""Dedicated evidence runtime; no AI models, DB connection or broker handles."""

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path


def main():
    from packages.application.inference_evidence import prepare_evidence
    from packages.domain.security import ServiceError
    from packages.image_evidence.perspective import CropError
    from packages.storage.s3 import S3Settings, S3Storage

    storage = None
    try:
        raw = Path(sys.argv[1]).read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Evidence input limit exceeded")
        value = json.loads(raw)
        storage = S3Storage(S3Settings.from_environment(os.environ["PUBLIC_ORIGIN"], worker=True))
        artifacts = prepare_evidence(storage, value["source"], value["result"])
        output = {"artifacts": [asdict(row) for row in artifacts]}
    except (ServiceError, CropError) as error:
        output = {"error": error.code}
    except ValueError:
        output = {"error": "SCHEMA_MISMATCH"}
    except Exception:
        output = {"error": "INTERNAL_ERROR"}
    finally:
        if storage is not None:
            storage.close()
    # Never include SDK errors, raw text or credentials in the result/error stream.
    Path(sys.argv[2]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
