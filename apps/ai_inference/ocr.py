"""Real CPU text detection and recognition using locally supplied ONNX files."""

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.ocr_cpu import OcrOptions, run_cpu_ocr


def main():
    parser = argparse.ArgumentParser(description="PP-OCRv6_small local CPU OCR")
    parser.add_argument("--det-dir", type=Path, required=True)
    parser.add_argument("--rec-dir", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True, nargs="+")
    parser.add_argument("--text-min", type=float, default=0.6)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--run-id", default=str(uuid4()))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    options = OcrOptions(
        str(args.det_dir.resolve()),
        str(args.rec_dir.resolve()),
        args.text_min,
        args.threads,
        args.timeout,
    )
    try:
        result = run_cpu_ocr(options, args.input, args.run_id)
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output is None:
            # Handle non-BMP dictionary entries on Windows consoles.
            sys.stdout.reconfigure(encoding="utf-8")
            print(text, end="")
        else:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(text)
    except AdapterError as error:
        print(json.dumps({"error": {"code": error.code, "message": str(error)}}), file=sys.stderr)
        return 1
    except OSError:
        print(json.dumps({"error": {"code": "OUTPUT_UNAVAILABLE"}}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
