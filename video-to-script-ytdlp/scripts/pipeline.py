#!/usr/bin/env python3
"""Subtitle-first video source extraction with private, recoverable jobs."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid
import wave

import video_acquire
import video_asr
from video_common import VideoError, atomic_json, owned_file, private_directory, read_json, reject_symlink_chain, run_external

SCHEMA = 2
DEFAULT_OUTPUT = Path.home() / ".openclaw/files/video-scripts"
MEDIA_LIMIT = 250 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def positive(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Expected a positive integer.") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("Expected a positive integer.")
    return number


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("url", nargs="?")
    run.add_argument("legacy_cookie", nargs="?")
    run.add_argument("legacy_language", nargs="?")
    run.add_argument("--cookies", type=Path)
    run.add_argument("--target-language")
    run.add_argument("--source-language")
    run.add_argument("--format", action="store_true")
    run.add_argument("--allow-asr", action="store_true",
                     help="Allow charged audio transcription if no usable subtitles are available")
    run.add_argument("--max-duration", type=positive, default=None, help="Seconds; default 1800")
    run.add_argument("--max-download-mb", type=positive, default=None, help="Default 250 MiB")
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--ytdlp-bin")
    run.add_argument("--resume", type=Path, help="Job directory from an earlier receipt")
    run.add_argument("--retry-uncertain", action="store_true",
                     help="Explicitly resubmit uncertain ASR chunks; may incur another charge")
    transcribe = commands.add_parser("transcribe")
    transcribe.add_argument("audio", nargs="?", type=Path)
    transcribe.add_argument("legacy_language", nargs="?")
    transcribe.add_argument("--source-language")
    transcribe.add_argument("--target-language")
    transcribe.add_argument("--format", action="store_true")
    transcribe.add_argument("--allow-asr", action="store_true")
    transcribe.add_argument("--max-duration", type=positive, default=None)
    transcribe.add_argument("--output-dir", type=Path)
    transcribe.add_argument("--resume", type=Path)
    transcribe.add_argument("--retry-uncertain", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("job", type=Path)
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("job", type=Path)
    cleanup.add_argument("--confirm", action="store_true", required=True)
    commands.add_parser("doctor")
    download = commands.add_parser("download")
    download.add_argument("url")
    download.add_argument("cookie", nargs="?")
    download.add_argument("--output-dir", type=Path)
    download.add_argument("--max-duration", type=positive, default=1800)
    extract = commands.add_parser("extract")
    extract.add_argument("audio", type=Path)
    extract.add_argument("--output-dir", type=Path)
    return root


def valid_language(value):
    if value in (None, ""):
        return None
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value):
        raise VideoError("Language must be a language code, not a filename or instruction.", code="invalid_input")
    return value


def output_directory(path):
    path = Path(path).expanduser()
    reject_symlink_chain(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or (os.name == "posix" and path.stat().st_uid != os.getuid()):
        raise VideoError("Output directory is not owned by this user.", code="unsafe_path")
    return path.resolve()


def load_job(path):
    if not Path(path).is_dir():
        raise VideoError("Job directory does not exist.", code="invalid_state")
    job = private_directory(path)
    state = read_json(job / "job.json")
    if not isinstance(state, dict) or state.get("schema") != SCHEMA or state.get("job_id") != job.name:
        raise VideoError("This is not a recognized transcript job.", code="invalid_state")
    return job, state


@contextmanager
def lock_job(job):
    path = job / "run.lock"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise VideoError("Job is already active, or an interrupted lock needs inspection.",
                         code="job_locked") from None
    with os.fdopen(descriptor, "w") as handle:
        json.dump({"pid": os.getpid(), "created_at": now()}, handle)
    try:
        yield
    finally:
        path.unlink()


def options(args):
    cookie = getattr(args, "cookies", None)
    legacy = getattr(args, "legacy_cookie", None)
    if cookie and legacy:
        raise VideoError("Use either --cookies or the legacy cookie argument.", code="invalid_input")
    if not cookie and legacy:
        cookie = Path(legacy)
    target = args.target_language or args.legacy_language or None
    return {
        "source_language": valid_language(args.source_language),
        "target_language": valid_language(target),
        "format": args.format,
        "max_duration": args.max_duration or 1800,
        "max_download_bytes": (getattr(args, "max_download_mb", None) or 250) * 1024 * 1024,
        "cookie_file": str(cookie.expanduser().absolute()) if cookie else None,
    }


def new_job(args):
    settings = options(args)
    output = output_directory(args.output_dir or DEFAULT_OUTPUT)
    job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:12]
    job = private_directory(output / "jobs" / job_id)
    kind = "local_audio" if args.command == "transcribe" else "video_url"
    source = str(args.audio.expanduser().absolute()) if kind == "local_audio" else args.url
    state = {
        "schema": SCHEMA, "job_id": job_id, "created_at": now(), "updated_at": now(),
        "kind": kind, "source": source, "output_dir": str(output), "options": settings,
        "status": "created", "stage": "created", "artifacts": {}, "warnings": [],
        "translation": {"target": settings["target_language"],
                        "status": "handoff_required" if settings["target_language"] else "not_requested"},
    }
    atomic_json(job / "job.json", state)
    return job, state


def checkpoint(job, state):
    state["updated_at"] = now()
    atomic_json(job / "job.json", state)


def check_duration(value, maximum):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise VideoError("Duration is unknown; refusing unbounded media processing.", code="duration_unknown")
    if value > maximum:
        raise VideoError("Media exceeds the approved duration limit.", code="duration_limit",
                         details={"duration_seconds": value, "limit_seconds": maximum})


def check_wave_duration(path, maximum):
    try:
        with wave.open(str(path), "rb") as source:
            duration = source.getnframes() / source.getframerate()
    except (OSError, wave.Error, ZeroDivisionError):
        raise VideoError("Normalized audio is invalid.", code="invalid_audio") from None
    check_duration(duration, maximum)
    return duration


def local_duration(path, job, maximum):
    probe = shutil.which("ffprobe")
    if not probe:
        raise VideoError("ffprobe is required for local audio preflight.", code="dependency")
    output = run_external([probe, "-v", "error", "-show_entries", "format=duration",
                           "-of", "json", str(path)], cwd=job, timeout=30)
    try:
        value = float(json.loads(output)["format"]["duration"])
    except (ValueError, TypeError, KeyError):
        raise VideoError("Unable to determine local audio duration.", code="duration_unknown") from None
    check_duration(value, maximum)
    return value


def safe_name(value):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(value)).strip(" .")
    return name[:45] or "untitled"


def timestamp(seconds):
    milliseconds = round(seconds * 1000)
    whole, fraction = divmod(milliseconds, 1000)
    result = "{:02d}:{:02d}:{:02d}".format(whole // 3600, (whole % 3600) // 60, whole % 60)
    return result + (".{:03d}".format(fraction) if fraction else "")


def format_text(text):
    formatted = re.sub(r"([。！？!?；;])(?=\S)", r"\1\n", text)
    formatted = re.sub(r"(?<=[.!?]) +(?=[A-Z])", "\n", formatted)
    if re.sub(r"\s", "", formatted) != re.sub(r"\s", "", text):
        raise VideoError("Formatting would change transcript content.", code="integrity")
    return formatted


def publish(job, state, transcript):
    text = transcript.get("text")
    segments = transcript.get("segments")
    if not isinstance(text, str) or not text.strip() or not isinstance(segments, list):
        raise VideoError("Extraction produced no usable transcript.", code="empty_transcript")
    existing = state["artifacts"].get("result")
    if existing:
        result = Path(existing["path"])
        if not result.is_file() or fingerprint(result) != existing["sha256"]:
            raise VideoError("Published output changed or disappeared.", code="integrity")
        return result
    raw = job / "transcript.raw.json"
    if raw.exists():
        if read_json(raw) != transcript:
            raise VideoError("Raw transcript changed within this job.", code="integrity")
    else:
        atomic_json(raw, transcript)
    state["artifacts"]["raw_transcript"] = {"path": str(raw), "sha256": fingerprint(raw)}
    metadata = state.get("metadata", {})
    title = metadata.get("title") or Path(state["source"]).name
    identity = metadata.get("id", "local")
    platform = metadata.get("extractor", "local_audio")
    filename = "{}_{}_{}_{}_{}.md".format(
        state["created_at"][:10], safe_name(platform), safe_name(title),
        safe_name(identity), state["job_id"].rsplit("_", 1)[-1])
    output_dir = output_directory(state["output_dir"])
    result = output_dir / filename
    lines = [
        "# " + str(title).replace("\n", " "), "",
        "- Source: " + state["source"],
        "- Source type: " + transcript["source_type"],
        "- Language: " + str(transcript.get("language") or "auto/unknown"),
        "- Duration: {} seconds".format(metadata.get("duration", "unknown")),
        "- Coverage: spoken audio or selected subtitle track; visual-only content is not included.",
        "- Raw transcript: " + str(raw),
        "- Job: " + str(job),
    ]
    lines.extend("- Note: " + warning for warning in transcript.get("warnings", []))
    if state["translation"]["target"]:
        lines.append("- Translation: NOT generated; hand off to the calling Agent for {}.".format(
            state["translation"]["target"]))
    lines.extend(["", "## Original transcript", ""])
    if segments:
        for segment in segments:
            lines.append("[{}–{}] {}".format(timestamp(segment["start"]), timestamp(segment["end"]), segment["text"]))
    else:
        lines.append(text)
    if state["options"]["format"]:
        formatted = job / "transcript.formatted.txt"
        content = format_text(text)
        if formatted.exists():
            if formatted.is_symlink() or formatted.read_text(encoding="utf-8") != content:
                raise VideoError("Formatted derivative changed.", code="integrity")
        else:
            with formatted.open("x", encoding="utf-8") as handle:
                handle.write(content)
        os.chmod(formatted, 0o600)
        state["artifacts"]["formatted"] = {"path": str(formatted), "sha256": fingerprint(formatted)}
        lines.extend(["", "Formatted derivative (whitespace only): " + str(formatted)])
    rendered = ("\n".join(lines) + "\n").encode("utf-8")
    intent = {"path": str(result), "sha256": hashlib.sha256(rendered).hexdigest()}
    prior_intent = state.get("publish_intent")
    if not result.exists():
        state["publish_intent"] = intent
        checkpoint(job, state)
    if result.exists():
        if result.is_symlink() or not result.is_file():
            raise VideoError("Unrecorded output exists with different content; refusing to overwrite.", code="integrity")
        existing_bytes = result.read_bytes()
        if existing_bytes != rendered:
            if prior_intent != intent or not rendered.startswith(existing_bytes):
                raise VideoError("Published output conflicts with this job.", code="integrity")
            descriptor, temporary = tempfile.mkstemp(prefix=".transcript-publish-", dir=output_dir)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(rendered)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, result)
            except OSError:
                Path(temporary).unlink(missing_ok=True)
                raise
    else:
        with os.fdopen(os.open(result, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
    state["artifacts"]["result"] = {"path": str(result), "sha256": fingerprint(result)}
    state["status"] = "partial" if state["translation"]["target"] else "complete"
    state["stage"] = "published"
    state["source_type"] = transcript["source_type"]
    checkpoint(job, state)
    return result


def remove_cookie_copy(job):
    for name in ("cookies.txt", "cookies.txt.sha256"):
        path = job / name
        if path.exists():
            owned_file(job, path).unlink()


def process_job(args):
    if args.resume:
        job, state = load_job(args.resume)
        has_source = args.url if args.command == "run" else args.audio
        if has_source:
            requested = args.url if args.command == "run" else str(args.audio.expanduser().absolute())
            if requested != state["source"]:
                raise VideoError("Resume source does not match the existing job.", code="invalid_input")
        expected_kind = "video_url" if args.command == "run" else "local_audio"
        if state["kind"] != expected_kind:
            raise VideoError("Resume command does not match the job type.", code="invalid_input")
        if args.source_language is not None and valid_language(args.source_language) != state["options"]["source_language"]:
            raise VideoError("Resume source language differs from the original job.", code="invalid_input")
        new_target = args.target_language or args.legacy_language
        if new_target and valid_language(new_target) != state["options"]["target_language"]:
            raise VideoError("Resume translation target differs from the original job.", code="invalid_input")
        if getattr(args, "max_download_mb", None):
            state["options"]["max_download_bytes"] = args.max_download_mb * 1024 * 1024
    else:
        if (args.command == "run" and not args.url) or (args.command == "transcribe" and not args.audio):
            raise VideoError("A source URL or local audio file is required.", code="invalid_input")
        job, state = new_job(args)
    with lock_job(job):
        try:
            raw_path = job / "transcript.raw.json"
            if raw_path.is_file():
                artifact = state["artifacts"].get("raw_transcript")
                if artifact and fingerprint(raw_path) != artifact["sha256"]:
                    raise VideoError("Raw transcript changed.", code="integrity")
                result = publish(job, state, read_json(raw_path))
                return job, state, result
            maximum = state["options"]["max_duration"]
            if args.max_duration:
                maximum = args.max_duration
                state["options"]["max_duration"] = maximum
            audio = None
            entry = state["artifacts"].get("normalized_audio")
            if entry:
                audio = owned_file(job, entry["path"])
                if fingerprint(audio) != entry["sha256"]:
                    raise VideoError("Normalized audio changed.", code="integrity")
                if state["kind"] == "local_audio" and Path(state["source"]).exists():
                    if fingerprint(state["source"]) != state.get("source_sha256"):
                        raise VideoError("Local source changed after this job began.", code="integrity")
            if state["kind"] == "video_url" and audio is None:
                cookie = state["options"]["cookie_file"]
                metadata = video_acquire.probe(state["source"], job, cookie_file=cookie, ytdlp=args.ytdlp_bin)
                state["metadata"] = {key: metadata.get(key) for key in
                                     ("id", "title", "uploader", "duration", "webpage_url", "extractor", "language")}
                state["stage"] = "metadata"
                checkpoint(job, state)
                if metadata.get("is_live"):
                    raise VideoError("Live streams are not supported.", code="live_stream")
                check_duration(metadata.get("duration"), maximum)
                track = video_acquire.select_caption(metadata, state["options"]["source_language"])
                if track:
                    transcript = video_acquire.fetch_caption(state["source"], metadata, track, job,
                                                             cookie_file=cookie, ytdlp=args.ytdlp_bin)
                    result = publish(job, state, transcript)
                    remove_cookie_copy(job)
                    return job, state, result
                state["stage"] = "no_usable_subtitles"
                checkpoint(job, state)
                if not args.allow_asr:
                    raise VideoError("No matching usable subtitle track; ASR requires --allow-asr.",
                                     code="asr_approval_required")
                media = video_acquire.download_audio(
                    state["source"], metadata, job, cookie_file=cookie, ytdlp=args.ytdlp_bin,
                    max_bytes=state["options"]["max_download_bytes"])
                audio = video_asr.normalize_audio(media, job)
            elif audio is None:
                if not args.allow_asr:
                    raise VideoError("Local audio transcription requires --allow-asr.", code="asr_approval_required")
                input_path = Path(state["source"])
                if not input_path.is_file() or input_path.is_symlink():
                    raise VideoError("Audio source is missing or unsafe.", code="invalid_input")
                if input_path.stat().st_size > state["options"]["max_download_bytes"]:
                    raise VideoError("Audio exceeds the size limit.", code="size_limit")
                local_duration(input_path, job, maximum)
                audio = video_asr.normalize_audio(input_path, job)
                state["source_sha256"] = fingerprint(input_path)
                state["metadata"] = {"title": input_path.stem, "id": fingerprint(input_path)[:16],
                                     "extractor": "local_audio"}
            duration = check_wave_duration(audio, maximum)
            state["metadata"]["duration"] = duration
            state["artifacts"]["normalized_audio"] = {"path": str(audio), "sha256": fingerprint(audio)}
            state["stage"] = "normalized"
            checkpoint(job, state)
            if not args.allow_asr:
                raise VideoError("Resuming audio transcription requires --allow-asr.", code="asr_approval_required")
            transcript = video_asr.transcribe_audio(
                audio, job, source_language=state["options"]["source_language"],
                retry_uncertain=args.retry_uncertain)
            result = publish(job, state, transcript)
            remove_cookie_copy(job)
            return job, state, result
        except VideoError as error:
            state["status"] = "blocked" if error.code in (
                "asr_approval_required", "duration_limit", "duration_unknown") else "failed"
            state["last_error"] = {"code": error.code, "message": error.message, **error.details}
            checkpoint(job, state)
            error.details["job_dir"] = str(job)
            raise
        finally:
            remove_cookie_copy(job)


def standalone(args):
    output = output_directory(args.output_dir or DEFAULT_OUTPUT)
    job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:12]
    job = private_directory(output / "jobs" / job_id)
    if args.command == "extract":
        source = args.audio.expanduser().absolute()
        if not source.is_file() or source.is_symlink():
            raise VideoError("Audio input is unavailable.", code="invalid_input")
        local_duration(source, job, 1800)
        audio = video_asr.normalize_audio(source, job)
        print("AUDIO_FILE=" + str(audio))
        return 0
    metadata = video_acquire.probe(args.url, job, cookie_file=args.cookie or None)
    check_duration(metadata.get("duration"), args.max_duration)
    if metadata.get("is_live"):
        raise VideoError("Live streams are not supported.", code="live_stream")
    media = video_acquire.download_audio(args.url, metadata, job, cookie_file=args.cookie or None,
                                         max_bytes=MEDIA_LIMIT)
    remove_cookie_copy(job)
    for key, value in (
        ("VIDEO_ID", metadata.get("id", "")), ("VIDEO_TITLE", metadata.get("title", "")),
        ("VIDEO_UPLOADER", metadata.get("uploader", "")), ("VIDEO_DURATION", metadata.get("duration", "")),
        ("PLATFORM", metadata.get("extractor", "")), ("FILE", str(media)),
    ):
        print("{}={}".format(key, str(value).replace("\r", " ").replace("\n", " ")))
    return 0


def doctor():
    tools = {name: shutil.which(name) for name in ("python3", "ffmpeg", "ffprobe", "yt-dlp")}
    historical = Path.home() / ".openclaw/workspace/venv/bin/yt-dlp"
    if not tools["yt-dlp"] and historical.is_file():
        tools["yt-dlp"] = str(historical)
    return {"tools": tools, "asr_credentials": "checked only when ASR is explicitly requested",
            "default_duration_limit_seconds": 1800, "translation": "calling Agent; no independent model credentials"}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            print(json.dumps(doctor()))
            return 0
        if args.command in ("status", "cleanup"):
            job, state = load_job(args.job)
            if args.command == "cleanup":
                with lock_job(job):
                    if state["status"] not in ("complete", "partial"):
                        raise VideoError("Only completed jobs can have media cleaned.", code="invalid_state")
                    for relative in ("media",):
                        path = job / relative
                        if path.exists():
                            if path.is_symlink():
                                raise VideoError("Refusing symlink cleanup.", code="unsafe_path")
                            shutil.rmtree(path)
                    asr_state = job / "asr/state.json"
                    if asr_state.is_file():
                        for chunk in read_json(asr_state)["chunks"]:
                            path = job / chunk["audio_path"]
                            if path.exists():
                                owned_file(job, path).unlink()
                    remove_cookie_copy(job)
                    state["media_cleaned"] = True
                    checkpoint(job, state)
            print(json.dumps({"status": state["status"], "stage": state["stage"], "job_dir": str(job),
                              "artifacts": state["artifacts"], "translation": state["translation"]}))
            return 0
        if args.command in ("download", "extract"):
            return standalone(args)
        job, state, result = process_job(args)
        print("RESULT=" + str(result))
        print("TRANSCRIPT=" + str(result))
        print(json.dumps({"status": state["status"], "result": str(result), "job_dir": str(job),
                          "source_type": state.get("source_type"), "translation": state["translation"]},
                         ensure_ascii=False))
        return 10 if state["status"] == "partial" else 0
    except VideoError as error:
        print(json.dumps({"error": True, "code": error.code, "message": error.message,
                          **error.details}, ensure_ascii=False))
        return 2 if error.code in ("asr_approval_required", "duration_limit", "duration_unknown") else 1
    except (OSError, ValueError, UnicodeError):
        print(json.dumps({"error": True, "code": "local_failure",
                          "message": "Local file or data handling failed; no automatic replay was attempted."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
