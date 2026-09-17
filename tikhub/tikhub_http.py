"""Isolated stdlib TikHub transport; platform-specific status checks belong to the CLI."""

import http.client
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone
from email.utils import parsedate_to_datetime


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 64
_TRANSIENT_CODES = {429, 500, 502, 503, 504}
_NETWORK_ERRORS = (
    urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError,
    http.client.HTTPException,
)
_AUTH_TEXT = re.compile(
    r"(?:bearer|authorization|api[_-]?key|access[_-]?token)[^\s]*", re.IGNORECASE
)


class TikHubError(Exception):
    """Safe CLI-facing failure; local/transport failures use code 0."""

    exit_code = 1

    def __init__(self, code, message, endpoint="", attempts=0, request_id=None):
        self.payload = {
            "code": code, "error": True, "message": message,
            "endpoint": endpoint, "attempts": attempts,
        }
        if request_id is not None:
            self.payload["request_id"] = request_id
        super().__init__(message)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_302(self, req, fp, code, msg, headers):
        # Refuse before urllib parses the untrusted Location header.
        raise urllib.error.HTTPError(req.full_url, code, "HTTP redirect refused.", headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _invalid_constant(value):
    raise ValueError("Nonstandard JSON constant")


def _numeric_code(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value):
        return int(value)
    return None


def _bounded_json_depth(value):
    stack = [(iter((value,)), 0)]
    while stack:
        iterator, depth = stack[-1]
        try:
            item = next(iterator)
        except StopIteration:
            stack.pop()
            continue
        if isinstance(item, (dict, list)):
            if depth >= MAX_JSON_DEPTH:
                return False
            stack.append((iter(item.values() if isinstance(item, dict) else item), depth + 1))
    return True


def _retry_delay(headers, attempt):
    fallback = min(2 ** (attempt - 1), 5)
    value = headers.get("Retry-After")
    if value is None:
        return fallback
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = max(0, date.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return fallback
    if delay != delay or delay < 0:
        return fallback
    return None if delay > 5 else delay


class TikHubClient:
    """Build lazily in CLI commands. An injected opener is a trusted test transport.

    No credentials are checked or connections opened until request(). Configuration
    is validated at construction. Successful response dictionaries are not rewritten.
    """

    def __init__(self, api_key=None, base_url=None, timeout=45, retries=1,
                 opener=None, sleep=None):
        self._api_key = os.environ.get("TIKHUB_API_KEY", "") if api_key is None else api_key
        base_url = os.environ.get("TIKHUB_API_BASE", "https://api.tikhub.io") \
            if base_url is None else base_url
        if not isinstance(base_url, str) or not re.fullmatch(
            r"https://api\.tikhub\.(?:io|dev)/?", base_url, re.IGNORECASE
        ):
            raise self._error(0, "Only official TikHub HTTPS base URLs are allowed.")
        if type(timeout) not in (int, float) or not 30 <= timeout <= 60:
            raise self._error(0, "Timeout must be a number between 30 and 60 seconds.")
        if type(retries) is not int or not 0 <= retries <= 2:
            raise self._error(0, "Retries must be an integer between 0 and 2.")
        self._base_url = base_url.rstrip("/").lower()
        self._timeout = timeout
        self._retries = retries
        self._opener = opener
        self._sleep = time.sleep if sleep is None else sleep

    def _safe_text(self, value):
        if isinstance(self._api_key, str) and self._api_key:
            value = value.replace(self._api_key, "[REDACTED]")
        return _AUTH_TEXT.sub("[REDACTED]", value)[:180]

    def _error(self, code, message, endpoint="", attempts=0, request_id=None):
        return TikHubError(code, self._safe_text(message), self._safe_text(endpoint),
                           attempts, request_id)

    def _request_id(self, data, headers):
        candidates = (
            data.get("request_id") if isinstance(data, dict) else None,
            headers.get("X-Request-ID"),
        )
        for value in candidates:
            if (isinstance(value, str)
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value)
                    and not _AUTH_TEXT.search(value)
                    and not (isinstance(self._api_key, str)
                             and self._api_key and self._api_key in value)):
                return value
        return None

    def request(self, method, path, params=None, body=None):
        """Return the original JSON object, or raise TikHubError without diagnostics.

        Retries apply only to transient HTTP/envelope codes or network timeouts.
        Error endpoints exclude query parameters; untrusted server messages are
        intentionally discarded rather than attempting to redact arbitrary bodies.
        """
        if (not isinstance(path, str)
                or not re.fullmatch(r"/api/v1/[A-Za-z0-9._/-]*", path)
                or "//" in path
                or any(part in (".", "..") for part in path.split("/"))):
            raise self._error(0, "Endpoint must be a safe /api/v1/ path.")
        if not isinstance(self._api_key, str) or not self._api_key.strip():
            raise self._error(0, "TIKHUB_API_KEY is required.", path)
        if not re.fullmatch(r"[\x21-\x7e]+", self._api_key):
            raise self._error(0, "API credential has an invalid format.", path)
        if not isinstance(method, str) or method.upper() not in {
            "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
        }:
            raise self._error(0, "Unsupported HTTP method.", path)
        if params is not None and not isinstance(params, dict):
            raise self._error(0, "Query parameters must be a dictionary.", path)
        try:
            query = urllib.parse.urlencode(
                {key: value for key, value in (params or {}).items() if value is not None}
            )
            data = None if body is None else json.dumps(
                body, ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
            raise self._error(0, "Request parameters or JSON body are invalid.", path) from None
        request = urllib.request.Request(
            self._base_url + path + ("?" + query if query else ""),
            data=data, method=method.upper(), headers={
                "Authorization": "Bearer " + self._api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "TikHub-CLI/2.0",
            },
        )
        if self._opener is None:
            self._opener = urllib.request.build_opener(_NoRedirect())
        open_request = self._opener.open if hasattr(self._opener, "open") else self._opener

        for attempt in range(1, self._retries + 2):
            headers = {}
            try:
                try:
                    response = open_request(request, timeout=self._timeout)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    status = response.getcode()
                    headers = response.headers
                    if 300 <= status < 400:
                        raise self._error(status, "HTTP redirect refused.", path, attempt,
                                          self._request_id(None, headers))
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
            except _NETWORK_ERRORS as exc:
                timed_out = isinstance(exc, TimeoutError) or (
                    isinstance(exc, urllib.error.URLError)
                    and isinstance(exc.reason, TimeoutError)
                )
                failure = self._error(
                    0, "Request timed out." if timed_out else "Network request failed.",
                    path, attempt,
                )
                retryable = timed_out
            else:
                request_id = self._request_id(None, headers)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise self._error(0, "Response exceeds the 8 MiB limit.",
                                      path, attempt, request_id) from None
                payload = None
                parse_error = None
                try:
                    payload = json.loads(raw.decode("utf-8"), parse_constant=_invalid_constant)
                except (UnicodeDecodeError, ValueError, RecursionError):
                    parse_error = "Response is not valid UTF-8 JSON."
                if parse_error is None and not isinstance(payload, dict):
                    parse_error = "Response JSON must be an object."
                if parse_error is None and not _bounded_json_depth(payload):
                    parse_error = "Response exceeds the JSON nesting limit."
                request_id = self._request_id(payload, headers)
                if not 200 <= status < 300:
                    code = status
                    message = "HTTP request failed (code {}).".format(code)
                    if parse_error:
                        message += " " + parse_error
                elif parse_error:
                    raise self._error(0, parse_error, path, attempt, request_id) from None
                elif "code" not in payload:
                    return payload
                else:
                    code = _numeric_code(payload["code"])
                    if code == 200:
                        return payload
                    if code is None:
                        raise self._error(0, "Invalid API response code.",
                                          path, attempt, request_id) from None
                    message = "TikHub API error (code {}).".format(code)
                failure = self._error(code, message, path, attempt, request_id)
                retryable = code in _TRANSIENT_CODES
            if not retryable or attempt > self._retries:
                raise failure from None
            delay = _retry_delay(headers, attempt)
            if delay is None:
                raise failure from None
            self._sleep(delay)
