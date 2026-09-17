import contextlib
import datetime as dt
import email.message
import http.client
import importlib
import io
import json
import os
import sys
import traceback
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock


MODULE_NAME = "tikhub_http"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_BASE_URL = "https://api.tikhub.io"
TEST_API_KEY = "test-api-key-123"


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_exact_size_success_body(total_size):
    template = {"code": 200, "data": ""}
    overhead = len(json_bytes(template))
    payload = {"code": 200, "data": "x" * (total_size - overhead)}
    body = json_bytes(payload)
    if len(body) != total_size:
        raise AssertionError("failed to build exact-size response body")
    return payload, body


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.status = status
        self.code = status
        self.headers = email.message.Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self._stream = io.BytesIO(self._body)
        self.read_sizes = []
        self.closed = False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self._stream.read(size)

    def getcode(self):
        return self.status

    def close(self):
        self.closed = True
        self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class BrokenReadResponse(FakeResponse):
    def read(self, size=-1):
        raise RuntimeError("read exploded")


class TrackingBytesIO(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class FakeHTTPError(urllib.error.HTTPError):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def make_http_error(url, code, body=b"{}", headers=None, msg="error"):
    payload = body if isinstance(body, bytes) else body.encode("utf-8")
    fp = TrackingBytesIO(payload)
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    err = FakeHTTPError(url, code, msg, message, fp)
    return err, fp


class ScriptedOpener:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    @property
    def open_count(self):
        return len(self.calls)

    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        if not self._script:
            raise AssertionError("unexpected open call")
        result = self._script.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class TikHubClientTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self._original_open = urllib.request.OpenerDirector.open
        self._network = mock.patch.object(
            urllib.request.OpenerDirector,
            "open",
            side_effect=AssertionError("unexpected network access"),
        )
        self._network.start()
        self.addCleanup(self._network.stop)
        module_dir = os.path.dirname(os.path.abspath(__file__))
        if module_dir not in sys.path:
            sys.path.insert(0, module_dir)

    def load_module(self):
        if MODULE_NAME in sys.modules:
            return importlib.reload(sys.modules[MODULE_NAME])
        return importlib.import_module(MODULE_NAME)

    def request_url(self, request):
        return getattr(request, "full_url", request.get_full_url())

    def request_headers(self, request):
        return {key.lower(): value for key, value in request.header_items()}

    def assert_no_secret_leak(self, exc, *secrets):
        payload = getattr(exc, "payload", {})
        haystacks = [
            str(exc),
            repr(exc),
            json.dumps(payload, sort_keys=True, ensure_ascii=False),
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        ]
        for secret in secrets:
            if not secret:
                continue
            for haystack in haystacks:
                self.assertNotIn(secret, haystack)

    def test_env_defaults_are_used(self):
        for env_base in (None, "https://api.tikhub.dev"):
            with self.subTest(env_base=env_base):
                os.environ["TIKHUB_API_KEY"] = TEST_API_KEY
                if env_base is not None:
                    os.environ["TIKHUB_API_BASE"] = env_base
                opener = ScriptedOpener([FakeResponse(json_bytes({"ok": True}))])
                module = self.load_module()
                client = module.TikHubClient(opener=opener)
                result = client.request("GET", "/api/v1/ping")
                self.assertEqual(result, {"ok": True})
                request, timeout = opener.calls[0]
                self.assertEqual(timeout, 45)
                expected_base = env_base or DEFAULT_BASE_URL
                self.assertEqual(
                    self.request_url(request),
                    expected_base.rstrip("/") + "/api/v1/ping",
                )

    def test_missing_api_key_raises_before_network(self):
        opener = ScriptedOpener([])
        module = self.load_module()
        try:
            client = module.TikHubClient(opener=opener)
        except module.TikHubError as exc:
            self.assertEqual(opener.open_count, 0)
            self.assertEqual(getattr(exc, "exit_code", None), 1)
            self.assertEqual(getattr(exc, "payload", {}).get("attempts"), 0)
            return
        with self.assertRaises(module.TikHubError) as ctx:
            client.request("GET", "/api/v1/ping")
        self.assertEqual(opener.open_count, 0)
        self.assertEqual(ctx.exception.exit_code, 1)
        self.assertEqual(ctx.exception.payload.get("attempts"), 0)

    def test_constructor_validation_for_timeout_and_retries(self):
        module = self.load_module()
        timeout_cases = [None, True, False, "30", 29, 61, 29.9, 60.1, float("nan"), float("inf")]
        for timeout in timeout_cases:
            with self.subTest(timeout=timeout):
                with self.assertRaises(module.TikHubError):
                    module.TikHubClient(api_key=TEST_API_KEY, timeout=timeout)
        retries_cases = [True, False, -1, 3, 1.5, "1"]
        for retries in retries_cases:
            with self.subTest(retries=retries):
                with self.assertRaises(module.TikHubError):
                    module.TikHubClient(api_key=TEST_API_KEY, retries=retries)
        opener = ScriptedOpener([FakeResponse(json_bytes({"ok": True}))])
        client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener)
        client.request("GET", "/api/v1/ping")
        self.assertEqual(opener.calls[0][1], 45)

    def test_base_url_validation_accepts_only_official_https_hosts(self):
        module = self.load_module()
        valid_bases = [
            "https://api.tikhub.io",
            "https://api.tikhub.io/",
            "https://api.tikhub.dev",
            "https://api.tikhub.dev/",
        ]
        for base_url in valid_bases:
            with self.subTest(base_url=base_url):
                opener = ScriptedOpener([FakeResponse(json_bytes({"ok": True}))])
                client = module.TikHubClient(
                    api_key=TEST_API_KEY,
                    base_url=base_url,
                    opener=opener,
                )
                client.request("GET", "/api/v1/ping")
                request = opener.calls[0][0]
                self.assertEqual(
                    self.request_url(request),
                    base_url.rstrip("/") + "/api/v1/ping",
                )
        invalid_bases = [
            "http://api.tikhub.io",
            "https://evil.example.com",
            "https://user:pass@api.tikhub.io",
            "https://api.tikhub.io:443",
            "https://api.tikhub.io?x=1",
            "https://api.tikhub.io#frag",
            "https://api.tikhub.io/api",
            "https://api.tikhub.io/custom",
            "https://api.tikhub.io.evil.example",
            "https://api.tikhub.io@evil.example",
            "https://api.tikhub.io./",
            "https://api.tikhub.io?",
            "https://api.tikhub.io#",
            "https://api.tikhub.io/\n",
            "",
        ]
        for base_url in invalid_bases:
            with self.subTest(base_url=base_url):
                opener = ScriptedOpener([])
                try:
                    client = module.TikHubClient(
                        api_key=TEST_API_KEY,
                        base_url=base_url,
                        opener=opener,
                    )
                except module.TikHubError:
                    self.assertEqual(opener.open_count, 0)
                    continue
                with self.assertRaises(module.TikHubError):
                    client.request("GET", "/api/v1/ping")
                self.assertEqual(opener.open_count, 0)

    def test_path_validation_rejects_unsafe_inputs(self):
        module = self.load_module()
        invalid_paths = [
            "https://api.tikhub.io/api/v1/ping",
            "/api/v1/../ping",
            "/api/v1/%2e%2e/ping",
            "/api/v1/%2E%2E/ping",
            "/api/v1/%252e%252e/ping",
            "/api/v1/.%2e/ping",
            "/api/v1/./ping",
            "//evil.example/api/v1/ping",
            "/api/v1/ping%2f..%2fpong",
            "/api/v1/ping\x00",
            "/api/v1//ping",
            "/api/v1/ping?x=1",
            "/api/v1/ping#frag",
            "/api/v1\\ping",
            "/api/v1/ping\r",
            "/api/v1/ping\n",
            "/v1/ping",
        ]
        for path in invalid_paths:
            with self.subTest(path=path):
                opener = ScriptedOpener([])
                client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener)
                with self.assertRaises(module.TikHubError):
                    client.request("GET", path)
                self.assertEqual(opener.open_count, 0)

    def test_request_encodes_query_and_body_and_returns_json_unchanged(self):
        module = self.load_module()
        payload = {
            "code": "200",
            "emoji": "雪",
            "data": {"status": "404", "items": [0, False, ""]},
        }
        opener = ScriptedOpener([FakeResponse(json_bytes(payload))])
        client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = client.request(
                "POST",
                "/api/v1/items",
                params={
                    "none": None,
                    "zero": 0,
                    "false": False,
                    "empty": "",
                    "unicode": "雪",
                    "url": "https://example.com/?a=1&b=2",
                },
                body={},
            )
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(result, payload)
        request, timeout = opener.calls[0]
        self.assertEqual(timeout, 45)
        self.assertEqual(request.get_method(), "POST")
        headers = self.request_headers(request)
        self.assertEqual(headers["authorization"], "Bearer " + TEST_API_KEY)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["accept"], "application/json")
        self.assertNotIn(TEST_API_KEY, self.request_url(request))
        parsed = urllib.parse.urlsplit(self.request_url(request))
        self.assertEqual(parsed.path, "/api/v1/items")
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        self.assertNotIn("none", query)
        self.assertEqual(query["zero"], ["0"])
        self.assertEqual(query["false"], ["False"])
        self.assertEqual(query["empty"], [""])
        self.assertEqual(query["unicode"], ["雪"])
        self.assertEqual(query["url"], ["https://example.com/?a=1&b=2"])
        self.assertEqual(request.data, b"{}")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {})

    def test_none_body_sends_no_data_and_callable_opener_is_supported(self):
        module = self.load_module()
        seen = {}

        def opener(request, timeout=None):
            seen["request"] = request
            seen["timeout"] = timeout
            return FakeResponse(json_bytes({"status": "ok"}))

        client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener)
        result = client.request("GET", "/api/v1/ping", body=None)
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(seen["timeout"], 45)
        self.assertIsNone(getattr(seen["request"], "data", None))

    def test_response_size_limit_applies_to_success_and_http_errors(self):
        module = self.load_module()
        payload, exact_body = build_exact_size_success_body(MAX_RESPONSE_BYTES)
        response = FakeResponse(exact_body)
        opener = ScriptedOpener([response])
        client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener)
        result = client.request("GET", "/api/v1/big")
        self.assertEqual(result, payload)
        self.assertEqual(response.read_sizes, [MAX_RESPONSE_BYTES + 1])

        oversized_body = b"{" + b"\"data\":\"" + (b"x" * MAX_RESPONSE_BYTES) + b"\"}"
        oversized_response = FakeResponse(oversized_body)
        opener = ScriptedOpener([oversized_response])
        client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener)
        with self.assertRaises(module.TikHubError) as ctx:
            client.request("GET", "/api/v1/big")
        self.assertIn("8 MiB", ctx.exception.payload["message"])
        self.assertEqual(ctx.exception.payload["attempts"], 1)
        self.assertEqual(oversized_response.read_sizes, [MAX_RESPONSE_BYTES + 1])
        self.assertTrue(oversized_response.closed)
        self.assertEqual(opener.open_count, 1)
        error, fp = make_http_error(
            "https://api.tikhub.io/api/v1/big?token=secret",
            400,
            oversized_body,
            headers={"X-Request-ID": "req-big"},
        )
        opener = ScriptedOpener([error])
        client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener, retries=0)
        with self.assertRaises(module.TikHubError):
            client.request("GET", "/api/v1/big", params={"token": "secret"})
        self.assertEqual(fp.read_sizes, [MAX_RESPONSE_BYTES + 1])
        self.assertTrue(fp.closed)

    def test_malformed_responses_raise_explicit_errors(self):
        module = self.load_module()
        cases = [
            ("invalid-utf8", b"\xff"),
            ("malformed-json", b"{"),
            ("non-object-array", b"[]"),
            ("non-object-null", b"null"),
            ("non-object-string", b'"string"'),
            ("non-object-number", b"200"),
            ("nonstandard-nan", b'{"data": NaN}'),
            ("nonstandard-infinity", b'{"data": Infinity}'),
            ("too-deep-json", b'{"data":' + b'[' * 2000 + b']' * 2000 + b'}'),
            ("invalid-top-level-code", json_bytes({"code": "abc", "message": "bad"})),
            ("boolean-code", json_bytes({"code": True})),
            ("float-code", json_bytes({"code": 200.0})),
            ("null-code", json_bytes({"code": None})),
        ]
        for name, body in cases:
            with self.subTest(case=name):
                opener = ScriptedOpener([FakeResponse(body)])
                client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener, retries=0)
                with self.assertRaises(module.TikHubError) as ctx:
                    client.request("GET", "/api/v1/ping")
                self.assertEqual(opener.open_count, 1)
                self.assertEqual(ctx.exception.payload["attempts"], 1)
                self.assertEqual(ctx.exception.payload["code"], 0)
                self.assertIs(ctx.exception.payload["error"], True)

    def test_runtime_errors_from_opener_and_response_are_not_swallowed(self):
        module = self.load_module()
        opener = ScriptedOpener([RuntimeError("opener exploded")])
        client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener, retries=2)
        with self.assertRaisesRegex(RuntimeError, "opener exploded"):
            client.request("GET", "/api/v1/ping")
        self.assertEqual(opener.open_count, 1)

        opener = ScriptedOpener([BrokenReadResponse(b"{}")])
        client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener, retries=2)
        with self.assertRaisesRegex(RuntimeError, "read exploded"):
            client.request("GET", "/api/v1/ping")
        self.assertEqual(opener.open_count, 1)

    def test_retries_transient_failures_and_business_codes(self):
        module = self.load_module()
        transient_http, _ = make_http_error(
            "https://api.tikhub.io/api/v1/retry",
            500,
            json_bytes({"message": "server said secret"}),
        )
        timeout_error = TimeoutError("timed out")
        wrapped_timeout = urllib.error.URLError(TimeoutError("wrapped timeout"))
        malformed_transient, _ = make_http_error(
            "https://api.tikhub.io/api/v1/retry",
            502,
            b"<html>temporarily bad</html>",
        )
        cases = [
            ("http-500", [transient_http], 1, [1]),
            ("timeout-chain", [timeout_error, wrapped_timeout], 2, [1, 2]),
            (
                "business-503",
                [FakeResponse(json_bytes({"code": "503", "message": "busy"}))],
                1,
                [1],
            ),
            ("malformed-transient-body", [malformed_transient], 1, [1]),
        ]
        for name, script, retries, expected_sleep in cases:
            with self.subTest(case=name):
                sleeps = []
                script = script + [FakeResponse(json_bytes({"code": 200, "ok": True}))]
                opener = ScriptedOpener(script)
                client = module.TikHubClient(
                    api_key=TEST_API_KEY,
                    opener=opener,
                    retries=retries,
                    sleep=sleeps.append,
                )
                result = client.request("GET", "/api/v1/retry")
                self.assertEqual(result, {"code": 200, "ok": True})
                self.assertEqual(sleeps, expected_sleep)
                self.assertEqual(opener.open_count, len(script))

    def test_retry_after_parsing_and_cap(self):
        module = self.load_module()
        numeric_retry, _ = make_http_error(
            "https://api.tikhub.io/api/v1/retry-after",
            429,
            json_bytes({"message": "slow down"}),
            headers={"Retry-After": "4"},
        )
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=20)).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
        capped_retry, _ = make_http_error(
            "https://api.tikhub.io/api/v1/retry-after",
            503,
            b"<html>busy</html>",
            headers={"Retry-After": future},
        )

        sleeps = []
        opener = ScriptedOpener([numeric_retry, FakeResponse(json_bytes({"ok": True}))])
        client = module.TikHubClient(
            api_key=TEST_API_KEY,
            opener=opener,
            retries=1,
            sleep=sleeps.append,
        )
        self.assertEqual(client.request("GET", "/api/v1/retry-after"), {"ok": True})
        self.assertEqual(opener.open_count, 2)
        self.assertEqual(sleeps, [4])

        sleeps = []
        opener = ScriptedOpener([capped_retry, FakeResponse(json_bytes({"ok": True}))])
        client = module.TikHubClient(
            api_key=TEST_API_KEY,
            opener=opener,
            retries=1,
            sleep=sleeps.append,
        )
        with self.assertRaises(module.TikHubError) as ctx:
            client.request("GET", "/api/v1/retry-after")
        self.assertEqual(opener.open_count, 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(ctx.exception.payload.get("attempts"), 1)

    def test_non_retryable_failures_do_not_retry(self):
        module = self.load_module()
        cases = [
            ("http-400", [make_http_error("https://api.tikhub.io/api/v1/fail", 400)[0]], 400),
            ("http-404", [make_http_error("https://api.tikhub.io/api/v1/fail", 404)[0]], 404),
            (
                "business-422",
                [FakeResponse(json_bytes({"code": "422", "message": "nope"}))],
                422,
            ),
            ("url-error", [urllib.error.URLError(OSError("no route"))], None),
        ]
        for name, script, expected_code in cases:
            with self.subTest(case=name):
                sleeps = []
                opener = ScriptedOpener(script)
                client = module.TikHubClient(
                    api_key=TEST_API_KEY,
                    opener=opener,
                    retries=2,
                    sleep=sleeps.append,
                )
                with self.assertRaises(module.TikHubError) as ctx:
                    client.request("GET", "/api/v1/fail")
                self.assertEqual(opener.open_count, 1)
                self.assertEqual(sleeps, [])
                if expected_code is not None:
                    self.assertEqual(ctx.exception.payload.get("code"), expected_code)

    def test_tikhub_error_payload_is_safe_and_normalized(self):
        module = self.load_module()
        secret_key = "realistic-secret-api-key"
        bearer = "Bearer very-secret-token"
        error, _ = make_http_error(
            "https://api.tikhub.io/api/v1/secret?token=leak",
            401,
            json_bytes(
                {
                    "message": "remote said " + bearer,
                    "details": secret_key,
                    "request_id": "unsafe request id",
                }
            ),
            headers={
                "X-Request-ID": "req_123-ABC",
                "Location": "https://evil.example.com/next?token=leak",
                "Authorization": bearer,
            },
        )
        opener = ScriptedOpener([error])
        client = module.TikHubClient(api_key=secret_key, opener=opener, retries=0)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(module.TikHubError) as ctx:
                client.request("GET", "/api/v1/secret", params={"token": "leak"})
        exc = ctx.exception
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(exc.exit_code, 1)
        self.assertEqual(exc.payload.get("code"), 401)
        self.assertIs(exc.payload.get("error"), True)
        self.assertEqual(exc.payload.get("endpoint"), "/api/v1/secret")
        self.assertEqual(exc.payload.get("attempts"), 1)
        self.assertEqual(exc.payload.get("request_id"), "req_123-ABC")
        self.assertIsInstance(exc.payload.get("message"), str)
        self.assert_no_secret_leak(
            exc,
            secret_key,
            bearer,
            "unsafe request id",
            "?token=leak",
            "evil.example.com",
        )

    def test_unsafe_request_id_is_omitted_from_payload(self):
        module = self.load_module()
        opener = ScriptedOpener(
            [
                FakeResponse(
                    json_bytes(
                        {
                            "code": "404",
                            "message": "missing",
                            "request_id": "Bearer hidden token",
                        }
                    )
                )
            ]
        )
        client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener, retries=2)
        with self.assertRaises(module.TikHubError) as ctx:
            client.request("GET", "/api/v1/missing")
        self.assertEqual(ctx.exception.payload.get("code"), 404)
        self.assertNotIn("request_id", ctx.exception.payload)

    def test_default_opener_disables_redirects(self):
        os.environ["TIKHUB_API_KEY"] = TEST_API_KEY
        original_build_opener = urllib.request.build_opener
        captured = {}

        class DummyOpener:
            def open(self, request, timeout=None):
                return FakeResponse(json_bytes({"code": 200, "ok": True}))

        def spy_build_opener(*handlers):
            captured["actual"] = original_build_opener(*handlers)
            return DummyOpener()

        with mock.patch("urllib.request.build_opener", side_effect=spy_build_opener):
            module = self.load_module()
            client = module.TikHubClient()
            self.assertEqual(client.request("GET", "/api/v1/ping"), {"code": 200, "ok": True})
        redirect_handler = next(
            (
                handler
                for handler in captured["actual"].handlers
                if isinstance(handler, urllib.request.HTTPRedirectHandler)
            ),
            None,
        )
        self.assertIsNotNone(redirect_handler)
        request = urllib.request.Request("https://api.tikhub.io/api/v1/ping")
        same_host = redirect_handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {"Location": "https://api.tikhub.io/api/v1/next"},
            "https://api.tikhub.io/api/v1/next",
        )
        offsite = redirect_handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {"Location": "https://evil.example.com/out"},
            "https://evil.example.com/out",
        )
        self.assertIsNone(same_host)
        self.assertIsNone(offsite)

    def test_redirect_http_error_does_not_leak_headers_or_body(self):
        module = self.load_module()
        secret = "Bearer redirected-secret"
        error, _ = make_http_error(
            "https://api.tikhub.io/api/v1/redirect",
            302,
            json_bytes({"message": secret}),
            headers={
                "Location": "https://evil.example.com/final?secret=1",
                "X-Request-ID": "req-redirect",
            },
        )
        opener = ScriptedOpener([error])
        client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener, retries=2)
        with self.assertRaises(module.TikHubError) as ctx:
            client.request("GET", "/api/v1/redirect")
        self.assertEqual(opener.open_count, 1)
        self.assertEqual(ctx.exception.payload.get("attempts"), 1)
        self.assert_no_secret_leak(ctx.exception, secret, "evil.example.com", "secret=1")

    def test_all_client_errors_and_envelopes_are_non_retryable(self):
        module = self.load_module()
        for code in (400, 401, 402, 403, 404, 422):
            for envelope in (False, True):
                with self.subTest(code=code, envelope=envelope):
                    data = {"code": str(code), "request_id": "req-business"}
                    response = FakeResponse(json_bytes(data)) if envelope else make_http_error(
                        DEFAULT_BASE_URL + "/api/v1/fail", code, json_bytes(data)
                    )[0]
                    opener = ScriptedOpener([response])
                    sleeps = []
                    client = module.TikHubClient(
                        api_key=TEST_API_KEY, opener=opener, retries=2, sleep=sleeps.append
                    )
                    with self.assertRaises(module.TikHubError) as ctx:
                        client.request("GET", "/api/v1/fail")
                    self.assertEqual(ctx.exception.payload["code"], code)
                    self.assertEqual(ctx.exception.payload["attempts"], 1)
                    self.assertEqual(ctx.exception.payload["request_id"], "req-business")
                    self.assertEqual(opener.open_count, 1)
                    self.assertEqual(sleeps, [])

    def test_all_transient_codes_retry_and_preserve_the_attempt_limit(self):
        module = self.load_module()
        for code in (429, 500, 502, 503, 504):
            for envelope in (False, True):
                for retries in (None, 0, 2):
                    with self.subTest(code=code, envelope=envelope, retries=retries):
                        count = 2 if retries is None else retries + 1
                        script = []
                        for attempt in range(1, count + 1):
                            body = json_bytes({"code": str(code), "request_id": f"req-{attempt}"})
                            script.append(
                                FakeResponse(body) if envelope else make_http_error(
                                    DEFAULT_BASE_URL + "/api/v1/retry", code, body
                                )[0]
                            )
                        opener, sleeps = ScriptedOpener(script), []
                        options = {} if retries is None else {"retries": retries}
                        client = module.TikHubClient(
                            api_key=TEST_API_KEY, opener=opener, sleep=sleeps.append, **options
                        )
                        with self.assertRaises(module.TikHubError) as ctx:
                            client.request("GET", "/api/v1/retry")
                        self.assertEqual(ctx.exception.payload["code"], code)
                        self.assertEqual(ctx.exception.payload["attempts"], count)
                        self.assertEqual(ctx.exception.payload["request_id"], f"req-{count}")
                        self.assertEqual(opener.open_count, count)
                        self.assertEqual(sleeps, [1, 2][:count - 1])

    def test_timeout_exhaustion_is_safe_and_non_timeout_network_errors_do_not_retry(self):
        module = self.load_module()
        secret = "network-secret-value"
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                errors = [TimeoutError("Bearer " + secret) for _ in range(3)]
                if wrapped:
                    errors = [urllib.error.URLError(error) for error in errors]
                opener, sleeps = ScriptedOpener(errors), []
                client = module.TikHubClient(
                    api_key=TEST_API_KEY, opener=opener, retries=2, sleep=sleeps.append
                )
                with self.assertRaises(module.TikHubError) as ctx:
                    client.request("GET", "/api/v1/timeout")
                self.assertEqual(ctx.exception.payload["code"], 0)
                self.assertEqual(ctx.exception.payload["attempts"], 3)
                self.assertEqual(opener.open_count, 3)
                self.assertEqual(sleeps, [1, 2])
                self.assert_no_secret_leak(ctx.exception, secret)
        for error in (
            ConnectionResetError(secret),
            urllib.error.URLError("Bearer " + secret),
            http.client.IncompleteRead(secret.encode("ascii")),
        ):
            with self.subTest(error_type=type(error).__name__):
                opener = ScriptedOpener([error])
                client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener, retries=2)
                with self.assertRaises(module.TikHubError) as ctx:
                    client.request("GET", "/api/v1/fail")
                self.assertEqual(opener.open_count, 1)
                self.assert_no_secret_leak(ctx.exception, secret)

    def test_retry_after_boundaries_and_dates_without_real_sleep(self):
        module = self.load_module()
        from email.utils import formatdate

        now = 1700000000
        cases = [
            ("0", 0), ("5", 5), ("5.1", None), ("60", None), ("inf", None),
            ("not-a-date", 1), ("nan", 1), ("-2", 1),
            (formatdate(now + 3, usegmt=True), 3),
            (formatdate(now + 6, usegmt=True), None),
            (formatdate(now - 30, usegmt=True), 0),
        ]
        for value, delay in cases:
            with self.subTest(value=value):
                response = FakeResponse(
                    json_bytes({"code": 429}), headers={"retry-after": value}
                )
                opener = ScriptedOpener([response, FakeResponse(b'{"code":200}')])
                sleeps = []
                client = module.TikHubClient(
                    api_key=TEST_API_KEY, opener=opener, sleep=sleeps.append
                )
                with mock.patch.object(module.time, "time", return_value=now):
                    if delay is None:
                        with self.assertRaises(module.TikHubError) as ctx:
                            client.request("GET", "/api/v1/retry")
                        self.assertEqual(ctx.exception.payload["attempts"], 1)
                        self.assertEqual(sleeps, [])
                        self.assertEqual(opener.open_count, 1)
                    else:
                        self.assertEqual(client.request("GET", "/api/v1/retry"), {"code": 200})
                        self.assertEqual(sleeps, [delay])
                        self.assertEqual(opener.open_count, 2)

    def test_success_dictionary_identity_and_nested_codes_remain_untouched(self):
        module = self.load_module()
        for code in (200, "200"):
            with self.subTest(code=code):
                payload = {
                    "code": code,
                    "data": {"code": 403, "status_code": 1, "error": True, "message": "平台错误"},
                    "message": "成功",
                    "request_id": "req-success",
                    "extra": [None, False],
                }
                opener = ScriptedOpener([FakeResponse(b"{}")])
                client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener)
                with mock.patch.object(module.json, "loads", return_value=payload):
                    result = client.request("GET", "/api/v1/passthrough")
                self.assertIs(result, payload)
                self.assertEqual(result["code"], code)
                self.assertEqual(result["data"]["code"], 403)
        opener = ScriptedOpener([FakeResponse(json_bytes({"data": {"code": 500}}))])
        client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener)
        self.assertEqual(client.request("GET", "/api/v1/passthrough"), {"data": {"code": 500}})

    def test_unicode_request_body_and_timeout_boundaries(self):
        module = self.load_module()
        for timeout in (30, 45.5, 60):
            with self.subTest(timeout=timeout):
                opener = ScriptedOpener([FakeResponse(b"{}")])
                client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener, timeout=timeout)
                client.request("post", "/api/v1/search", body={"keyword": "雪🌏"})
                request, actual_timeout = opener.calls[0]
                self.assertEqual(actual_timeout, timeout)
                self.assertEqual(request.get_method(), "POST")
                self.assertIn("雪🌏".encode("utf-8"), request.data)
                self.assertEqual(json.loads(request.data.decode("utf-8")), {"keyword": "雪🌏"})

    def test_import_and_construction_are_lazy_and_explicit_configuration_wins(self):
        with mock.patch.object(urllib.request, "build_opener") as build:
            module = self.load_module()
            module.TikHubClient()
            build.assert_not_called()
        with mock.patch.dict(os.environ, {
            "TIKHUB_API_KEY": "env-fake-key",
            "TIKHUB_API_BASE": "https://not-official.example",
        }):
            opener = ScriptedOpener([FakeResponse(b"{}")])
            client = module.TikHubClient(
                api_key=TEST_API_KEY, base_url=DEFAULT_BASE_URL, opener=opener
            )
            client.request("GET", "/api/v1/ping")
            self.assertEqual(
                self.request_headers(opener.calls[0][0])["authorization"], "Bearer " + TEST_API_KEY
            )
            with self.assertRaises(module.TikHubError):
                module.TikHubClient(api_key=TEST_API_KEY, opener=ScriptedOpener([]))

    def test_invalid_credentials_and_request_serialization_never_open_connections(self):
        module = self.load_module()
        for key in ("", "   ", "fake\r\nInjected: value", "fake\x00key", "非ASCII", 123):
            with self.subTest(key_type=type(key).__name__):
                opener = ScriptedOpener([])
                client = module.TikHubClient(api_key=key, opener=opener)
                with self.assertRaises(module.TikHubError) as ctx:
                    client.request("GET", "/api/v1/ping")
                self.assertEqual(ctx.exception.payload["attempts"], 0)
                self.assertEqual(opener.open_count, 0)
        circular = {}
        circular["self"] = circular
        for options in (
            {"body": {"secret": object()}}, {"body": circular}, {"body": {"x": float("nan")}},
            {"params": ["not", "a", "dict"]}, {"params": {"x": "\ud800"}},
        ):
            with self.subTest(options_type=next(iter(options))):
                opener = ScriptedOpener([])
                client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener)
                with self.assertRaises(module.TikHubError) as ctx:
                    client.request("POST", "/api/v1/ping", **options)
                self.assertEqual(ctx.exception.payload["attempts"], 0)
                self.assertEqual(opener.open_count, 0)

    def test_http_protocol_parser_errors_are_safe_and_do_not_retry(self):
        module = self.load_module()
        secret = "protocol-secret-value"
        wires = (
            ("HTTP/9." + secret + " 200 OK\r\n\r\n").encode("ascii"),
            b"HTTP/1.1 200 OK\r\n" + b"X-Header: value\r\n" * 101 + b"\r\n",
            b"HTTP/1.1 200 OK\r\nX-Header: " + b"x" * 66000 + b"\r\n\r\n",
        )
        for wire in wires:
            with self.subTest(wire_size=len(wire)):
                stream = io.BytesIO(wire)
                fake_socket = mock.Mock()
                fake_socket.makefile.return_value = stream
                calls = []

                def opener(request, timeout):
                    calls.append(request)
                    response = http.client.HTTPResponse(fake_socket)
                    try:
                        response.begin()
                        return response
                    except http.client.HTTPException:
                        response.close()
                        raise

                client = module.TikHubClient(api_key=TEST_API_KEY, opener=opener, retries=2)
                with self.assertRaises(module.TikHubError) as ctx:
                    client.request("GET", "/api/v1/parser")
                self.assertEqual(ctx.exception.payload["attempts"], 1)
                self.assertEqual(len(calls), 1)
                self.assert_no_secret_leak(ctx.exception, secret)

    def test_credential_reflections_and_query_secrets_never_enter_errors(self):
        module = self.load_module()
        secret = "fake-private-credential"
        for request_id in (secret, "req-" + secret, "Authorization-secret", "Bearer-secret"):
            with self.subTest(request_id=request_id):
                body = {
                    "code": 401, "request_id": request_id,
                    "message": secret + " Bearer remote-token Authorization: another-token",
                }
                opener = ScriptedOpener([FakeResponse(json_bytes(body))])
                client = module.TikHubClient(api_key=secret, opener=opener)
                with self.assertRaises(module.TikHubError) as ctx:
                    client.request("GET", "/api/v1/" + secret, params={"token": "query-secret"})
                self.assertNotIn("request_id", ctx.exception.payload)
                self.assert_no_secret_leak(
                    ctx.exception, secret, "remote-token", "another-token", "query-secret"
                )

    def test_all_redirect_statuses_stop_immediately_even_on_same_host(self):
        module = self.load_module()
        for status in (300, 301, 302, 303, 304, 307, 308):
            for target in ("https://api.tikhub.io/api/v1/next", "https://evil.example/out"):
                with self.subTest(status=status, target=target):
                    response = FakeResponse(b"", status=status, headers={"Location": target})
                    opener, sleeps = ScriptedOpener([response]), []
                    client = module.TikHubClient(
                        api_key=TEST_API_KEY, opener=opener, retries=2, sleep=sleeps.append
                    )
                    with self.assertRaises(module.TikHubError) as ctx:
                        client.request("GET", "/api/v1/redirect")
                    self.assertEqual(ctx.exception.payload["code"], status)
                    self.assertIn("redirect", ctx.exception.payload["message"])
                    self.assertEqual(opener.open_count, 1)
                    self.assertEqual(sleeps, [])
                    self.assertTrue(response.closed)
                    self.assert_no_secret_leak(ctx.exception, target, TEST_API_KEY)

    def test_default_opener_refuses_redirect_locations_without_parsing_or_following(self):
        module = self.load_module()
        secret = "redirect-location-secret"
        for status in (301, 302, 303, 307, 308):
            for location in (
                "https://api.tikhub.io/api/v1/next",
                "https://evil.example/" + secret,
                "https://[" + secret,
                "file:" + secret,
            ):
                with self.subTest(status=status, location=location):
                    calls, responses = [], []

                    class MemoryHTTPSHandler(urllib.request.HTTPSHandler):
                        def https_open(self, request):
                            calls.append(request)
                            response = urllib.response.addinfourl(
                                io.BytesIO(b"ignored redirect body"),
                                email.message.Message(), request.full_url, status,
                            )
                            response.msg = "Found"
                            response.headers["Location"] = location
                            responses.append(response)
                            return response

                    build_opener = urllib.request.build_opener
                    def build(*handlers):
                        director = build_opener(MemoryHTTPSHandler(), *handlers)
                        director.open = self._original_open.__get__(director)
                        return director

                    with mock.patch.object(urllib.request, "build_opener", side_effect=build):
                        client = module.TikHubClient(api_key=TEST_API_KEY, retries=2)
                        with self.assertRaises(module.TikHubError) as ctx:
                            client.request("GET", "/api/v1/redirect")
                    self.assertEqual(ctx.exception.payload["code"], status)
                    self.assertEqual(ctx.exception.payload["attempts"], 1)
                    self.assertEqual(len(calls), 1)
                    self.assertTrue(responses[0].closed)
                    self.assert_no_secret_leak(ctx.exception, secret, TEST_API_KEY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
