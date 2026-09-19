"""Shared, credential-free primitives for the video transcript pipeline."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time


class VideoError(Exception):
    def __init__(self, message, *, code="operation_failed", details=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


def reject_symlink_chain(path):
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise VideoError("Working paths must not traverse symlinks.", code="unsafe_path")


def private_directory(path):
    path = Path(path)
    reject_symlink_chain(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise VideoError("Working directory is unavailable.", code="unsafe_path")
    if os.name == "posix":
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise VideoError("Working directory must be owned by this user with mode 0700.", code="unsafe_path")
    return path.resolve()


def atomic_json(path, value):
    path = Path(path)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if path.is_symlink():
        raise VideoError("Refusing to replace a symlink.", code="unsafe_path")
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError):
        Path(temporary).unlink(missing_ok=True)
        raise


def read_json(path, *, limit=16 * 1024 * 1024):
    path = Path(path)
    if path.is_symlink():
        raise VideoError("Refusing a symlink state file.", code="unsafe_path")
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise VideoError("State file exceeds the size limit.", code="invalid_state")
        return json.loads(raw)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        raise VideoError("State file is missing or invalid.", code="invalid_state") from None


def owned_file(job_dir, path):
    job_dir = Path(job_dir).resolve()
    path = Path(path)
    if path.is_symlink():
        raise VideoError("Artifact must not be a symlink.", code="unsafe_path")
    resolved = path.resolve()
    if not resolved.is_relative_to(job_dir) or not resolved.is_file():
        raise VideoError("Artifact is outside this job or missing.", code="unsafe_path")
    return resolved


def run_external(argv, *, cwd, timeout, max_output=8 * 1024 * 1024):
    """Capture bounded output without logging commands, cookies or signed URLs."""
    if not argv or not 1 <= timeout <= 3600:
        raise VideoError("Invalid subprocess request.", code="invalid_input")
    try:
        process = subprocess.Popen(
            [str(value) for value in argv], cwd=str(cwd), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name == "posix")
        with process:
            outputs = [bytearray(), bytearray()]
            exceeded = threading.Event()
            reader_errors = []
            def drain(stream, slot, limit):
                try:
                    while True:
                        block = stream.read(65536)
                        if not block:
                            break
                        remaining = max(0, limit + 1 - len(outputs[slot]))
                        outputs[slot].extend(block[:remaining])
                        if len(outputs[slot]) > limit:
                            exceeded.set()
                except OSError:
                    reader_errors.append(slot)
                    exceeded.set()
                finally:
                    stream.close()
            readers = [
                threading.Thread(target=drain, args=(process.stdout, 0, max_output), daemon=True),
                threading.Thread(target=drain, args=(process.stderr, 1, 1024 * 1024), daemon=True),
            ]
            for reader in readers:
                reader.start()
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if exceeded.is_set():
                        raise subprocess.TimeoutExpired(argv, timeout)
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    time.sleep(0.02)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                else:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                    process.wait()
                for reader in readers:
                    reader.join(timeout=5)
                raise VideoError("External command exceeded its time or output limit; no automatic retry.",
                                 code="output_limit" if exceeded.is_set() else "command_timeout") from None
            for reader in readers:
                reader.join(timeout=5)
            if exceeded.is_set() or reader_errors or any(reader.is_alive() for reader in readers):
                raise VideoError("External output exceeded its limit or could not be read.", code="output_limit")
            if process.returncode:
                diagnostic = bytes(outputs[1][:16384]).decode("utf-8", errors="replace").lower()
                details = {"exit_code": process.returncode}
                code = "command_failed"
                message = "External command failed."
                option = re.search(r"no such option:\s*(--[a-z0-9-]+)", diagnostic)
                if option:
                    code = "unsupported_option"
                    message = "Installed tool does not support a requested option."
                    details["option"] = option.group(1)
                elif any(word in diagnostic for word in ("sign in to confirm", "login required", "login-required", "cookies are no longer valid")):
                    code = "access_required"
                    message = "Source requires valid authorized access; no login or Cookie retry was attempted."
                elif "http error 429" in diagnostic:
                    code = "rate_limited"
                    message = "Source rate limit reached; no retry was attempted."
                elif "http error 403" in diagnostic:
                    code = "access_denied"
                    message = "Source access was denied; this does not by itself prove a Cookie expired."
                elif "video unavailable" in diagnostic or "this video is not available" in diagnostic:
                    code = "source_unavailable"
                    message = "The source reports this video unavailable."
                raise VideoError(message, code=code, details=details)
            raw = bytes(outputs[0])
            if len(raw) > max_output:
                raise VideoError("External output exceeds the limit.", code="output_limit")
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                raise VideoError("External output is not valid UTF-8.", code="invalid_output") from None
    except FileNotFoundError:
        raise VideoError("Required executable is unavailable.", code="dependency") from None
    except OSError:
        raise VideoError("Unable to execute an external command.", code="command_failed") from None
