import base64
import builtins
import io
import json
import os
import socket
import ssl
import struct
import sys
import tempfile
import unittest
import urllib.error
import zlib
from unittest import mock


THIS_DIR = os.path.dirname(os.path.abspath(__file__))
UPDATED_DIR = os.path.dirname(THIS_DIR)
WORK_ROOT = os.path.dirname(UPDATED_DIR)
SCRIPTS_DIR = os.path.join(UPDATED_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
for pillow_path in (
    os.path.join(WORK_ROOT, "test-venv", "Lib", "site-packages"),
    "C:\\Users\\zhaojian\\.cache\\codex-runtimes\\codex-runtime-install-LF8Btn\\payload\\codex-primary-runtime\\dependencies\\python\\Lib\\site-packages",
):
    if pillow_path not in sys.path and os.path.isdir(os.path.join(pillow_path, "PIL")):
        sys.path.append(pillow_path)

import image_transport as mod


def json_bytes(value):
    return json.dumps(value).encode("utf-8")


def png_bytes(width=1, height=1):
    def chunk(name, payload):
        crc = zlib.crc32(name)
        crc = zlib.crc32(payload, crc) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + (b"\x00\x00\x00" * width)
    idat = zlib.compress(row * height)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def pillow_image_bytes(image_mode, size, image_format, **save_kwargs):
    from PIL import Image

    image = Image.new(image_mode, size)
    image.putdata(
        [
            tuple(((index * 40) + channel * 20) % 256 for channel in range(len(image.getbands())))
            for index in range(size[0] * size[1])
        ]
    )
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **save_kwargs)
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, body=b"", *, url, code=200, headers=None):
        self._body = io.BytesIO(body)
        self._closed = False
        self._url = url
        self.status = code
        self.headers = headers or {}

    def read(self, size=-1):
        return self._body.read(size)

    def geturl(self):
        return self._url

    def getcode(self):
        return self.status

    def close(self):
        self._closed = True
        self._body.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class FakeOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "authorization": request.get_header("Authorization"),
                "method": request.get_method(),
            }
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSocket:
    def __init__(self, family, socktype):
        self.family = family
        self.socktype = socktype
        self.connected = None
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def connect(self, sockaddr):
        self.connected = sockaddr

    def close(self):
        self.closed = True


class FakeTLSContext:
    def __init__(self):
        self.calls = []
        self.verify_mode = ssl.CERT_REQUIRED
        self.check_hostname = True

    def wrap_socket(self, raw_socket, server_hostname=None):
        self.calls.append({"sockaddr": raw_socket.connected, "server_hostname": server_hostname})
        return raw_socket


class ImageTransportTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.tmp_root = self.tempdir.name

    def tearDown(self):
        self.tempdir.cleanup()

    def _dns(self, mapping):
        def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            values = mapping[host]
            results = []
            for value in values:
                if ":" in value:
                    results.append((socket.AF_INET6, socket.SOCK_STREAM, proto, "", (value, port, 0, 0)))
                else:
                    results.append((socket.AF_INET, socket.SOCK_STREAM, proto, "", (value, port)))
            return results

        return fake_getaddrinfo

    def _transport(self, opener):
        with mock.patch.object(mod.socket, "getaddrinfo", side_effect=self._dns({"api.example.com": ["93.184.216.34"]})):
            return mod.ImageTransport(
                "https://api.example.com",
                "secret-token",
                opener=opener,
                allowed_api_hosts=("api.example.com",),
            )

    def test_request_json_success_invalid_payload_and_redirect_refusal(self):
        redirect_error = urllib.error.HTTPError(
            "https://api.example.com/v1/images",
            302,
            "Found",
            {"Location": "https://evil.example/image"},
            io.BytesIO(b'{"request_id":"req-302"}'),
        )
        success_body = b'{"data":[{"id":"ok"}]}'
        opener = FakeOpener([redirect_error, FakeResponse(success_body, url="https://api.example.com/v1/images")])
        transport = self._transport(opener)
        with self.assertRaises(mod.GenerationError) as payload_error:
            transport.request_json("/v1/images", {"value": float("nan")})
        with self.assertRaises(mod.GenerationError) as first_error:
            transport.request_json("/v1/images", {"prompt": "x"})
        result = transport.request_json("/v1/images", {"prompt": "x"})
        self.assertEqual(payload_error.exception.category, "invalid_request")
        self.assertEqual(first_error.exception.status, 302)
        self.assertEqual(first_error.exception.request_id, "req-302")
        self.assertFalse(first_error.exception.can_fallback)
        self.assertEqual(result, {"data": [{"id": "ok"}]})
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(opener.requests[0]["authorization"], "Bearer secret-token")

    def test_request_json_handles_http_network_and_depth_errors_without_token_leak(self):
        deep = {}
        cursor = deep
        for _ in range(65):
            next_value = {}
            cursor["x"] = next_value
            cursor = next_value
        opener = FakeOpener(
            [
                urllib.error.HTTPError(
                    "https://api.example.com/v1/images",
                    503,
                    "Busy",
                    {},
                    io.BytesIO(b'{"error":{"request_id":"req-503-secret-token"}}'),
                ),
                urllib.error.URLError("timeout"),
                FakeResponse(json_bytes(deep), url="https://api.example.com/v1/images"),
            ]
        )
        transport = self._transport(opener)
        with self.assertRaises(mod.GenerationError) as http_error:
            transport.request_json("/v1/images", {"prompt": "x"})
        with self.assertRaises(mod.GenerationError) as network_error:
            transport.request_json("/v1/images", {"prompt": "x"})
        with self.assertRaises(mod.GenerationError) as depth_error:
            transport.request_json("/v1/images", {"prompt": "x"})
        self.assertEqual(http_error.exception.status, 503)
        self.assertTrue(http_error.exception.can_fallback)
        self.assertIsNone(http_error.exception.request_id)
        self.assertEqual(network_error.exception.category, "unknown_outcome")
        self.assertFalse(network_error.exception.can_fallback)
        self.assertEqual(depth_error.exception.category, "invalid_response")

    def test_public_image_redirects_private_hosts_and_pinned_connection(self):
        image = png_bytes()
        opener = FakeOpener(
            [
                urllib.error.HTTPError(
                    "https://cdn.example.com/image?sig=1",
                    302,
                    "Found",
                    {"Location": "https://img.example.net/final?sig=2"},
                    io.BytesIO(b""),
                ),
                FakeResponse(image, url="https://img.example.net/final?sig=2"),
            ]
        )
        dns_map = {
            "api.example.com": ["93.184.216.34"],
            "cdn.example.com": ["93.184.216.34"],
            "img.example.net": ["2606:2800:220:1:248:1893:25c8:1946"],
        }
        with mock.patch.object(mod.socket, "getaddrinfo", side_effect=self._dns(dns_map)):
            transport = mod.ImageTransport(
                "https://api.example.com",
                "secret-token",
                opener=opener,
                allowed_api_hosts=("api.example.com",),
            )
            result = transport.get_public_image("https://cdn.example.com/image?sig=1")
        self.assertEqual(result, image)
        self.assertIsNone(opener.requests[0]["authorization"])
        self.assertIsNone(opener.requests[1]["authorization"])

        ipv6_dns = {
            "api.example.com": ["93.184.216.34"],
            "bad.example.com": ["fe80::1"],
        }
        with mock.patch.object(mod.socket, "getaddrinfo", side_effect=self._dns(ipv6_dns)):
            transport = mod.ImageTransport(
                "https://api.example.com",
                "secret-token",
                opener=FakeOpener([]),
                allowed_api_hosts=("api.example.com",),
            )
            with self.assertRaises(mod.GenerationError):
                transport.get_public_image("https://bad.example.com/image.png")

        context = FakeTLSContext()
        sockets = []

        def fake_socket(family, socktype, proto=0):
            instance = FakeSocket(family, socktype)
            sockets.append(instance)
            return instance

        connection = mod._PinnedHTTPSConnection(
            "img.example.net",
            pinned_addresses=((socket.AF_INET, ("93.184.216.34", 443)),),
            timeout=12,
            context=context,
        )
        with mock.patch.object(mod.socket, "socket", side_effect=fake_socket):
            connection.connect()
        self.assertEqual(sockets[0].connected, ("93.184.216.34", 443))
        self.assertEqual(context.calls, [{"sockaddr": ("93.184.216.34", 443), "server_hostname": "img.example.net"}])

    def test_decode_and_inspect_with_pillow_rejects_truncated_header_only_and_animated(self):
        png = png_bytes(2, 3)
        jpeg = pillow_image_bytes("RGB", (4, 5), "JPEG", progressive=True)
        webp = pillow_image_bytes("RGBA", (6, 7), "WEBP", lossless=True)

        self.assertEqual(mod.decode_base64_image(base64.b64encode(png).decode("ascii")), png)
        self.assertEqual(mod.inspect_image(png)["extension"], "png")
        self.assertEqual(mod.inspect_image(jpeg), {"format": "jpeg", "extension": "jpg", "width": 4, "height": 5})
        self.assertEqual(mod.inspect_image(webp), {"format": "webp", "extension": "webp", "width": 6, "height": 7})
        with self.assertRaises(mod.GenerationError):
            mod.decode_base64_image("not base64!!!")
        with self.assertRaises(mod.GenerationError):
            mod.inspect_image(b"<html>oops</html>")
        with self.assertRaises(mod.GenerationError):
            mod.inspect_image(png[:-12])
        with self.assertRaises(mod.GenerationError):
            mod.inspect_image(jpeg[:-2])
        with self.assertRaises(mod.GenerationError):
            mod.inspect_image(b"\x89PNG\r\n\x1a\n")

        animated_image = type(
            "AnimatedImage",
            (),
            {"format": "WEBP", "n_frames": 2, "is_animated": True, "size": (2, 2)},
        )()
        with self.assertRaises(mod.GenerationError):
            mod._inspect_open_image(animated_image, mod.SUPPORTED_IMAGE_FORMATS)

    def test_save_images_validates_before_mkdir_rolls_back_and_surfaces_cleanup_failure(self):
        output_dir = os.path.join(self.tmp_root, "images")
        png = png_bytes()
        jpeg = pillow_image_bytes("RGB", (2, 3), "JPEG", progressive=True)
        saved = mod.save_images([png, jpeg], output_dir)
        self.assertEqual(len(saved), 2)
        self.assertTrue(saved[0].endswith(".png"))
        self.assertTrue(saved[1].endswith(".jpg"))
        self.assertEqual(sorted(os.listdir(output_dir)), sorted(os.path.basename(path) for path in saved))

        empty_dir = os.path.join(self.tmp_root, "empty")
        with self.assertRaises(mod.GenerationError):
            mod.save_images([], empty_dir)
        self.assertFalse(os.path.exists(empty_dir))

        rollback_dir = os.path.join(self.tmp_root, "rollback")
        real_open = builtins.open
        open_count = {"value": 0}

        class FailingWriter:
            def __init__(self, path):
                self._handle = real_open(path, "xb")

            def write(self, data):
                raise OSError("disk full")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self._handle.close()
                return False

        def flaky_open(path, mode="r", *args, **kwargs):
            if mode == "xb":
                open_count["value"] += 1
                if open_count["value"] == 2:
                    return FailingWriter(path)
            return real_open(path, mode, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=flaky_open):
            with self.assertRaises(OSError):
                mod.save_images([png, jpeg], rollback_dir)
        self.assertFalse(os.path.exists(rollback_dir))

        cleanup_dir = os.path.join(self.tmp_root, "cleanup")

        with mock.patch("builtins.open", side_effect=lambda path, mode="r", *args, **kwargs: FailingWriter(path)):
            with mock.patch("os.remove", side_effect=OSError("locked file")):
                with self.assertRaises(OSError):
                    mod.save_images([png], cleanup_dir)

    def test_constructor_requires_allowlist_and_strict_url_validation(self):
        with self.assertRaises(ValueError):
            mod.ImageTransport("https://api.example.com", "secret-token", opener=FakeOpener([]))
        with self.assertRaises(ValueError):
            mod.ImageTransport("https://api.example.com", "bad\nline", opener=FakeOpener([]), allowed_api_hosts=("api.example.com",))
        with self.assertRaises(mod.GenerationError):
            mod._validate_https_url(
                "https://[::1",
                allow_query=True,
                allow_any_path=True,
                require_dns_name=False,
                pin_cache={},
            )


if __name__ == "__main__":
    unittest.main()
