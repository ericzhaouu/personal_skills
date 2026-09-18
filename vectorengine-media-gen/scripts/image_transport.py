import base64
import binascii
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings
from datetime import datetime, timezone

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 8192
MAX_JSON_DEPTH = 64
MAX_IMAGE_PIXELS = 64 * 1024 * 1024
MAX_REDIRECTS = 3
REDIRECT_CODES = {301, 302, 303, 307, 308}
LOCAL_SUFFIXES = (".internal", ".lan", ".local", ".localdomain", ".localhost", ".home")
REQUEST_ID_KEYS = ("request_id", "requestId", "req_id")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REQUEST_ID_SUFFIX_RE = re.compile(r"\(request id:\s*([A-Za-z0-9][A-Za-z0-9._:-]{0,127})\)\s*$", re.IGNORECASE)
SUPPORTED_IMAGE_FORMATS = {
    "PNG": {"format": "png", "extension": "png"},
    "JPEG": {"format": "jpeg", "extension": "jpg"},
    "WEBP": {"format": "webp", "extension": "webp"},
}
NETWORK_EXCEPTIONS = (
    urllib.error.URLError,
    socket.timeout,
    TimeoutError,
    ConnectionAbortedError,
    ConnectionRefusedError,
    ConnectionResetError,
    BrokenPipeError,
    http.client.HTTPException,
    ssl.SSLError,
    OSError,
)


class GenerationError(Exception):
    def __init__(self, message, *, category="invalid_response", status=None, request_id=None, can_fallback=False):
        super().__init__("Image generation failed.")
        self.message = message
        self.category = category
        self.status = status
        self.request_id = request_id
        self.can_fallback = can_fallback

    def __str__(self):
        return "Image generation failed."


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def _deny(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_302 = http_error_303 = http_error_307 = http_error_308 = _deny


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, pinned_addresses, **kwargs):
        self._pinned_addresses = tuple(pinned_addresses)
        super().__init__(host, **kwargs)

    def connect(self):
        last_error = None
        for family, sockaddr in self._pinned_addresses:
            raw_socket = None
            try:
                raw_socket = socket.socket(family, socket.SOCK_STREAM)
                if self.timeout is not None:
                    raw_socket.settimeout(self.timeout)
                if self.source_address:
                    raw_socket.bind(self.source_address)
                raw_socket.connect(sockaddr)
                self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
                return
            except OSError as error:
                last_error = error
                if raw_socket is not None:
                    raw_socket.close()
        if last_error is None:
            raise OSError("pinned connection failed")
        raise last_error


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned_hosts):
        super().__init__()
        self._pinned_hosts = pinned_hosts

    def https_open(self, req):
        return self.do_open(self._make_connection, req)

    def _make_connection(self, host, **kwargs):
        normalized_host = _connection_host_name(host)
        pinned_addresses = self._pinned_hosts.get(normalized_host)
        if not pinned_addresses:
            _invalid("host resolution failed")
        return _PinnedHTTPSConnection(host, pinned_addresses=pinned_addresses, **kwargs)


def _invalid(message, *, status=None, request_id=None, can_fallback=False):
    raise GenerationError(message, category="invalid_response", status=status, request_id=request_id, can_fallback=can_fallback)


def _connection_host_name(host):
    if not isinstance(host, str) or not host:
        _invalid("missing host")
    if host.startswith("["):
        end = host.find("]")
        if end <= 0:
            _invalid("invalid authority")
        return host[1:end].lower()
    if host.count(":") == 1:
        name, port = host.rsplit(":", 1)
        if port.isdigit():
            return name.lower()
    return host.lower()


def _format_url_host(host):
    return f"[{host}]" if ":" in host else host


def _normalize_allowed_api_hosts(allowed_api_hosts):
    normalized = {(item or "").rstrip(".").lower() for item in allowed_api_hosts if item}
    if not normalized:
        raise ValueError("allowed_api_hosts must be non-empty")
    return normalized


def _validate_token(token):
    if not isinstance(token, str):
        raise ValueError("token must be a string")
    if not token or token.strip() != token:
        raise ValueError("token must be non-empty")
    if any(ord(char) < 0x20 or ord(char) > 0x7E for char in token):
        raise ValueError("token must be printable ASCII")
    if "\r" in token or "\n" in token:
        raise ValueError("token must not contain CRLF")
    return token


def _is_public_ip(value):
    if not isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        try:
            value = ipaddress.ip_address(value)
        except ValueError:
            return False
    if value.version == 6 and value.ipv4_mapped is not None:
        value = value.ipv4_mapped
    return bool(value.is_global)


def _build_literal_pin(address):
    host = address.compressed.lower()
    if address.version == 6:
        return host, ((socket.AF_INET6, (host, 443, 0, 0)),)
    return host, ((socket.AF_INET, (host, 443)),)


def _resolve_public_host(host, *, require_dns_name, pin_cache):
    host = (host or "").rstrip(".").lower()
    if not host or "%" in host:
        _invalid("invalid authority")
    try:
        parsed_ip = ipaddress.ip_address(host)
    except ValueError:
        parsed_ip = None
    if parsed_ip is not None:
        if require_dns_name or not _is_public_ip(parsed_ip):
            _invalid("non-public host")
        normalized, pinned_addresses = _build_literal_pin(parsed_ip)
        pin_cache[normalized] = pinned_addresses
        return normalized
    if "." not in host or host == "localhost" or host == "metadata" or host.startswith("metadata."):
        _invalid("public dns host required")
    if any(host.endswith(suffix) for suffix in LOCAL_SUFFIXES):
        _invalid("local host not allowed")
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        _invalid("host resolution failed")
    if not infos:
        _invalid("host resolution failed")
    pinned_addresses = []
    seen = set()
    for family, _, _, _, sockaddr in infos:
        address_text = sockaddr[0].split("%", 1)[0]
        if not _is_public_ip(address_text):
            _invalid("non-public host")
        if family == socket.AF_INET6:
            pinned_sockaddr = (address_text, 443, 0, 0)
        else:
            pinned_sockaddr = (address_text, 443)
        key = (family, pinned_sockaddr)
        if key not in seen:
            seen.add(key)
            pinned_addresses.append(key)
    if not pinned_addresses:
        _invalid("host resolution failed")
    pin_cache[host] = tuple(pinned_addresses)
    return host


def _validate_https_url(url, *, allow_query, allow_any_path, require_dns_name, pin_cache, allowed_hosts=None):
    if not isinstance(url, str) or not url:
        _invalid("invalid url")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        _invalid("invalid authority")
    if parsed.scheme != "https":
        _invalid("https required")
    if not parsed.netloc or parsed.username or parsed.password or port is not None:
        _invalid("invalid authority")
    if parsed.fragment:
        _invalid("fragments not allowed")
    if not allow_any_path and parsed.path not in ("", "/"):
        _invalid("unexpected path")
    if not allow_query and parsed.query:
        _invalid("unexpected query")
    candidate_host = (parsed.hostname or "").rstrip(".").lower()
    if allowed_hosts is not None and candidate_host not in allowed_hosts:
        _invalid("api host is not allowlisted")
    host = _resolve_public_host(candidate_host, require_dns_name=require_dns_name, pin_cache=pin_cache)
    normalized = urllib.parse.urlunsplit(
        ("https", _format_url_host(host), parsed.path or "", parsed.query if allow_query else "", "")
    )
    return parsed, normalized, host


def _close_response(response):
    close = getattr(response, "close", None)
    if callable(close):
        close()
        return
    fp = getattr(response, "fp", None)
    if fp is not None and hasattr(fp, "close"):
        fp.close()


def _read_limited(stream, limit):
    headers = getattr(stream, "headers", None)
    if headers:
        raw_length = headers.get("Content-Length")
        if raw_length:
            try:
                content_length = int(raw_length)
            except ValueError:
                _invalid("invalid content length")
            if content_length > limit:
                _invalid("response too large")
    chunks = []
    total = 0
    while True:
        chunk = stream.read(min(65536, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            _invalid("response too large")


def _json_depth(root):
    stack = [(root, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            _invalid("json too deep")
        if isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    stack.append((nested, depth + 1))
        elif isinstance(value, list):
            for nested in value:
                if isinstance(nested, (dict, list)):
                    stack.append((nested, depth + 1))


def _parse_json_object(data):
    def reject_constant(value):
        raise ValueError("Nonstandard JSON constant")
    try:
        payload = json.loads(data.decode("utf-8"), parse_constant=reject_constant)
    except UnicodeDecodeError:
        _invalid("response is not utf-8 json")
    except (ValueError, RecursionError):
        _invalid("response is not valid json")
    if not isinstance(payload, dict):
        _invalid("json object required")
    _json_depth(payload)
    return payload


def _sanitize_request_id(value, *, token):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 128 or not REQUEST_ID_RE.fullmatch(value):
        return None
    if "sk-" in value.lower() or (token and token in value):
        return None
    return value


def extract_request_id(value, *, token=""):
    stack = [value]
    while stack:
        candidate = stack.pop()
        if isinstance(candidate, dict):
            for key in (*REQUEST_ID_KEYS, "task_id", "taskId"):
                result = _sanitize_request_id(candidate.get(key), token=token)
                if result:
                    return result
            message = candidate.get("message")
            if isinstance(message, str):
                match = REQUEST_ID_SUFFIX_RE.search(message[-200:])
                if match:
                    result = _sanitize_request_id(match.group(1), token=token)
                    if result:
                        return result
            stack.extend(item for item in candidate.values() if isinstance(item, (dict, list)))
        elif isinstance(candidate, list):
            stack.extend(item for item in candidate if isinstance(item, (dict, list)))
    return None


def _request_id_from_error(error, *, token):
    if getattr(error, "fp", None) is None:
        return None
    try:
        body = error.fp.read(MAX_ERROR_BODY_BYTES)
    except OSError:
        return None
    if not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    return extract_request_id(payload, token=token)


def _response_code(response):
    code = getattr(response, "status", None)
    if code is None and hasattr(response, "getcode"):
        code = response.getcode()
    if code is not None and not isinstance(code, int):
        _invalid("invalid http status")
    return code


def _validate_dimensions(width, height):
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        _invalid("invalid image dimensions")


def _load_pillow():
    try:
        from PIL import Image, ImageFile, UnidentifiedImageError
    except ImportError:
        raise GenerationError("Pillow is required for image validation.", category="dependency", can_fallback=False)
    return Image, ImageFile, UnidentifiedImageError


def _inspect_open_image(image, supported_formats):
    format_name = (image.format or "").upper()
    metadata = supported_formats.get(format_name)
    if metadata is None:
        _invalid("unsupported image format")
    if getattr(image, "n_frames", 1) != 1 or getattr(image, "is_animated", False):
        _invalid("animated images not supported")
    width, height = image.size
    _validate_dimensions(width, height)
    return {
        "format": metadata["format"],
        "extension": metadata["extension"],
        "width": width,
        "height": height,
    }


def _validate_image_with_pillow(data):
    Image, ImageFile, UnidentifiedImageError = _load_pillow()
    previous_max_pixels = Image.MAX_IMAGE_PIXELS
    previous_truncated = ImageFile.LOAD_TRUNCATED_IMAGES
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                info = _inspect_open_image(image, SUPPORTED_IMAGE_FORMATS)
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                _inspect_open_image(image, SUPPORTED_IMAGE_FORMATS)
                image.load()
        return info
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        _invalid("image exceeds pixel limit")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        _invalid("invalid image data")
    finally:
        Image.MAX_IMAGE_PIXELS = previous_max_pixels
        ImageFile.LOAD_TRUNCATED_IMAGES = previous_truncated


def _encode_json_payload(payload):
    try:
        return json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise GenerationError("request payload must be valid JSON.", category="invalid_request", can_fallback=False)


def decode_base64_image(value):
    if not isinstance(value, str) or not value:
        _invalid("image base64 must be a non-empty string")
    if len(value) > ((MAX_RESPONSE_BYTES + 2) // 3) * 4 + 8:
        _invalid("encoded image too large")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        _invalid("invalid base64 image")
    if len(data) > MAX_RESPONSE_BYTES:
        _invalid("decoded image too large")
    return data


def inspect_image(data):
    if not isinstance(data, (bytes, bytearray, memoryview)):
        _invalid("image bytes required")
    data = bytes(data)
    if not data:
        _invalid("empty image")
    if len(data) > MAX_RESPONSE_BYTES:
        _invalid("decoded image too large")
    return _validate_image_with_pillow(data)


def _make_output_name(extension):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}_{uuid.uuid4().hex}.{extension}"


def save_images(images, output_dir):
    validated = []
    for image in images:
        raw_image = bytes(image)
        if not raw_image:
            _invalid("empty image")
        validated.append((raw_image, inspect_image(raw_image)["extension"]))
    if not validated:
        _invalid("at least one image required")

    created_directory = False
    if os.path.isdir(output_dir):
        pass
    else:
        os.makedirs(output_dir, exist_ok=False)
        created_directory = True

    created_paths = []
    try:
        for image, extension in validated:
            path = os.path.join(output_dir, _make_output_name(extension))
            with open(path, "xb") as handle:
                created_paths.append(path)
                handle.write(image)
                handle.flush()
                os.fsync(handle.fileno())
        return created_paths
    except OSError as write_error:
        cleanup_errors = []
        for path in created_paths:
            try:
                os.remove(path)
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if created_directory:
            try:
                os.rmdir(output_dir)
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            cleanup_failure = OSError("failed to clean up partial image files")
            cleanup_failure.add_note(str(write_error))
            raise cleanup_failure from cleanup_errors[0]
        raise


class ImageTransport:
    def __init__(self, base_url, token, *, timeout=180, opener=None, allowed_api_hosts=()):
        if not isinstance(timeout, int) or timeout < 10 or timeout > 600:
            raise ValueError("timeout must be an integer between 10 and 600")
        self.token = _validate_token(token)
        self._pinned_hosts = {}
        self._allowed_api_hosts = _normalize_allowed_api_hosts(allowed_api_hosts)
        _, normalized, host = _validate_https_url(
            base_url,
            allow_query=False,
            allow_any_path=False,
            require_dns_name=True,
            pin_cache=self._pinned_hosts,
            allowed_hosts=self._allowed_api_hosts,
        )
        self.base_url = normalized
        self.api_host = host
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
            _PinnedHTTPSHandler(self._pinned_hosts),
        )

    def _open(self, request):
        if hasattr(self.opener, "open"):
            return self.opener.open(request, timeout=self.timeout)
        return self.opener(request, timeout=self.timeout)

    def request_json(self, path, payload):
        encoded_payload = _encode_json_payload(payload)
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            _invalid("path must be relative")
        api_path = parsed.path or "/"
        if not api_path.startswith("/"):
            api_path = f"/{api_path}"
        url = urllib.parse.urlunsplit(("https", self.api_host, api_path, parsed.query, ""))
        request = urllib.request.Request(
            url,
            data=encoded_payload,
            method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with self._open(request) as response:
                code = _response_code(response)
                response_url = getattr(response, "geturl", lambda: url)()
                if response_url != url or code in REDIRECT_CODES:
                    _invalid("api redirect not allowed", status=code)
                if code is None or code < 200 or code >= 300:
                    raise GenerationError(
                        "api request failed",
                        category="invalid_response",
                        status=code,
                        can_fallback=code in (429, 503),
                    )
                return _parse_json_object(_read_limited(response, MAX_RESPONSE_BYTES))
        except urllib.error.HTTPError as error:
            try:
                request_id = _request_id_from_error(error, token=self.token)
                if error.code in REDIRECT_CODES:
                    _invalid("api redirect not allowed", status=error.code, request_id=request_id)
                raise GenerationError(
                    "api request failed",
                    category="invalid_response",
                    status=error.code,
                    request_id=request_id,
                    can_fallback=error.code in (429, 503),
                )
            finally:
                _close_response(error)
        except GenerationError:
            raise
        except NETWORK_EXCEPTIONS:
            raise GenerationError("api request outcome unknown", category="unknown_outcome", can_fallback=False)

    def get_public_image(self, url):
        current = url
        redirects = 0
        while True:
            _, normalized, _ = _validate_https_url(
                current,
                allow_query=True,
                allow_any_path=True,
                require_dns_name=False,
                pin_cache=self._pinned_hosts,
            )
            request = urllib.request.Request(normalized, method="GET")
            try:
                with self._open(request) as response:
                    code = _response_code(response)
                    response_url = getattr(response, "geturl", lambda: normalized)()
                    if response_url != normalized:
                        _invalid("automatic redirects are not allowed")
                    if code in REDIRECT_CODES:
                        location = response.headers.get("Location")
                        if not location:
                            _invalid("redirect location missing", status=code)
                        if redirects >= MAX_REDIRECTS:
                            _invalid("too many redirects", status=code)
                        current = urllib.parse.urljoin(normalized, location)
                        redirects += 1
                        continue
                    if code is None or code < 200 or code >= 300:
                        raise GenerationError("image download failed", category="invalid_response", status=code, can_fallback=False)
                    return _read_limited(response, MAX_RESPONSE_BYTES)
            except urllib.error.HTTPError as error:
                try:
                    if error.code in REDIRECT_CODES:
                        location = error.headers.get("Location")
                        if not location:
                            _invalid("redirect location missing", status=error.code)
                        if redirects >= MAX_REDIRECTS:
                            _invalid("too many redirects", status=error.code)
                        current = urllib.parse.urljoin(normalized, location)
                        redirects += 1
                        continue
                    raise GenerationError(
                        "image download failed",
                        category="invalid_response",
                        status=error.code,
                        request_id=_request_id_from_error(error, token=self.token),
                        can_fallback=False,
                    )
                finally:
                    _close_response(error)
            except GenerationError:
                raise
            except NETWORK_EXCEPTIONS:
                raise GenerationError("image download failed", category="invalid_response", can_fallback=False)
