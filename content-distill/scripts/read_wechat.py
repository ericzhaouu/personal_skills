#!/usr/bin/env python3
"""Read WeChat article content through the local TikHub skill."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

MAGIC = "content-distill:wechat-cache:v1"
CACHE_FORMAT = "content-distill-wechat-cache"
SCHEMA_VERSION = 1
PURPOSE = "wechat article cache"
MAX_CACHE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_CHARS = 6000
MAX_READ_CHARS = 12000


class ControlledError(Exception):
    """Safe error shown to the caller."""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ControlledError("Invalid command arguments; use --help.")


def _emit(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _safe_error(message):
    return {"error": True, "message": message}


def _iso_utc(now=None):
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_wechat_url(url):
    if not isinstance(url, str) or not url.strip():
        raise ControlledError("URL must be a non-empty string.")
    if any(ord(char) < 32 for char in url):
        raise ControlledError("URL must not contain control characters.")
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise ControlledError("URL is not valid.") from None
    if parts.scheme.lower() not in {"http", "https"} or parts.hostname is None:
        raise ControlledError("URL must use http or https.")
    if parts.username is not None or parts.password is not None:
        raise ControlledError("URL must not include userinfo.")
    try:
        port = parts.port
    except ValueError:
        raise ControlledError("URL port is invalid.") from None
    if port is not None:
        raise ControlledError("URL must not include a port.")
    if parts.hostname.lower() != "mp.weixin.qq.com":
        raise ControlledError("URL hostname is not allowed.")
    path = parts.path or "/"
    if path != "/s" and not (path.startswith("/s/") and path.count("/") == 2 and path != "/s/"):
        raise ControlledError("URL path is not allowed.")
    return urlunsplit((parts.scheme.lower(), parts.hostname.lower(), path, parts.query, ""))


def _safe_compare_key(url):
    parts = urlsplit(url)
    return (parts.hostname.lower(), parts.path, parts.query)


def _resolve_tikhub_script(tikhub_script=None):
    script = Path(tikhub_script) if tikhub_script is not None else Path(__file__).resolve().parents[2] / "tikhub" / "tikhub.py"
    if not script.is_file():
        raise ControlledError("TikHub script is not installed.")
    return script


def _check_output_path(output_path):
    parent = output_path.parent
    if not parent.exists():
        raise ControlledError("Output parent directory does not exist.")
    if not parent.is_dir():
        raise ControlledError("Output parent directory is not a directory.")
    if os.path.islink(output_path):
        raise ControlledError("Output path must not be a symlink.")
    if os.path.lexists(output_path):
        raise ControlledError("Output file already exists.")


def _parse_code(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{3}", value):
        return int(value)
    raise ControlledError("TikHub response code is invalid.")


def _safe_request_id(value):
    if isinstance(value, str):
        trimmed = value.strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", trimmed) and "sk-" not in trimmed.lower():
            return trimmed
    return None


def _published_at(value):
    if type(value) is int and value > 0:
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            raise ControlledError("TikHub publication timestamp is invalid.") from None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _build_cache(requested_url, payload, *, now=None):
    if not isinstance(payload, dict):
        raise ControlledError("TikHub response is not an object.")
    if payload.get("error") not in (None, False, 0, "0", ""):
        raise ControlledError("TikHub fetch failed.")
    if _parse_code(payload.get("code")) != 200:
        raise ControlledError("TikHub fetch did not succeed.")
    tikhub_meta = payload.get("_tikhub")
    if not isinstance(tikhub_meta, dict) or tikhub_meta.get("status") != "ok":
        raise ControlledError("TikHub fetch did not return usable content.")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ControlledError("TikHub response data is missing.")
    returned_url = data.get("url", requested_url)
    if not isinstance(returned_url, str):
        raise ControlledError("TikHub response source URL is invalid.")
    source_url = _validate_wechat_url(returned_url)
    content = data.get("content")
    if not isinstance(content, dict):
        raise ControlledError("TikHub response content is missing.")
    title = content.get("title")
    if not isinstance(title, str):
        raise ControlledError("TikHub response title is invalid.")
    content_text = content.get("content_text")
    if not isinstance(content_text, str) or not content_text.strip():
        raise ControlledError("TikHub response content text is invalid.")
    author = None
    for key in ("nick_name", "user_name"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            author = value.strip()
            break
    source = {
        "source_kind": "wechat",
        "requested_url": requested_url,
        "source_url": source_url,
        "source_url_matches_requested": _safe_compare_key(requested_url) == _safe_compare_key(source_url),
        "title": title,
        "character_count": len(content_text),
        "retrieved_at": _iso_utc(now),
        "completeness": "unassessed",
    }
    if author is not None:
        source["author"] = author
    published_at = _published_at(content.get("create_time"))
    if published_at is not None:
        source["published_at"] = published_at
    request_id = _safe_request_id(payload.get("request_id"))
    if request_id is not None:
        source["request_id"] = request_id
    return {
        "magic": MAGIC,
        "format": CACHE_FORMAT,
        "schemaVersion": SCHEMA_VERSION,
        "purpose": PURPOSE,
        "source": source,
        "content": {"content_text": content_text},
    }


def _exclusive_json_write(output_path, payload):
    try:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        raise ControlledError("TikHub returned invalid Unicode content.") from None
    if len(data) > MAX_CACHE_BYTES:
        raise ControlledError("Article cache exceeds the size limit.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(output_path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        if os.path.exists(output_path):
            os.unlink(output_path)
        raise


def fetch(url, output_path, *, runner=subprocess.run, tikhub_script=None, now=None):
    requested_url = _validate_wechat_url(url)
    output_path = Path(output_path).absolute()
    _check_output_path(output_path)
    script = _resolve_tikhub_script(tikhub_script)
    command = [
        sys.executable,
        str(script),
        "fetch",
        requested_url,
        "--compact",
        "--retries",
        "0",
        "--timeout",
        "45",
    ]
    try:
        completed = runner(command, capture_output=True, text=True, encoding="utf-8", timeout=65, check=False)
    except subprocess.TimeoutExpired:
        raise ControlledError("TikHub fetch timed out.") from None
    except (OSError, UnicodeDecodeError):
        raise ControlledError("Failed to run TikHub fetch.") from None
    if completed.returncode != 0:
        raise ControlledError("TikHub fetch failed.")
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, RecursionError):
        raise ControlledError("TikHub returned invalid JSON.") from None
    cache = _build_cache(requested_url, payload, now=now)
    try:
        _exclusive_json_write(output_path, cache)
    except FileExistsError:
        raise ControlledError("Output file already exists.") from None
    except OSError:
        raise ControlledError("Failed to write cache file.") from None
    source = cache["source"]
    result = {
        "ok": True,
        "cache_file": str(output_path),
        "source": source,
        "character_count": source["character_count"],
        "requested_url": source["requested_url"],
        "source_url": source["source_url"],
        "source_url_matches_requested": source["source_url_matches_requested"],
        "title": source["title"],
    }
    if "author" in source:
        result["author"] = source["author"]
    if "published_at" in source:
        result["published_at"] = source["published_at"]
    return result


def _load_cache(cache_file):
    cache_path = Path(cache_file)
    try:
        size = cache_path.stat().st_size
    except FileNotFoundError:
        raise ControlledError("Cache file does not exist.") from None
    except PermissionError:
        raise ControlledError("Cache file is not readable.") from None
    except IsADirectoryError:
        raise ControlledError("Cache path must be a file.") from None
    if size > MAX_CACHE_BYTES:
        raise ControlledError("Cache file is too large.")
    try:
        with cache_path.open("rb") as handle:
            data = handle.read(MAX_CACHE_BYTES + 1)
        if len(data) > MAX_CACHE_BYTES:
            raise ControlledError("Cache file is too large.")
        raw_text = data.decode("utf-8")
    except FileNotFoundError:
        raise ControlledError("Cache file does not exist.") from None
    except PermissionError:
        raise ControlledError("Cache file is not readable.") from None
    except IsADirectoryError:
        raise ControlledError("Cache path must be a file.") from None
    except UnicodeDecodeError:
        raise ControlledError("Cache file is not valid UTF-8.") from None
    try:
        payload = json.loads(raw_text)
    except (json.JSONDecodeError, RecursionError):
        raise ControlledError("Cache file is not valid JSON.") from None
    if not isinstance(payload, dict):
        raise ControlledError("Cache file is not an object.")
    if payload.get("magic") != MAGIC or payload.get("format") != CACHE_FORMAT or payload.get("schemaVersion") != SCHEMA_VERSION or payload.get("purpose") != PURPOSE:
        raise ControlledError("Cache file format is not recognized.")
    source = payload.get("source")
    content = payload.get("content")
    if not isinstance(source, dict) or not isinstance(content, dict):
        raise ControlledError("Cache file is incomplete.")
    content_text = content.get("content_text")
    character_count = source.get("character_count")
    if source.get("source_kind") != "wechat" or not isinstance(content_text, str) or type(character_count) is not int:
        raise ControlledError("Cache file contents are invalid.")
    if character_count != len(content_text):
        raise ControlledError("Cache file character count is inconsistent.")
    try:
        json.dumps({"source": source, "content_text": content_text}, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise ControlledError("Cache file contains invalid Unicode.") from None
    return source, content_text


def read(cache_file, *, offset=0, max_chars=DEFAULT_MAX_CHARS):
    if type(offset) is not int or offset < 0:
        raise ControlledError("offset must be a non-negative integer.")
    if type(max_chars) is not int or not 1 <= max_chars <= MAX_READ_CHARS:
        raise ControlledError("max-chars must be between 1 and 12000.")
    source, content_text = _load_cache(cache_file)
    if offset > len(content_text):
        raise ControlledError("offset exceeds content length.")
    next_offset = min(len(content_text), offset + max_chars)
    return {
        "source": source,
        "offset": offset,
        "next_offset": next_offset,
        "end_reached": next_offset >= len(content_text),
        "character_count": len(content_text),
        "content_text": content_text[offset:next_offset],
    }


def build_parser():
    parser = JsonArgumentParser(prog="read_wechat", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_parser = commands.add_parser("fetch")
    fetch_parser.add_argument("url")
    fetch_parser.add_argument("--output", required=True)
    read_parser = commands.add_parser("read")
    read_parser.add_argument("cache_file")
    read_parser.add_argument("--offset", type=int, default=0)
    read_parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        if args.command == "fetch":
            _emit(fetch(args.url, args.output))
        else:
            _emit(read(args.cache_file, offset=args.offset, max_chars=args.max_chars))
        return 0
    except ControlledError as error:
        _emit(_safe_error(str(error)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
