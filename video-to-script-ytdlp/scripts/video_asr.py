"""Audio normalization and Azure-backed ASR helpers."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave

from video_common import VideoError, atomic_json, owned_file, private_directory, read_json, run_external


API_VERSION = "2024-06-01"
CHUNK_SECONDS = 480
EXPECTED_CHANNELS = 1
EXPECTED_SAMPLE_RATE = 16000
EXPECTED_SAMPLE_WIDTH = 2
MAX_ASR_FILE_BYTES = 24 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_STATE_BYTES = 64 * 1024 * 1024
STATE_VERSION = 1
WARNING_TEXT = "ASR times mark chunk boundaries, not word-aligned timestamps."
_ALLOWED_HOST_SUFFIXES = (".openai.azure.com", ".cognitiveservices.azure.com")


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sanitize_header(value, *, limit=128, token=""):
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", text) or (token and token in text) or "sk-" in text:
        return None
    return text[:limit]


def _atomic_text(path, value):
    path = Path(path)
    if path.is_symlink():
        raise VideoError("Refusing to replace a symlink.", code="unsafe_path")
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, UnicodeError):
        Path(temporary).unlink(missing_ok=True)
        raise


def _state_copy(state):
    return copy.deepcopy(state)


def _notify_progress(state, on_progress):
    if on_progress is not None:
        on_progress(_state_copy(state))


def _wave_metadata(path, *, chunk_limit=False):
    candidate = Path(path)
    try:
        size = candidate.stat().st_size
        with wave.open(str(candidate), "rb") as handle:
            channels = handle.getnchannels()
            sample_rate = handle.getframerate()
            sample_width = handle.getsampwidth()
            frame_count = handle.getnframes()
    except (OSError, wave.Error):
        raise VideoError("Audio must be a readable WAV file.", code="invalid_input") from None
    if channels != EXPECTED_CHANNELS or sample_rate != EXPECTED_SAMPLE_RATE or sample_width != EXPECTED_SAMPLE_WIDTH:
        raise VideoError("Audio must be normalized mono 16 kHz PCM.", code="invalid_input")
    if frame_count <= 0:
        raise VideoError("Audio must contain samples.", code="invalid_input")
    if size < frame_count * channels * sample_width:
        raise VideoError("Audio WAV is truncated.", code="invalid_input")
    if chunk_limit and size > MAX_ASR_FILE_BYTES:
        raise VideoError("Audio chunk exceeds the ASR size limit.", code="invalid_input")
    return {
        "channels": channels,
        "sample_rate": sample_rate,
        "sample_width": sample_width,
        "sample_count": frame_count,
        "size": size,
        "duration": frame_count / sample_rate,
    }


def _chunk_times(index, sample_rate, chunk_frames, total_frames):
    start_frame = (index - 1) * chunk_frames
    end_frame = min(total_frames, index * chunk_frames)
    return round(start_frame / sample_rate, 6), round(end_frame / sample_rate, 6)


def _state_path(job_dir):
    return private_directory(Path(job_dir) / "asr") / "state.json"


def _chunk_path(asr_dir, index, suffix):
    return asr_dir / f"chunk-{index:04d}{suffix}"


def _write_chunk_file(path, *, params, frames):
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.close(descriptor)
        with wave.open(str(temporary), "wb") as handle:
            handle.setnchannels(params.nchannels)
            handle.setsampwidth(params.sampwidth)
            handle.setframerate(params.framerate)
            handle.writeframes(frames)
        os.replace(temporary, path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise


def _build_initial_state(audio_path, job_dir, source_language):
    audio_path = Path(audio_path)
    metadata = _wave_metadata(audio_path)
    job_dir = Path(job_dir)
    asr_dir = private_directory(job_dir / "asr")
    existing = [child.name for child in asr_dir.iterdir() if child.name != "state.json"]
    if existing:
        raise VideoError("ASR working directory is not empty.", code="invalid_state")
    chunk_frames = CHUNK_SECONDS * EXPECTED_SAMPLE_RATE
    chunk_count = (metadata["sample_count"] + chunk_frames - 1) // chunk_frames
    state = {
        "version": STATE_VERSION,
        "source_sha256": _sha256_file(audio_path),
        "source_language": source_language,
        "chunksecs": CHUNK_SECONDS,
        "sample_count": metadata["sample_count"],
        "sample_rate": metadata["sample_rate"],
        "chunks": [],
    }
    with wave.open(str(audio_path), "rb") as reader:
        params = reader.getparams()
        for index in range(1, chunk_count + 1):
            frames = reader.readframes(chunk_frames)
            if not frames:
                break
            chunk_file = _chunk_path(asr_dir, index, ".wav")
            _write_chunk_file(chunk_file, params=params, frames=frames)
            chunk_meta = _wave_metadata(chunk_file, chunk_limit=True)
            start, end = _chunk_times(index, metadata["sample_rate"], chunk_frames, metadata["sample_count"])
            state["chunks"].append({
                "index": index,
                "start": start,
                "end": end,
                "audio_path": str(chunk_file.relative_to(job_dir)),
                "audio_sha256": _sha256_file(chunk_file),
                "audio_size": chunk_meta["size"],
                "status": "pending",
                "text_path": None,
                "text_sha256": None,
                "result_path": None,
                "result_sha256": None,
                "error_code": None,
            })
    atomic_json(asr_dir / "state.json", state)
    return state


def _load_state(state_path):
    state = read_json(state_path, limit=MAX_STATE_BYTES)
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION or not isinstance(state.get("chunks"), list):
        raise VideoError("ASR checkpoint is invalid.", code="invalid_state")
    return state


def _validate_state(state, *, audio_path, job_dir, source_language):
    metadata = _wave_metadata(audio_path)
    if state.get("source_sha256") != _sha256_file(audio_path):
        raise VideoError("ASR checkpoint does not match this audio.", code="invalid_state")
    if state.get("source_language") != source_language:
        raise VideoError("ASR checkpoint language does not match.", code="invalid_state")
    if state.get("chunksecs") != CHUNK_SECONDS:
        raise VideoError("ASR checkpoint chunking is incompatible.", code="invalid_state")
    if state.get("sample_count") != metadata["sample_count"] or state.get("sample_rate") != metadata["sample_rate"]:
        raise VideoError("ASR checkpoint does not match audio metadata.", code="invalid_state")
    expected_count = (metadata["sample_count"] + (CHUNK_SECONDS * EXPECTED_SAMPLE_RATE) - 1) // (CHUNK_SECONDS * EXPECTED_SAMPLE_RATE)
    if len(state["chunks"]) != expected_count:
        raise VideoError("ASR checkpoint is incomplete.", code="invalid_state")
    chunk_frames = CHUNK_SECONDS * EXPECTED_SAMPLE_RATE
    for index, chunk in enumerate(state["chunks"], start=1):
        if chunk.get("index") != index:
            raise VideoError("ASR checkpoint chunk order is invalid.", code="invalid_state")
        expected_start, expected_end = _chunk_times(index, metadata["sample_rate"], chunk_frames, metadata["sample_count"])
        if chunk.get("start") != expected_start or chunk.get("end") != expected_end:
            raise VideoError("ASR checkpoint chunk timing is invalid.", code="invalid_state")
        audio_file = owned_file(job_dir, Path(job_dir) / chunk.get("audio_path", ""))
        if not audio_file.is_file() or _sha256_file(audio_file) != chunk.get("audio_sha256"):
            raise VideoError("ASR checkpoint audio chunk is missing or changed.", code="invalid_state")
        _wave_metadata(audio_file, chunk_limit=True)
        status = chunk.get("status")
        if status not in {"pending", "in_flight", "complete", "failed", "uncertain"}:
            raise VideoError("ASR checkpoint status is invalid.", code="invalid_state")
        if status == "complete":
            for key_name, hash_name in (("text_path", "text_sha256"), ("result_path", "result_sha256")):
                artifact = owned_file(job_dir, Path(job_dir) / chunk.get(key_name, ""))
                expected_hash = chunk.get(hash_name)
                if not artifact.is_file() or not expected_hash or _sha256_file(artifact) != expected_hash:
                    raise VideoError("ASR checkpoint result is missing or changed.", code="invalid_state")
            text = (Path(job_dir) / chunk["text_path"]).read_text(encoding="utf-8")
            if not text.strip():
                raise VideoError("ASR checkpoint result is empty.", code="invalid_state")
            result = read_json(Path(job_dir) / chunk["result_path"])
            if not isinstance(result, dict) or result.get("text") != text or result.get("source_type") != "asr":
                raise VideoError("ASR checkpoint result is invalid.", code="invalid_state")
        elif chunk.get("text_path") or chunk.get("result_path"):
            raise VideoError("ASR checkpoint has partial artifacts.", code="invalid_state")
    return metadata


def _resolve_api_key(value, env):
    if isinstance(value, str):
        if not value:
            raise VideoError("ASR API key is empty.", code="configuration_error")
        return value
    if isinstance(value, dict):
        if value.get("source") == "env" and isinstance(value.get("id"), str):
            key_name = value["id"]
        elif "env" in value and isinstance(value["env"], str):
            key_name = value["env"]
        elif value.get("type") == "env" and isinstance(value.get("name"), str):
            key_name = value["name"]
        elif isinstance(value.get("secretRef"), dict) and isinstance(value["secretRef"].get("env"), str):
            key_name = value["secretRef"]["env"]
        else:
            raise VideoError("Unsupported ASR API key reference.", code="configuration_error")
        resolved = env.get(key_name)
        if not resolved:
            raise VideoError("Referenced ASR API key is unavailable.", code="configuration_error")
        return resolved
    raise VideoError("Unsupported ASR API key configuration.", code="configuration_error")


def _extract_legacy_audio_config(payload):
    if not isinstance(payload, dict):
        raise VideoError("ASR configuration is invalid.", code="configuration_error")
    for keys in (("tools", "media", "audio"), ("media", "audio")):
        cursor = payload
        try:
            for key in keys:
                cursor = cursor[key]
        except (KeyError, TypeError):
            continue
        payload = cursor
        break
    if not isinstance(payload, dict):
        raise VideoError("ASR configuration is invalid.", code="configuration_error")
    if payload.get("enabled") is False:
        raise VideoError("ASR is disabled by configuration.", code="configuration_error")
    return payload


def _validate_base_url(base_url):
    try:
        parsed = urllib.parse.urlsplit(str(base_url))
        port = parsed.port
    except ValueError:
        raise VideoError("ASR endpoint is invalid.", code="configuration_error") from None
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password or port or parsed.query or parsed.fragment:
        raise VideoError("ASR endpoint must be an HTTPS Azure endpoint.", code="configuration_error")
    hostname = (parsed.hostname or "").lower()
    if not hostname or not any(hostname.endswith(suffix) for suffix in _ALLOWED_HOST_SUFFIXES):
        raise VideoError("ASR endpoint host is unsupported.", code="configuration_error")
    path = parsed.path.rstrip("/")
    if path.endswith("/audio/transcriptions"):
        endpoint_path = path
    elif "/openai/deployments/" in path:
        endpoint_path = path + "/audio/transcriptions"
    else:
        raise VideoError("ASR endpoint path is unsupported.", code="configuration_error")
    return urllib.parse.urlunsplit(("https", hostname, endpoint_path, f"api-version={API_VERSION}", ""))


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AzureTranscriber:
    def __init__(self, config_path=None, opener=None, timeout=120, env=None):
        self.config_path = Path(config_path) if config_path is not None else Path.home() / ".openclaw/openclaw.json"
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener(_NoRedirectHandler)
        self._env = env if env is not None else os.environ

    def _load_config(self):
        env_base = self._env.get("VIDEO_ASR_BASE_URL")
        env_key = self._env.get("VIDEO_ASR_API_KEY")
        if bool(env_base) != bool(env_key):
            raise VideoError("Set both VIDEO_ASR_BASE_URL and VIDEO_ASR_API_KEY.", code="configuration_error")
        if env_base and env_key:
            return _validate_base_url(env_base), env_key
        if self.config_path is None:
            raise VideoError("ASR configuration is unavailable.", code="configuration_error")
        payload = read_json(self.config_path, limit=1024 * 1024)
        audio_config = _extract_legacy_audio_config(payload)
        headers = audio_config.get("headers")
        if not isinstance(headers, dict):
            raise VideoError("ASR headers are missing.", code="configuration_error")
        base_url = audio_config.get("baseUrl")
        api_key = _resolve_api_key(headers.get("api-key"), self._env)
        return _validate_base_url(base_url), api_key

    def transcribe(self, audio_file, *, language=None):
        if not (1 <= self.timeout <= 3600):
            raise VideoError("ASR timeout is invalid.", code="invalid_input")
        audio_path = Path(audio_file)
        _wave_metadata(audio_path, chunk_limit=True)
        endpoint, api_key = self._load_config()
        if not isinstance(api_key, str) or not re.fullmatch(r"[\x21-\x7e]+", api_key):
            raise VideoError("ASR credential format is invalid.", code="configuration_error")
        if language is not None and not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language):
            raise VideoError("ASR language is invalid.", code="invalid_input")
        audio_bytes = audio_path.read_bytes()
        boundary = uuid.uuid4().hex
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\ngpt-4o-transcribe\r\n".encode("utf-8"),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\njson\r\n".encode("utf-8"),
        ]
        if language is not None:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n{language}\r\n".encode("utf-8")
            )
        parts.append(
            (
                f"--{boundary}\r\n"
                "Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n"
                "Content-Type: audio/wav\r\n\r\n"
            ).encode("utf-8") + audio_bytes + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)
        if len(body) > MAX_ASR_FILE_BYTES + 64 * 1024:
            raise VideoError("ASR request exceeds the size limit.", code="invalid_input")
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
                "api-key": api_key,
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if getattr(response, "status", 200) not in (200, 201):
                    raise VideoError("ASR returned an unsuccessful status.", code="operation_failed")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise VideoError("ASR response exceeds the size limit.", code="invalid_output")
        except urllib.error.HTTPError as error:
            status = int(error.code)
            details = {"http_status": status}
            request_id = _sanitize_header(error.headers.get("x-request-id") or error.headers.get("apim-request-id"), token=api_key)
            error.close()
            if request_id:
                details["request_id"] = request_id
            if status in {401, 403}:
                raise VideoError("ASR authorization failed.", code="permission_denied", details=details) from None
            if status == 402:
                raise VideoError("ASR billing authorization failed.", code="billing_required", details=details) from None
            if status == 404:
                raise VideoError("ASR deployment endpoint is unavailable.", code="configuration_error", details=details) from None
            if status == 400:
                raise VideoError("ASR rejected the audio request.", code="invalid_input", details=details) from None
            raise VideoError("ASR request failed.", code="operation_failed", details=details) from None
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError):
            raise VideoError("ASR request outcome is unknown; retry explicitly.", code="unknown_outcome") from None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise VideoError("ASR response could not be decoded; outcome may already be billed.", code="unknown_outcome") from None
        if not isinstance(payload, dict):
            raise VideoError("ASR response has an invalid shape.", code="invalid_output")
        if payload.get("error"):
            raise VideoError("ASR returned a provider error.", code="operation_failed")
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise VideoError("ASR returned an empty transcript.", code="invalid_output")
        return text


def normalize_audio(input_path, job_dir, *, runner=run_external):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoError("ffmpeg is required for audio normalization.", code="dependency")
    input_path = Path(input_path)
    if not input_path.is_file():
        raise VideoError("Input media file is unavailable.", code="invalid_input")
    job_dir = Path(job_dir)
    media_dir = private_directory(job_dir / "media")
    output_path = media_dir / "normalized.wav"
    receipt_path = media_dir / "normalization.json"
    source_digest = _sha256_file(input_path)
    if output_path.exists():
        if output_path.is_symlink():
            raise VideoError("Normalized output must not be a symlink.", code="unsafe_path")
        receipt = read_json(receipt_path)
        if receipt.get("source_sha256") != source_digest or receipt.get("audio_sha256") != _sha256_file(output_path):
            raise VideoError("Existing normalized audio has no matching source receipt.", code="invalid_state")
        _wave_metadata(output_path)
        return output_path
    temporary = media_dir / (".normalize-" + uuid.uuid4().hex + ".wav")
    try:
        runner([
            ffmpeg,
            "-nostdin",
            "-i",
            str(input_path),
            "-vn",
            "-ar",
            str(EXPECTED_SAMPLE_RATE),
            "-ac",
            str(EXPECTED_CHANNELS),
            "-c:a",
            "pcm_s16le",
            "-loglevel",
            "error",
            "-n",
            str(temporary),
        ], cwd=job_dir, timeout=1200)
        _wave_metadata(temporary)
        os.replace(temporary, output_path)
    except (VideoError, OSError):
        temporary.unlink(missing_ok=True)
        raise
    os.chmod(output_path, 0o600)
    atomic_json(receipt_path, {"source_sha256": source_digest, "audio_sha256": _sha256_file(output_path)})
    return output_path


def transcribe_audio(audio_path, job_dir, *, source_language=None, retry_uncertain=False, client=None, on_progress=None):
    if client is None:
        client = AzureTranscriber()
    audio_path = Path(audio_path)
    job_dir = Path(job_dir).resolve()
    state_path = _state_path(job_dir)
    if state_path.exists():
        state = _load_state(state_path)
    else:
        state = _build_initial_state(audio_path, job_dir, source_language)
        _notify_progress(state, on_progress)
    _validate_state(state, audio_path=audio_path, job_dir=job_dir, source_language=source_language)
    for chunk in state["chunks"]:
        if chunk["status"] in {"in_flight", "uncertain"}:
            if not retry_uncertain:
                raise VideoError(
                    "ASR has an uncertain prior request; retry explicitly.",
                    code="unknown_outcome",
                    details={"chunk_index": chunk["index"], "chunk_count": len(state["chunks"])},
                )
            chunk["status"] = "pending"
            chunk["error_code"] = None
    atomic_json(state_path, state)
    _notify_progress(state, on_progress)
    for chunk in state["chunks"]:
        if chunk["status"] == "complete":
            continue
        if chunk["status"] != "pending" and chunk["status"] != "failed":
            raise VideoError("ASR checkpoint status is invalid.", code="invalid_state")
        chunk["status"] = "in_flight"
        chunk["error_code"] = None
        atomic_json(state_path, state)
        _notify_progress(state, on_progress)
        chunk_file = job_dir / chunk["audio_path"]
        try:
            text = client.transcribe(chunk_file, language=source_language)
            if not isinstance(text, str) or not text.strip():
                raise VideoError("ASR returned an empty transcript.", code="invalid_output")
            text_path = _chunk_path(state_path.parent, chunk["index"], ".txt")
            result_path = _chunk_path(state_path.parent, chunk["index"], ".json")
            _atomic_text(text_path, text)
            result = {"text": text, "start": chunk["start"], "end": chunk["end"], "source_type": "asr"}
            atomic_json(result_path, result)
            chunk["text_path"] = str(text_path.relative_to(job_dir))
            chunk["text_sha256"] = _sha256_file(text_path)
            chunk["result_path"] = str(result_path.relative_to(job_dir))
            chunk["result_sha256"] = _sha256_file(result_path)
            chunk["status"] = "complete"
            atomic_json(state_path, state)
            _notify_progress(state, on_progress)
        except VideoError as error:
            chunk["status"] = "uncertain" if error.code == "unknown_outcome" else "failed"
            chunk["error_code"] = error.code
            atomic_json(state_path, state)
            _notify_progress(state, on_progress)
            raise VideoError(
                error.message,
                code=error.code,
                details={**error.details, "chunk_index": chunk["index"], "chunk_count": len(state["chunks"])},
            ) from None
    segments = []
    text_parts = []
    for chunk in state["chunks"]:
        if chunk["status"] != "complete":
            raise VideoError("ASR did not complete all chunks.", code="invalid_state")
        result = read_json(job_dir / chunk["result_path"])
        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            raise VideoError("ASR checkpoint result is empty.", code="invalid_state")
        segments.append({"start": result["start"], "end": result["end"], "text": text})
        text_parts.append(text)
    return {
        "text": "\n\n".join(text_parts),
        "segments": segments,
        "source_type": "asr",
        "language": source_language,
        "raw_path": str(state_path),
        "warnings": [WARNING_TEXT],
    }
