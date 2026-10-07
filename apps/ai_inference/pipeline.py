"""CLI for real quality gating followed by joint local CPU detection/OCR."""

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.adapters.quality import QualityThresholds
from apps.ai_inference.pipeline_cpu import PipelineOptions, run_cpu_pipeline


def main():
    parser = argparse.ArgumentParser(description="Quality + COCO80 + PP-OCRv6_small CPU pipeline")
    parser.add_argument("--model", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--preprocessor", type=Path)
    parser.add_argument("--det-dir", type=Path)
    parser.add_argument("--rec-dir", type=Path)
    parser.add_argument("--quality-only", action="store_true")
    parser.add_argument("--input", type=Path, required=True, nargs="+")
    parser.add_argument("--threshold", type=float, default=0.4)
    parser.add_argument("--text-min", type=float, default=0.6)
    parser.add_argument("--blur-min", type=float, default=80)
    parser.add_argument("--dark-min", type=float, default=0.12)
    parser.add_argument("--glare-max", type=float, default=0.30)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--run-id", default=str(uuid4()))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    options = PipelineOptions(
        model_path=str(args.model.resolve()) if args.model else None,
        det_dir=str(args.det_dir.resolve()) if args.det_dir else None,
        rec_dir=str(args.rec_dir.resolve()) if args.rec_dir else None,
        config_path=str(args.config.resolve()) if args.config else None,
        preprocessor_path=str(args.preprocessor.resolve()) if args.preprocessor else None,
        detection_min=args.threshold,
        text_min=args.text_min,
        quality=QualityThresholds(args.blur_min, args.dark_min, args.glare_max),
        quality_only=args.quality_only,
        threads=args.threads,
        timeout_seconds=args.timeout,
    )
    try:
        result = run_cpu_pipeline(options, args.input, args.run_id)
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output is None:
            sys.stdout.reconfigure(encoding="utf-8")
            print(encoded, end="")
        else:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
    except AdapterError as error:
        print(json.dumps({"error": {"code": error.code, "message": str(error)}}), file=sys.stderr)
        return 1
    except OSError:
        print(json.dumps({"error": {"code": "OUTPUT_UNAVAILABLE"}}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
