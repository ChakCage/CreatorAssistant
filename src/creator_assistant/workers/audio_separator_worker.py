from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path


PREFIX = "CREATOR_JSON:"


def emit(payload):
    print(PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--output-name", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--sample-rate", required=True, type=int)
    parser.add_argument("--use-gpu", action="store_true")
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    source = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    model_path = Path(args.model_path).resolve()
    expected = output_dir / args.output_name
    if not source.is_file() or not model_path.is_file():
        raise FileNotFoundError("Input или UVR-модель не найдены")
    output_dir.mkdir(parents=True, exist_ok=True)
    if not args.use_gpu:
        # audio-separator automatically selects CUDA when it is visible. Hiding
        # it before importing torch provides a deterministic CPU fallback.
        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
    started = time.monotonic()
    emit({"type": "progress", "stage": "stem_separation", "percent": None, "message": "Запуск Audio Separator", "elapsed_seconds": 0})
    from audio_separator.separator import Separator

    separator = Separator(
        log_level=20,
        model_file_dir=str(model_path.parent),
        output_dir=str(output_dir),
        output_format="FLAC",
        output_single_stem="Instrumental",
        sample_rate=args.sample_rate,
        normalization_threshold=1.0,
        amplification_threshold=0.0,
        mdx_params={"segment_size": 256, "overlap": 0.25, "batch_size": 1, "hop_length": 1024, "enable_denoise": False},
    )
    torch_device = str(separator.torch_device)
    onnx_provider = list(separator.onnx_execution_provider or [])
    emit(
        {
            "type": "environment",
            "message": f"Audio Separator device: {torch_device}; ONNX: {', '.join(onnx_provider)}",
            "torch_device": torch_device,
            "onnx_provider": onnx_provider,
            "elapsed_seconds": int(time.monotonic() - started),
        }
    )
    if args.use_gpu and (not torch_device.startswith("cuda") or "CUDAExecutionProvider" not in onnx_provider):
        raise RuntimeError("GPU processing was requested, but CUDA is not active in PyTorch and ONNX Runtime.")
    emit({"type": "progress", "stage": "stem_separation", "percent": None, "message": "Загрузка UVR-MDX-NET Inst HQ 3", "elapsed_seconds": int(time.monotonic() - started)})
    separator.load_model(model_filename=model_path.name)
    emit({"type": "progress", "stage": "stem_separation", "percent": None, "message": "Обработка сегментов", "elapsed_seconds": int(time.monotonic() - started)})
    heartbeat_stop = threading.Event()

    def heartbeat() -> None:
        while not heartbeat_stop.wait(20):
            emit(
                {
                    "type": "progress",
                    "stage": "stem_separation",
                    "percent": None,
                    "message": "Обработка сегментов (GPU активен)" if args.use_gpu else "Обработка сегментов (CPU)",
                    "elapsed_seconds": int(time.monotonic() - started),
                }
            )

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    try:
        output_files = separator.separate(str(source), {"Instrumental": expected.stem})
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)
    candidates = [Path(value) if Path(value).is_absolute() else output_dir / value for value in output_files]
    actual = next((path for path in candidates if path.is_file() and "instrumental" in path.stem.casefold()), None)
    if actual is None and expected.is_file():
        actual = expected
    if actual is None:
        raise FileNotFoundError(f"Audio Separator не вернул Instrumental: {output_files}")
    if actual.resolve() != expected.resolve():
        if expected.exists():
            raise FileExistsError(f"Ожидаемый файл уже существует: {expected}")
        os.replace(str(actual), str(expected))
    emit({"type": "result", "path": str(expected), "job_id": args.job_id, "elapsed_seconds": int(time.monotonic() - started)})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        emit({"type": "error", "message": str(exc)})
        raise
