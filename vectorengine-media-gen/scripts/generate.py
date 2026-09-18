#!/usr/bin/env python3
"""Generate images through RelayRouter; retain the vectorengine-media-gen CLI."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

from image_transport import GenerationError, ImageTransport, inspect_image, save_images
from model_protocols import build_request, extract_images


SKILL_DIR = Path(__file__).resolve().parent.parent
REGISTRY_PATH = SKILL_DIR / "config" / "models.json"
MODEL_ORDER = (
    "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst",
    "gemini-3.1-flash-image",
    "doubao-seedream-5-0-pro-260628",
)
DEFAULT_IMAGE_MODEL = MODEL_ORDER[0]
OUTPUT_DIR = Path.home() / ".openclaw" / "media" / "vecengine" / "pic"


def load_registry(path=REGISTRY_PATH):
    try:
        registry = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise GenerationError("Cannot read the model registry.", category="configuration") from None
    if not isinstance(registry, dict) or registry.get("schemaVersion") != 2:
        raise GenerationError("Unsupported registry schema.", category="configuration")
    models = registry.get("image")
    if not isinstance(models, dict) or tuple(models) != MODEL_ORDER:
        raise GenerationError("Registry must contain exactly the four approved models in order.",
                              category="configuration")
    if registry.get("defaultModel") != DEFAULT_IMAGE_MODEL:
        raise GenerationError("Registry default model is inconsistent.", category="configuration")
    if not isinstance(registry.get("baseUrl"), str):
        raise GenerationError("Registry base URL is missing.", category="configuration")
    if not isinstance(registry.get("allowedApiHosts"), list) or not registry["allowedApiHosts"]:
        raise GenerationError("Registry API host allowlist is missing.", category="configuration")
    for model, config in models.items():
        if not isinstance(config, dict) or config.get("modelId") != model:
            raise GenerationError("Registry model identity is inconsistent.", category="configuration")
    return registry


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media", required=True, choices=("image", "video", "audio"))
    parser.add_argument("--prompt", "-p")
    parser.add_argument("--model", "-m", help="Exact registered model; disables automatic fallback")
    parser.add_argument("--size", "-s", default="3:4", help="Requested aspect ratio or supported pixel size")
    parser.add_argument("--resolution", "-r", default="1K", choices=("1K", "2K", "4K"))
    parser.add_argument("--num", "-n", type=int, default=1, help="Images requested, 1-4; model limits apply")
    parser.add_argument("--output-dir", "-o", type=Path)
    parser.add_argument("--timeout", type=int, default=300, help="Per-request timeout, 10-600 seconds")
    parser.add_argument("--list-models", action="store_true")
    return parser


def list_models(registry):
    print("Available image models:")
    for model, config in registry["image"].items():
        default = " (default)" if model == DEFAULT_IMAGE_MODEL else ""
        print("  {} [family={}]{}".format(model, config["family"], default))


def safe_error(error, model=None):
    return {
        "error": True,
        "category": error.category,
        "status": error.status,
        "request_id": error.request_id,
        "message": error.message,
        "model": model,
    }


def generate(args, registry, *, transport_factory=ImageTransport):
    if args.media != "image":
        raise GenerationError("Video and audio generation are not implemented.", category="unsupported")
    if not isinstance(args.prompt, str) or not args.prompt.strip():
        raise GenerationError("A nonempty --prompt is required.", category="input")
    if type(args.num) is not int or not 1 <= args.num <= 4:
        raise GenerationError("--num must be between 1 and 4.", category="input")
    if not 10 <= args.timeout <= 600:
        raise GenerationError("--timeout must be between 10 and 600 seconds.", category="input")
    if args.model is not None and args.model not in registry["image"]:
        raise GenerationError("Unknown model; old model IDs and aliases were removed.", category="input")
    if importlib.util.find_spec("PIL") is None:
        raise GenerationError("Pillow is required; install this Skill's requirements.txt.",
                              category="dependency")
    token = os.environ.get("VECENGINE_API_TOKEN", "")
    if not token:
        raise GenerationError("VECENGINE_API_TOKEN is required.", category="authentication")
    base_url = os.environ.get("RELAYROUTER_BASE_URL", registry["baseUrl"])
    try:
        transport = transport_factory(base_url, token, timeout=args.timeout,
                                      allowed_api_hosts=registry["allowedApiHosts"])
    except ValueError:
        raise GenerationError("Invalid API credential or transport configuration.",
                              category="configuration") from None
    selected = (args.model,) if args.model else MODEL_ORDER
    attempts = []
    last_error = None
    for model in selected:
        config = registry["image"][model]
        path, payload, requested = build_request(
            model, config, args.prompt, size=args.size, resolution=args.resolution, count=args.num,
        )
        print("[{}] submitting once".format(model), file=sys.stderr)
        try:
            response = transport.request_json(path, payload)
            images = extract_images(model, config, response, transport)
            if len(images) != args.num:
                raise GenerationError("Returned image count does not match --num; not resubmitting.",
                                      category="invalid_response")
            metadata = [inspect_image(image) for image in images]
            output_dir = (args.output_dir or OUTPUT_DIR).expanduser().resolve()
            saved = save_images(images, str(output_dir))
        except GenerationError as error:
            error.model = model
            attempts.append(safe_error(error, model))
            print(json.dumps(attempts[-1], ensure_ascii=True), file=sys.stderr)
            last_error = error
            if args.model or not error.can_fallback:
                raise
            continue
        attempts.append({"model": model, "status": "succeeded"})
        for path in saved:
            print("SAVED: {} ({} bytes)".format(path, Path(path).stat().st_size))
        summary = {
            "media": "image", "files": saved, "model": model, "prompt": args.prompt,
            "images": metadata, "requested_settings": requested, "attempts": attempts,
        }
        print(json.dumps(summary, ensure_ascii=False))
        return summary
    if last_error is not None:
        raise last_error
    raise GenerationError("No model was attempted.", category="configuration")


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        registry = load_registry()
        if args.list_models:
            if args.media != "image":
                raise GenerationError("Video and audio models are not implemented.", category="unsupported")
            list_models(registry)
            return 0
        generate(args, registry)
        return 0
    except GenerationError as error:
        print(json.dumps(safe_error(error, getattr(error, "model", args.model)), ensure_ascii=True))
        return 1
    except OSError:
        print(json.dumps({"error": True, "category": "filesystem",
                          "message": "Unable to save images; no generation retry was attempted."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
