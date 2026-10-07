"""CLI for local, real COCO-80 CPU detection, outside the fixture HTTP service."""

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.cpu import CpuOptions, run_cpu_detection


def main():
    parser = argparse.ArgumentParser(description="Run official COCO-80 detection on CPU")
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--preprocessor", type=Path)
    parser.add_argument("--input", required=True, nargs="+", type=Path)
    parser.add_argument("--threshold", type=float, default=0.4)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--run-id", default=str(uuid4()))
    parser.add_argument("--output", type=Path, help="Write JSON after all images succeed")
    args = parser.parse_args()
    options = CpuOptions(
        str(args.model.resolve()),
        str((args.config or args.model.with_name("config.json")).resolve()),
        str((args.preprocessor or args.model.with_name("preprocessor_config.json")).resolve()),
        args.threshold,
        args.threads,
        args.timeout,
    )
    try:
        result = run_cpu_detection(options, args.input, args.run_id)
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output is None:
            print(encoded, end="")
        else:
            # Exclusive create prevents accidentally replacing a previous report.
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
    except AdapterError as error:
        print(json.dumps({"error": {"code": error.code, "message": str(error)}}), file=sys.stderr)
        return 1
    except OSError:
        print(
            json.dumps(
                {"error": {"code": "OUTPUT_UNAVAILABLE", "message": "Cannot create output file"}}
            ),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
