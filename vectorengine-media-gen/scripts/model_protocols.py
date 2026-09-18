import math
import re

from image_transport import GenerationError, decode_base64_image, extract_request_id


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PIXELS_RE = re.compile(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$")
_RATIO_RE = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*$")
_ALLOWED_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp"}
_SUPPORTED_GEMINI_RATIOS = {
    "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9",
}
_GPT_LIMITS = {"min_pixels": 655360, "max_pixels": 8294400, "max_edge": 3840}
_GPT_TARGETS = {"1K": 1048576, "2K": 4194304, "4K": 8294400}
_GPT_EXACT = {
    "1K": {"1:1": (1024, 1024), "3:4": (864, 1152), "4:3": (1152, 864), "16:9": (1536, 864),
           "9:16": (864, 1536), "2:3": (832, 1248), "3:2": (1248, 832)},
    "2K": {"1:1": (2048, 2048), "3:4": (1728, 2304), "4:3": (2304, 1728), "16:9": (3072, 1728),
           "9:16": (1728, 3072), "2:3": (1664, 2496), "3:2": (2496, 1664)},
}
_SEEDREAM_GEOMETRY = {
    "1K": {"1:1": (1024, 1024), "4:3": (1152, 864), "3:4": (864, 1152), "16:9": (1424, 800),
           "9:16": (800, 1424), "3:2": (1248, 832), "2:3": (832, 1248), "21:9": (1568, 672)},
    "2K": {"1:1": (2048, 2048), "4:3": (2368, 1776), "3:4": (1776, 2368), "16:9": (2816, 1584),
           "9:16": (1584, 2816), "3:2": (2496, 1664), "2:3": (1664, 2496), "21:9": (3136, 1344)},
}


def _fail(message, *, category="invalid_response", status=None, request_id=None, can_fallback=False):
    raise GenerationError(message, category=category, status=status, request_id=request_id,
                          can_fallback=can_fallback)


def _parse_ratio(value):
    match = _RATIO_RE.fullmatch(value or "")
    if not match:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        _fail("Aspect ratio must be positive.", category="input")
    if max(width, height) / min(width, height) > 3:
        _fail("Aspect ratio exceeds the 3:1 provider limit.", category="input")
    scale = math.gcd(width, height)
    return width // scale, height // scale


def _parse_pixels(value):
    match = _PIXELS_RE.fullmatch(value or "")
    if not match:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        _fail("Pixel dimensions must be positive.", category="input")
    return width, height


def _sanitize_request_id(value):
    if isinstance(value, str):
        value = value.strip()
        if _REQUEST_ID_RE.fullmatch(value) and "sk-" not in value.lower():
            return value
    return None


def _extract_request_id(value):
    return extract_request_id(value)


def _extract_error_message(payload):
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            error = current.get("error")
            if error:
                return "Provider returned an error.", _extract_request_id(error) or _extract_request_id(payload)
            if current.get("success") is False:
                return "Provider reported an unsuccessful result.", _extract_request_id(payload)
            stack.extend(item for item in current.values() if isinstance(item, (dict, list)))
        elif isinstance(current, list):
            stack.extend(item for item in current if isinstance(item, (dict, list)))
    return None, None


def _exact_multiples_of_sixteen(numerator, denominator, target_pixels):
    unit = math.lcm(16 // math.gcd(16, numerator), 16 // math.gcd(16, denominator))
    best = None
    for scale in range(1, 2049):
        width = numerator * unit * scale
        height = denominator * unit * scale
        pixels = width * height
        if width > _GPT_LIMITS["max_edge"] or height > _GPT_LIMITS["max_edge"]:
            break
        if pixels < _GPT_LIMITS["min_pixels"] or pixels > _GPT_LIMITS["max_pixels"]:
            continue
        candidate = (abs(pixels - target_pixels), -pixels, width, height)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        _fail("Requested ratio cannot satisfy GPT image geometry rules.", category="input")
    return best[2], best[3]


def _build_gpt_geometry(size, resolution):
    explicit = _parse_pixels(size)
    if explicit is not None:
        width, height = explicit
        if width % 16 or height % 16:
            _fail("Explicit size must use dimensions that are multiples of 16.", category="input")
        pixels = width * height
        if max(width, height) > _GPT_LIMITS["max_edge"] or pixels < _GPT_LIMITS["min_pixels"] or pixels > _GPT_LIMITS["max_pixels"]:
            _fail("Explicit size violates GPT image geometry limits.", category="input")
        if max(width, height) / min(width, height) > 3:
            _fail("Explicit size exceeds the 3:1 provider limit.", category="input")
        settings = {"requested_size": size, "applied_size": f"{width}x{height}", "count": None, "resolution": resolution,
                    "resolution_ignored": True, "experimental_resolution": pixels > 2560 * 1440}
        return f"{width}x{height}", settings
    ratio = _parse_ratio(size)
    if ratio is None:
        _fail("Size must be WIDTHxHEIGHT or a supported aspect ratio.", category="input")
    ratio_text = f"{ratio[0]}:{ratio[1]}"
    width, height = _GPT_EXACT.get(resolution, {}).get(ratio_text, (None, None))
    if width is None:
        width, height = _exact_multiples_of_sixteen(ratio[0], ratio[1], _GPT_TARGETS[resolution])
    settings = {"requested_size": size, "applied_size": f"{width}x{height}", "aspect_ratio": ratio_text,
                "resolution": resolution, "resolution_ignored": False,
                "experimental_resolution": width * height > 2560 * 1440}
    return f"{width}x{height}", settings


def _build_gpt_request(model, config, prompt, *, size, resolution, count):
    if len(prompt) > 1000:
        _fail("Prompt exceeds the GPT image 1000 character limit.", category="input")
    provider_size, settings = _build_gpt_geometry(size, resolution)
    payload = {
        "model": model,
        "prompt": prompt,
        "n": count,
        "size": provider_size,
        "format": config["defaults"]["format"],
        "quality": config["defaults"]["quality"],
        "response_format": config["defaults"]["response_format"],
    }
    settings["count"] = count
    return config["endpoint"], payload, settings


def _ratio_from_pixels(size):
    width, height = _parse_pixels(size) or (None, None)
    if width is None:
        return None
    scale = math.gcd(width, height)
    return f"{width // scale}:{height // scale}"


def _build_gemini_request(model, config, prompt, *, size, resolution, count):
    if count != 1:
        _fail("Gemini image generation currently supports only --num 1.", category="input")
    ratio_text = _ratio_from_pixels(size) or (":".join(map(str, _parse_ratio(size))) if _parse_ratio(size) else None)
    if ratio_text not in _SUPPORTED_GEMINI_RATIOS:
        _fail("Gemini requires one of the documented native aspect ratios.", category="input")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": ratio_text, "imageSize": resolution},
        },
    }
    settings = {"requested_size": size, "aspect_ratio": ratio_text, "native_image_size": resolution,
                "count": 1, "provider_returns_native_geometry": True}
    if _parse_pixels(size) is not None:
        settings["explicit_pixels_used_for_ratio_only"] = True
    return config["endpoint"].format(model=model), payload, settings


def _build_seedream_request(model, config, prompt, *, size, resolution, count):
    if count != 1:
        _fail("Seedream initial integration is restricted to --num 1.", category="input")
    if resolution == "4K":
        _fail("Seedream does not document a 4K request size.", category="unsupported")
    explicit = _parse_pixels(size)
    if explicit is not None:
        provider_size = f"{explicit[0]}x{explicit[1]}"
        allowed = {f"{w}x{h}" for values in _SEEDREAM_GEOMETRY.values() for (w, h) in values.values()}
        if provider_size not in allowed:
            _fail("Seedream explicit size is not in the documented enum.", category="input")
        settings = {"requested_size": size, "applied_size": provider_size, "count": 1,
                    "resolution": resolution, "resolution_ignored": True}
    else:
        ratio = _parse_ratio(size)
        if ratio is None:
            _fail("Seedream size must be WIDTHxHEIGHT or a documented aspect ratio.", category="input")
        ratio_text = f"{ratio[0]}:{ratio[1]}"
        geometry = _SEEDREAM_GEOMETRY[resolution].get(ratio_text)
        if geometry is None:
            _fail("Seedream does not document that aspect ratio at the requested native size.", category="input")
        provider_size = f"{geometry[0]}x{geometry[1]}"
        settings = {"requested_size": size, "applied_size": provider_size, "aspect_ratio": ratio_text,
                    "resolution": resolution, "resolution_ignored": False, "count": 1}
        if ratio_text == "16:9" and resolution == "1K":
            settings["geometry_note"] = "Applied documented near-16:9 native size 1424x800."
    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": provider_size,
        "output_format": config["defaults"]["output_format"],
        "response_format": config["defaults"]["response_format"],
        "watermark": config["defaults"]["watermark"],
    }
    return config["endpoint"], payload, settings


def build_request(model, config, prompt, *, size, resolution, count):
    protocol = config.get("protocol")
    if protocol == "gpt-images-v1":
        return _build_gpt_request(model, config, prompt, size=size, resolution=resolution, count=count)
    if protocol == "gemini-generate-content":
        return _build_gemini_request(model, config, prompt, size=size, resolution=resolution, count=count)
    if protocol == "seedream-v3":
        return _build_seedream_request(model, config, prompt, size=size, resolution=resolution, count=count)
    _fail("Unknown model protocol.", category="configuration")


def _image_from_record(item, transport):
    if not isinstance(item, dict):
        _fail("Image record must be an object.")
    if "b64_json" in item:
        return decode_base64_image(item["b64_json"])
    if "url" in item:
        return transport.get_public_image(item["url"])
    if "error" in item:
        message, request_id = _extract_error_message(item)
        _fail(message or "Provider returned an image item error.", request_id=request_id)
    refusal = item.get("refusal") or item.get("message")
    if isinstance(refusal, str) and refusal:
        _fail("Provider refused the image request.", category="policy")
    _fail("Provider returned no image bytes.")


def _extract_gpt_images(model, response, transport):
    if response.get("model") and response["model"] != model:
        _fail("Provider model mismatch in response.")
    data = response.get("data")
    if not isinstance(data, list) or not data:
        _fail("GPT response did not include image data.")
    images = []
    for item in data:
        if isinstance(item, dict) and item.get("model") and item["model"] != model:
            _fail("Provider model mismatch in image item.")
        images.append(_image_from_record(item, transport))
    return images


def _part_inline_bytes(part):
    blob = part.get("inlineData") or part.get("inline_data")
    if not isinstance(blob, dict):
        return None
    mime = blob.get("mimeType") or blob.get("mime_type")
    if not isinstance(mime, str):
        _fail("Gemini returned an invalid inline image MIME type.")
    mime = mime.lower()
    if mime not in _ALLOWED_IMAGE_MIME:
        _fail("Gemini returned an unsupported inline image MIME type.")
    return decode_base64_image(blob.get("data"))


def _part_url_bytes(part, transport):
    blob = part.get("fileData") or part.get("file_data")
    if not isinstance(blob, dict):
        return None
    mime = blob.get("mimeType") or blob.get("mime_type")
    if not isinstance(mime, str):
        _fail("Gemini returned an invalid file image MIME type.")
    mime = mime.lower()
    url = blob.get("fileUri") or blob.get("file_uri") or blob.get("uri") or blob.get("url")
    if mime not in _ALLOWED_IMAGE_MIME:
        _fail("Gemini returned an unsupported file image MIME type.")
    if not isinstance(url, str) or not url:
        _fail("Gemini returned an invalid image URL.")
    return transport.get_public_image(url)


def _extract_gemini_images(response, transport):
    prompt_feedback = response.get("promptFeedback")
    if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
        _fail("Gemini blocked the prompt.", category="policy")
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        _fail("Gemini response did not include candidates.")
    images = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            _fail("Gemini candidate must be an object.")
        finish = candidate.get("finishReason") or candidate.get("finish_reason")
        if finish and finish != "STOP":
            category = "policy" if finish == "SAFETY" else "invalid_response"
            _fail("Gemini did not finish successfully.", category=category)
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            image = _part_inline_bytes(part)
            if image is None:
                image = _part_url_bytes(part, transport)
            if image is not None:
                images.append(image)
    if not images:
        _fail("Gemini returned no image parts.")
    return images


def _extract_seedream_images(response, transport):
    code = response.get("code")
    if code not in (None, 0):
        _fail("Seedream returned an error.", request_id=_extract_request_id(response))
    data = response.get("data")
    if isinstance(data, dict) and _sanitize_request_id(data.get("task_id")) and not isinstance(data.get("images"), list):
        _fail("accepted task has no documented polling route; not resubmitting",
              category="pending", request_id=data["task_id"])
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get("images"), list):
        items = data["images"]
    else:
        _fail("Seedream response did not include final images.")
    if not items:
        _fail("Seedream response returned an empty image list.")
    return [_image_from_record(item, transport) for item in items]


def extract_images(model, config, response, transport):
    if not isinstance(response, dict):
        _fail("Response must be a JSON object.")
    message, request_id = _extract_error_message(response)
    if message:
        _fail(message, request_id=request_id)
    protocol = config.get("protocol")
    if protocol == "gpt-images-v1":
        return _extract_gpt_images(model, response, transport)
    if protocol == "gemini-generate-content":
        return _extract_gemini_images(response, transport)
    if protocol == "seedream-v3":
        return _extract_seedream_images(response, transport)
    _fail("Unknown model protocol.", category="configuration")
