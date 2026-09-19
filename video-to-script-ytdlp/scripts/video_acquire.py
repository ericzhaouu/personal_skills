from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import shutil
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from video_common import VideoError, private_directory, run_external

_MAX_OUTPUT = 8 * 1024 * 1024
_DEFAULT_AUDIO_MAX_BYTES = 250 * 1024 * 1024
_BASE_ARGS = [
    "--ignore-config",
    "--no-playlist",
    "--no-cache-dir",
    "--no-remote-components",
    "--retries",
    "0",
    "--fragment-retries",
    "0",
    "--socket-timeout",
    "20",
]
_ALLOWED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "bilibili.com",
    "b23.tv",
    "xiaohongshu.com",
    "xhslink.com",
    "xhslink.cn",
    "vimeo.com",
    "douyin.com",
    "iesdouyin.com",
    "tiktok.com",
    "x.com",
    "twitter.com",
    "ximalaya.com",
)
_CAPTION_EXTS = ("json3", "vtt", "srt")
_BLOCKED_LANGS = {"live_chat", "storyboard"}
_TAG_RE = re.compile(r"(?is)<br\s*/?>|</?[^>]+>")
_VTT_BLOCK_RE = re.compile(r"^(NOTE|STYLE|REGION)(?:\s|$)", re.IGNORECASE)
_LANG_CLEAN_RE = re.compile(r"[^a-z0-9-]+")


def probe(url, job_dir, *, cookie_file=None, ytdlp=None, runner=run_external) -> dict:
    parsed_url = _validate_public_url(url)
    work_dir = _ensure_dir(job_dir)
    copied_cookie = _prepare_cookie_file(cookie_file, work_dir)
    argv = _yt_dlp_base_command(copied_cookie, ytdlp)
    argv.extend(["--skip-download", "--dump-single-json", "--", parsed_url])
    stdout = runner(argv, cwd=work_dir, timeout=60, max_output=_MAX_OUTPUT)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise VideoError("yt-dlp returned invalid metadata", code="invalid_metadata") from exc
    if not isinstance(payload, dict):
        raise VideoError("yt-dlp returned unexpected metadata", code="invalid_metadata")
    if isinstance(payload.get("entries"), list):
        raise VideoError("Playlists are not supported", code="unsupported_media_type")

    video_id = _require_text(payload.get("id"), "Video metadata is missing an id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", video_id):
        raise VideoError("Video identity is not safe for artifact naming", code="invalid_metadata")
    title = _require_text(payload.get("title"), "Video metadata is missing a title")
    duration = _normalize_duration(payload.get("duration"))
    language = _normalize_lang(payload.get("language"))
    is_live = bool(payload.get("is_live")) or str(payload.get("live_status") or "").lower() not in {"", "not_live", "was_live"}
    webpage_url = _require_text(payload.get("webpage_url") or payload.get("original_url") or parsed_url, "Video metadata is missing a webpage URL")
    extractor = _require_text(payload.get("extractor") or payload.get("extractor_key"), "Video metadata is missing an extractor")
    return {
        "id": video_id,
        "title": title,
        "uploader": _optional_text(payload.get("uploader")),
        "duration": duration,
        "webpage_url": webpage_url,
        "extractor": extractor,
        "language": language,
        "is_live": is_live,
        "subtitles": _copy_caption_map(payload.get("subtitles")),
        "automatic_captions": _copy_caption_map(payload.get("automatic_captions")),
    }


def select_caption(metadata, source_language=None) -> dict | None:
    requested = _normalize_lang(source_language) or _normalize_lang(metadata.get("language"))
    if not requested:
        original_tracks = [key[:-5] for key in metadata.get("automatic_captions", {}) if key.endswith("-orig")]
        if len(original_tracks) == 1:
            requested = _normalize_lang(original_tracks[0])
    candidates = []
    for source_key, bucket in (
        ("manual_subtitles", metadata.get("subtitles")),
        ("automatic_subtitles", metadata.get("automatic_captions")),
    ):
        if not isinstance(bucket, dict):
            continue
        for download_language, entries in bucket.items():
            candidate = _build_caption_candidate(download_language, entries, source_key, requested)
            if candidate is not None and not candidate["translated"] and (not requested or candidate["match_rank"] > 0):
                candidates.append(candidate)
    if not candidates:
        return None
    candidates.sort(key=_caption_sort_key, reverse=True)
    best = candidates[0]
    return {
        "language": best["language"],
        "download_language": best["download_language"],
        "format": best["format"],
        "source_type": best["source_type"],
    }


def fetch_caption(url, metadata, track, job_dir, *, cookie_file=None, ytdlp=None, runner=run_external) -> dict:
    if not isinstance(track, dict):
        raise VideoError("Caption track selection is invalid", code="invalid_caption")
    parsed_url = _validate_public_url(url)
    work_dir = _ensure_dir(job_dir)
    copied_cookie = _prepare_cookie_file(cookie_file, work_dir)
    captions_dir = _ensure_dir(work_dir / "captions", mode=0o700)
    before = _snapshot_dir(captions_dir)
    track_language = _require_text(track.get("download_language") or track.get("language"), "Caption track is missing a language")
    track_format = _require_text(track.get("format"), "Caption track is missing a format").lower()
    if track_format not in _CAPTION_EXTS:
        raise VideoError("Caption format is unsupported", code="invalid_caption")

    argv = _yt_dlp_base_command(copied_cookie, ytdlp)
    argv.extend(
        [
            "--skip-download",
            "-o",
            str(captions_dir / "%(id)s.%(ext)s"),
            "--sub-langs",
            "^" + re.escape(track_language) + "$",
            "--sub-format",
            track_format,
        ]
    )
    source_type = track.get("source_type")
    if source_type == "manual_subtitles":
        argv.extend(["--write-subs", "--no-write-auto-subs"])
    elif source_type == "automatic_subtitles":
        argv.extend(["--write-auto-subs", "--no-write-subs"])
    else:
        raise VideoError("Caption source type is invalid", code="invalid_caption")
    argv.extend(["--", parsed_url])
    runner(argv, cwd=work_dir, timeout=120, max_output=_MAX_OUTPUT)
    raw_path = _find_new_caption_file(captions_dir, before, metadata.get("id"), track_format)
    if raw_path.stat().st_size > _MAX_OUTPUT:
        raise VideoError("Subtitle file exceeds the size limit", code="caption_too_large")
    raw_bytes = raw_path.read_bytes()
    segments = _parse_caption_bytes(raw_bytes, track_format)
    segments = _dedupe_overlapping_segments(segments)
    if not segments:
        raise VideoError("Caption file was empty", code="caption_empty")
    text = "\n".join(segment["text"] for segment in segments)
    return {
        "text": text,
        "segments": segments,
        "language": _normalize_lang(track.get("language")) or _normalize_lang(track_language),
        "source_type": source_type,
        "raw_path": str(raw_path),
        "warnings": [],
    }


def download_audio(url, metadata, job_dir, *, cookie_file=None, ytdlp=None, max_bytes=_DEFAULT_AUDIO_MAX_BYTES, runner=run_external) -> Path:
    parsed_url = _validate_public_url(url)
    work_dir = _ensure_dir(job_dir)
    copied_cookie = _prepare_cookie_file(cookie_file, work_dir)
    media_dir = _ensure_dir(work_dir / "media", mode=0o700)
    expected_id = _require_text(metadata.get("id"), "Video metadata is missing an id")

    argv = _yt_dlp_base_command(copied_cookie, ytdlp)
    argv.extend(
        [
            "--format",
            "bestaudio/best",
            "--restrict-filenames",
            "--no-overwrites",
            "--max-filesize",
            str(int(max_bytes)),
            "-o",
            str(media_dir / "%(id)s.%(ext)s"),
            "--print",
            "after_move:filepath",
            "--",
            parsed_url,
        ]
    )
    stdout = runner(argv, cwd=work_dir, timeout=300, max_output=_MAX_OUTPUT)
    output_path = _extract_download_path(stdout, media_dir)
    if output_path.stem != expected_id:
        raise VideoError("Downloaded audio does not match the requested video", code="download_mismatch")
    if output_path.is_symlink() or not output_path.is_file():
        raise VideoError("Downloaded audio file is invalid", code="download_invalid")
    size = output_path.stat().st_size
    if size > int(max_bytes):
        raise VideoError("Downloaded audio exceeds the size limit", code="download_too_large", details={"size": size, "max_bytes": int(max_bytes)})
    return output_path


def _validate_public_url(url: str) -> str:
    if not isinstance(url, str):
        raise VideoError("Video URL must be a string", code="invalid_url")
    value = url.strip()
    if any(ord(character) < 32 for character in value):
        raise VideoError("Video URL contains control characters", code="invalid_url")
    if not value or value.startswith("-"):
        raise VideoError("Video URL must be a public HTTP or HTTPS URL", code="invalid_url")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        raise VideoError("Video URL is malformed", code="invalid_url") from None
    if parsed.scheme not in {"http", "https"}:
        raise VideoError("Video URL must use HTTP or HTTPS", code="invalid_url")
    if parsed.username or parsed.password:
        raise VideoError("Video URL must not include credentials", code="invalid_url")
    if port is not None:
        raise VideoError("Video URL must not include a custom port", code="invalid_url")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or "localhost" in host:
        raise VideoError("Video URL host is not allowed", code="invalid_url")
    if re.fullmatch(r"\d+(?:\.\d+){3}", host):
        raise VideoError("Video URL host is not allowed", code="invalid_url")
    try:
        host_ascii = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise VideoError("Video URL host is invalid", code="invalid_url") from exc
    if not any(host_ascii == domain or host_ascii.endswith("." + domain) for domain in _ALLOWED_DOMAINS):
        raise VideoError("Video URL host is not supported", code="invalid_url")
    return value


def _ensure_dir(path, mode=0o700) -> Path:
    return private_directory(path)


def _prepare_cookie_file(cookie_file, job_dir: Path) -> Path | None:
    if cookie_file is None:
        return None
    source = Path(cookie_file)
    try:
        source_stat = source.stat()
    except FileNotFoundError as exc:
        raise VideoError("Cookie file does not exist", code="invalid_cookie_file") from exc
    if source.is_symlink() or not source.is_file():
        raise VideoError("Cookie file must be a regular non-symlink file", code="invalid_cookie_file")
    if not _owner_readable(source_stat.st_mode):
        raise VideoError("Cookie file must be owner-readable", code="invalid_cookie_file")
    target = job_dir / "cookies.txt"
    if target.is_symlink():
        raise VideoError("Cookie working copy must not be a symlink", code="invalid_cookie_file")
    digest = _hash_file(source)
    digest_path = job_dir / "cookies.txt.sha256"
    existing = digest_path.read_text(encoding="ascii").strip() if digest_path.exists() else None
    if existing != digest or not target.exists():
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        digest_path.write_text(digest, encoding="ascii")
        os.chmod(digest_path, 0o600)
    return target.resolve()


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _owner_readable(mode: int) -> bool:
    return bool(mode & stat.S_IRUSR) or os.name == "nt"


def _yt_dlp_base_command(cookie_file: Path | None, explicit) -> list[str]:
    binary = _resolve_ytdlp(explicit)
    argv = [binary]
    argv.extend(_BASE_ARGS)
    if shutil.which("node"):
        argv.extend(["--js-runtimes", "node"])
    if cookie_file is not None:
        argv.extend(["--cookies", str(cookie_file)])
    return argv


def _resolve_ytdlp(explicit) -> str:
    if explicit:
        return str(Path(explicit))
    env_value = os.environ.get("YTDLP_BIN")
    if env_value:
        return env_value
    discovered = shutil.which("yt-dlp")
    if discovered:
        return discovered
    fallback = Path.home() / ".openclaw" / "workspace" / "venv" / "bin" / "yt-dlp"
    if os.name == "nt" and not fallback.exists():
        exe_fallback = fallback.with_suffix(".exe")
        if exe_fallback.exists():
            return str(exe_fallback)
    return str(fallback)


def _copy_caption_map(value) -> dict:
    if not isinstance(value, dict):
        return {}
    copied = {}
    for language, entries in value.items():
        if not isinstance(entries, list):
            continue
        copied[str(language)] = [dict(entry) for entry in entries if isinstance(entry, dict)]
    return copied


def _build_caption_candidate(download_language, entries, source_type: str, requested: str | None) -> dict | None:
    language_key = _optional_text(download_language)
    if language_key is None:
        return None
    normalized = _normalize_lang(language_key)
    if not normalized or normalized in _BLOCKED_LANGS:
        return None
    if language_key.lower().endswith("-orig"):
        normalized = _normalize_lang(language_key[:-5]) or normalized
    entry = _pick_caption_entry(entries)
    if entry is None:
        return None
    translated = _looks_translated(entry) or _looks_translated({"name": language_key})
    match_rank = _language_match_rank(normalized, requested)
    fallback_rank = 2 if _primary_lang(normalized) == "en" else 1 if _primary_lang(normalized) == "zh" else 0
    return {
        "language": normalized,
        "download_language": language_key,
        "format": entry["ext"],
        "source_type": source_type,
        "translated": translated,
        "match_rank": match_rank,
        "fallback_rank": fallback_rank,
    }


def _pick_caption_entry(entries) -> dict | None:
    if not isinstance(entries, list):
        return None
    best = None
    for ext in _CAPTION_EXTS:
        for entry in entries:
            if isinstance(entry, dict) and str(entry.get("ext") or "").lower() == ext:
                best = dict(entry)
                best["ext"] = ext
                break
        if best is not None:
            return best
    return None


def _caption_sort_key(candidate: dict) -> tuple:
    manual_rank = 1 if candidate["source_type"] == "manual_subtitles" else 0
    translated_penalty = 0 if not candidate["translated"] else -1
    return (
        candidate["match_rank"],
        manual_rank,
        translated_penalty,
        candidate["fallback_rank"],
        -_CAPTION_EXTS.index(candidate["format"]),
        candidate["language"],
        candidate["download_language"],
    )


def _normalize_lang(value) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().replace("_", "-").lower()
    if not cleaned:
        return None
    if cleaned.endswith("-orig"):
        cleaned = cleaned[:-5]
    cleaned = _LANG_CLEAN_RE.sub("", cleaned)
    return cleaned or None


def _primary_lang(value: str | None) -> str | None:
    return value.split("-", 1)[0] if value else None


def _language_match_rank(language: str, requested: str | None) -> int:
    if not requested:
        return 1
    if language == requested:
        return 4
    if _primary_lang(language) == _primary_lang(requested):
        return 3
    return 0


def _looks_translated(entry: dict) -> bool:
    text = " ".join(str(entry.get(key) or "") for key in ("name", "format_note", "source", "language"))
    lowered = text.lower()
    url = entry.get("url")
    has_translation_target = isinstance(url, str) and bool(parse_qs(urlparse(url).query).get("tlang"))
    return has_translation_target or "translated" in lowered or "translation" in lowered


def _snapshot_dir(directory: Path) -> dict[Path, tuple[int, int]]:
    snapshot = {}
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        stat_result = path.stat()
        snapshot[path.resolve()] = (stat_result.st_mtime_ns, stat_result.st_size)
    return snapshot


def _find_new_caption_file(directory: Path, before: dict, expected_id, expected_ext: str) -> Path:
    after = _snapshot_dir(directory)
    candidates = []
    prefix = _require_text(expected_id, "Video metadata is missing an id")
    for path, current in after.items():
        if path.suffix.lower() != "." + expected_ext:
            continue
        if not path.name.startswith(prefix + "."):
            continue
        if before.get(path) != current:
            candidates.append(path)
    if len(candidates) != 1:
        raise VideoError("yt-dlp did not produce exactly one caption file", code="caption_missing")
    return candidates[0]


def _parse_caption_bytes(data: bytes, extension: str) -> list[dict]:
    if extension == "json3":
        return _parse_json3(data)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise VideoError("Caption text is not UTF-8", code="invalid_caption_format") from None
    if extension == "vtt":
        return _parse_vtt(text)
    if extension == "srt":
        return _parse_srt(text)
    raise VideoError("Caption format is unsupported", code="invalid_caption")


def _parse_json3(data: bytes) -> list[dict]:
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VideoError("Caption JSON is malformed", code="invalid_caption_format") from exc
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise VideoError("Caption JSON is malformed", code="invalid_caption_format")
    segments = []
    for event in events:
        if not isinstance(event, dict):
            continue
        parts = event.get("segs")
        if not isinstance(parts, list):
            continue
        text = _clean_caption_text("".join(seg.get("utf8", "") for seg in parts
                                          if isinstance(seg, dict) and isinstance(seg.get("utf8", ""), str)))
        if not text:
            continue
        start = _ms_to_seconds(event.get("tStartMs"))
        duration = _ms_to_seconds(event.get("dDurationMs"), allow_none=True)
        end = start if duration is None else start + duration
        if text:
            segments.append(_segment(start, end, text))
    return segments


def _parse_vtt(text: str) -> list[dict]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    segments = []
    index = 0
    if lines and lines[0].lstrip("\ufeff").startswith("WEBVTT"):
        index = 1
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if _VTT_BLOCK_RE.match(line):
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            continue
        if "-->" not in line:
            if index + 1 >= len(lines) or "-->" not in lines[index + 1]:
                raise VideoError("Caption VTT timestamps are malformed", code="invalid_caption_format")
            index += 1
            line = lines[index].strip()
        start_text, end_text = [part.strip() for part in line.split("-->", 1)]
        start = _parse_timestamp(start_text)
        end = _parse_timestamp(end_text.split(" ", 1)[0])
        index += 1
        block = []
        while index < len(lines) and lines[index].strip():
            block.append(lines[index])
            index += 1
        cleaned = _clean_caption_text("\n".join(block))
        if cleaned:
            segments.append(_segment(start, end, cleaned))
    return segments


def _parse_srt(text: str) -> list[dict]:
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip())
    segments = []
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if "-->" not in lines[0]:
            if len(lines) < 2 or "-->" not in lines[1]:
                raise VideoError("Caption SRT timestamps are malformed", code="invalid_caption_format")
            lines = lines[1:]
        start_text, end_text = [part.strip() for part in lines[0].split("-->", 1)]
        start = _parse_timestamp(start_text.replace(",", "."))
        end = _parse_timestamp(end_text.split(" ", 1)[0].replace(",", "."))
        cleaned = _clean_caption_text("\n".join(lines[1:]))
        if cleaned:
            segments.append(_segment(start, end, cleaned))
    return segments


def _clean_caption_text(text: str) -> str:
    if not text:
        return ""
    def replace_tag(match):
        token = match.group(0)
        return "\n" if token.lower().startswith("<br") else ""
    stripped = _TAG_RE.sub(replace_tag, text)
    stripped = html.unescape(stripped)
    lines = [re.sub(r"\s+", " ", part).strip() for part in stripped.splitlines()]
    return "\n".join(line for line in lines if line)


def _segment(start: float, end: float, text: str) -> dict:
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
        raise VideoError("Caption timestamps are malformed", code="invalid_caption_format")
    return {"start": start, "end": end, "text": text}


def _ms_to_seconds(value, allow_none=False) -> float | None:
    if value is None and allow_none:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise VideoError("Caption timestamps are malformed", code="invalid_caption_format") from exc
    if not math.isfinite(number) or number < 0:
        raise VideoError("Caption timestamps are malformed", code="invalid_caption_format")
    return number / 1000.0


def _parse_timestamp(value: str) -> float:
    match = re.fullmatch(r"(?:(\d+):)?(\d{2}):(\d{2})(?:\.(\d{1,3}))?", value.strip())
    if not match:
        raise VideoError("Caption timestamps are malformed", code="invalid_caption_format")
    hours = int(match.group(1) or "0")
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    millis = int((match.group(4) or "0").ljust(3, "0"))
    if minutes >= 60 or seconds >= 60:
        raise VideoError("Caption timestamps are malformed", code="invalid_caption_format")
    return hours * 3600.0 + minutes * 60.0 + seconds + millis / 1000.0


def _dedupe_overlapping_segments(segments: list[dict]) -> list[dict]:
    merged = []
    for segment in segments:
        if not merged:
            merged.append(segment)
            continue
        previous = merged[-1]
        if segment["start"] < previous["end"] and _rolling_related(previous["text"], segment["text"]):
            if len(segment["text"]) >= len(previous["text"]):
                previous["text"] = segment["text"]
            previous["end"] = max(previous["end"], segment["end"])
            continue
        merged.append(segment)
    return merged


def _rolling_related(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    return left.startswith(right) or left.endswith(right) or right.startswith(left) or right.endswith(left)


def _extract_download_path(stdout: str, media_dir: Path) -> Path:
    root = media_dir.resolve()
    candidates = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        candidate = Path(line)
        if candidate.is_symlink():
            raise VideoError("Downloaded path must not be a symlink", code="unsafe_path")
        resolved = candidate.resolve()
        if not _is_relative_to(resolved, root):
            continue
        if resolved.exists():
            candidates.append(resolved)
    if len(candidates) != 1:
        raise VideoError("yt-dlp did not report exactly one audio file", code="download_missing")
    return candidates[0]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalize_duration(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise VideoError("Video metadata has an invalid duration", code="invalid_metadata") from exc
    if not math.isfinite(number) or number <= 0:
        raise VideoError("Video metadata has an invalid duration", code="invalid_metadata")
    return number


def _optional_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_text(value, message: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise VideoError(message, code="invalid_metadata")
    return text
