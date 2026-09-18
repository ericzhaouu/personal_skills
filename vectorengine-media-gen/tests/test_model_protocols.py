import base64
import io
import os
import struct
import sys
import unittest
import zlib


THIS_DIR = os.path.dirname(os.path.abspath(__file__))
UPDATED_DIR = os.path.dirname(THIS_DIR)
SCRIPTS_DIR = os.path.join(UPDATED_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import generate
import model_protocols as mod
from image_transport import GenerationError


def png_bytes(width=1, height=1):
    def chunk(name, payload):
        crc = zlib.crc32(name)
        crc = zlib.crc32(payload, crc) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + (b"\x10\x20\x30" * width)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b"")


class FakeTransport:
    def __init__(self, image_bytes):
        self.image_bytes = image_bytes
        self.urls = []

    def get_public_image(self, url):
        self.urls.append(url)
        return self.image_bytes


class ModelProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = generate.load_registry()
        cls.png = png_bytes()
        cls.png_b64 = base64.b64encode(cls.png).decode("ascii")

    def test_registry_contains_exact_four_models_in_order(self):
        keys = list(self.registry["image"])
        self.assertEqual(
            keys,
            [
                "gpt-image-2.5-flare",
                "gpt-image-2.5-sunburst",
                "gemini-3.1-flash-image",
                "doubao-seedream-5-0-pro-260628",
            ],
        )
        self.assertEqual(self.registry["schemaVersion"], 2)
        self.assertEqual(self.registry["defaultModel"], "gpt-image-2.5-flare")
        self.assertEqual(self.registry["allowedApiHosts"], ["api.relayrouter.ai"])
        self.assertEqual(len(self.registry["image"]), 4)
        for key, value in self.registry["image"].items():
            self.assertEqual(value["modelId"], key)

    def test_gpt_build_request_uses_exact_fields_and_geometry(self):
        config = self.registry["image"]["gpt-image-2.5-flare"]
        path, payload, requested = mod.build_request(
            "gpt-image-2.5-flare", config, "draw a lighthouse", size="3:4", resolution="1K", count=2
        )
        self.assertEqual(path, "/v1/images/generations")
        self.assertEqual(payload["model"], "gpt-image-2.5-flare")
        self.assertEqual(payload["size"], "864x1152")
        self.assertEqual(payload["format"], "png")
        self.assertEqual(payload["quality"], "auto")
        self.assertEqual(payload["response_format"], "b64_json")
        self.assertNotIn("output_format", payload)
        self.assertNotIn("Authorization", payload)
        self.assertEqual(requested["applied_size"], "864x1152")
        self.assertFalse(requested["resolution_ignored"])
        explicit = mod.build_request(
            "gpt-image-2.5-flare", config, "draw a bridge", size="1600x896", resolution="2K", count=1
        )[2]
        self.assertTrue(explicit["resolution_ignored"])
        self.assertEqual(explicit["applied_size"], "1600x896")
        with self.assertRaises(GenerationError):
            mod.build_request("gpt-image-2.5-flare", config, "x" * 1001, size="1:1", resolution="1K", count=1)

    def test_gemini_build_request_keeps_exact_id_and_native_size(self):
        config = self.registry["image"]["gemini-3.1-flash-image"]
        path, payload, requested = mod.build_request(
            "gemini-3.1-flash-image", config, "make a poster", size="1536x864", resolution="2K", count=1
        )
        self.assertEqual(path, "/v1beta/models/gemini-3.1-flash-image:generateContent")
        self.assertNotIn("?", path)
        self.assertEqual(payload["contents"][0]["parts"][0]["text"], "make a poster")
        self.assertEqual(payload["generationConfig"]["responseModalities"], ["TEXT", "IMAGE"])
        self.assertEqual(payload["generationConfig"]["imageConfig"], {"aspectRatio": "16:9", "imageSize": "2K"})
        self.assertNotIn("key", payload)
        self.assertTrue(requested["explicit_pixels_used_for_ratio_only"])
        self.assertTrue(requested["provider_returns_native_geometry"])
        with self.assertRaises(GenerationError):
            mod.build_request("gemini-3.1-flash-image", config, "x", size="7:5", resolution="1K", count=1)
        with self.assertRaises(GenerationError):
            mod.build_request("gemini-3.1-flash-image", config, "x", size="1:1", resolution="1K", count=2)

    def test_seedream_build_request_uses_documented_endpoint_and_notes_geometry(self):
        config = self.registry["image"]["doubao-seedream-5-0-pro-260628"]
        path, payload, requested = mod.build_request(
            "doubao-seedream-5-0-pro-260628", config, "city skyline", size="16:9", resolution="1K", count=1
        )
        self.assertEqual(path, "/api/v3/images/generations")
        self.assertEqual(payload["model"], "doubao-seedream-5-0-pro-260628")
        self.assertEqual(payload["size"], "1424x800")
        self.assertEqual(payload["response_format"], "b64_json")
        self.assertEqual(payload["output_format"], "png")
        self.assertFalse(payload["watermark"])
        self.assertEqual(requested["applied_size"], "1424x800")
        self.assertIn("near-16:9", requested["geometry_note"])
        explicit = mod.build_request(
            "doubao-seedream-5-0-pro-260628", config, "city skyline", size="2816x1584", resolution="1K", count=1
        )[2]
        self.assertTrue(explicit["resolution_ignored"])
        with self.assertRaises(GenerationError):
            mod.build_request("doubao-seedream-5-0-pro-260628", config, "x", size="1:1", resolution="4K", count=1)
        with self.assertRaises(GenerationError):
            mod.build_request("doubao-seedream-5-0-pro-260628", config, "x", size="1:1", resolution="1K", count=2)

    def test_gpt_extract_images_supports_base64_and_url(self):
        config = self.registry["image"]["gpt-image-2.5-flare"]
        transport = FakeTransport(self.png)
        images = mod.extract_images(
            "gpt-image-2.5-flare",
            config,
            {"data": [{"b64_json": self.png_b64}, {"url": "https://cdn.example.test/a.png"}]},
            transport,
        )
        self.assertEqual(images, [self.png, self.png])
        self.assertEqual(transport.urls, ["https://cdn.example.test/a.png"])
        with self.assertRaises(GenerationError):
            mod.extract_images("gpt-image-2.5-flare", config, {"model": "wrong", "data": [{"b64_json": self.png_b64}]}, transport)

    def test_gemini_extract_images_supports_inline_and_file_urls(self):
        config = self.registry["image"]["gemini-3.1-flash-image"]
        transport = FakeTransport(self.png)
        response = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {"text": "ignored"},
                            {"inlineData": {"mimeType": "image/png", "data": self.png_b64}},
                            {"fileData": {"mimeType": "image/png", "fileUri": "https://cdn.example.test/g.png"}},
                        ]
                    },
                }
            ]
        }
        self.assertEqual(mod.extract_images("gemini-3.1-flash-image", config, response, transport), [self.png, self.png])
        self.assertEqual(transport.urls, ["https://cdn.example.test/g.png"])
        with self.assertRaises(GenerationError) as blocked:
            mod.extract_images(
                "gemini-3.1-flash-image",
                config,
                {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []},
                transport,
            )
        self.assertEqual(blocked.exception.category, "policy")
        with self.assertRaises(GenerationError):
            mod.extract_images(
                "gemini-3.1-flash-image",
                config,
                {"candidates": [{"finishReason": "RECITATION", "content": {"parts": []}}]},
                transport,
            )

    def test_seedream_extract_images_handles_final_images_and_pending_task(self):
        config = self.registry["image"]["doubao-seedream-5-0-pro-260628"]
        transport = FakeTransport(self.png)
        response = {"code": 0, "data": {"images": [{"b64_json": self.png_b64}, {"url": "https://cdn.example.test/s.png"}]}}
        self.assertEqual(mod.extract_images("doubao-seedream-5-0-pro-260628", config, response, transport), [self.png, self.png])
        self.assertEqual(transport.urls, ["https://cdn.example.test/s.png"])
        with self.assertRaises(GenerationError) as pending:
            mod.extract_images(
                "doubao-seedream-5-0-pro-260628",
                config,
                {"code": 0, "data": {"task_id": "task_example_001"}},
                transport,
            )
        self.assertEqual(pending.exception.category, "pending")
        self.assertEqual(pending.exception.request_id, "task_example_001")

    def test_nested_error_and_no_image_cases_fail_closed(self):
        transport = FakeTransport(self.png)
        gpt = self.registry["image"]["gpt-image-2.5-flare"]
        with self.assertRaises(GenerationError) as nested:
            mod.extract_images("gpt-image-2.5-flare", gpt, {"data": {"error": {"message": "denied", "request_id": "req-1"}}}, transport)
        self.assertEqual(nested.exception.request_id, "req-1")
        gemini = self.registry["image"]["gemini-3.1-flash-image"]
        with self.assertRaises(GenerationError):
            mod.extract_images("gemini-3.1-flash-image", gemini, {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "not an image"}]}}]}, transport)
        seedream = self.registry["image"]["doubao-seedream-5-0-pro-260628"]
        with self.assertRaises(GenerationError):
            mod.extract_images("doubao-seedream-5-0-pro-260628", seedream, {"code": 0, "data": []}, transport)

    def test_documented_error_request_id_is_preserved_without_message(self):
        transport = FakeTransport(self.png)
        config = self.registry["image"]["gpt-image-2.5-flare"]
        with self.assertRaises(GenerationError) as failure:
            mod.extract_images("gpt-image-2.5-flare", config, {
                "error": {"message": "private detail (request id: 20260805120000123456789AbCdEfGh)"}
            }, transport)
        self.assertEqual(failure.exception.request_id, "20260805120000123456789AbCdEfGh")
        self.assertNotIn("private detail", failure.exception.message)

    def test_invalid_pixels_and_mime_fail_explicitly(self):
        for size in ("0x0", "0x1024"):
            for model in self.registry["image"]:
                with self.subTest(size=size, model=model), self.assertRaises(GenerationError):
                    mod.build_request(model, self.registry["image"][model], "sample",
                                      size=size, resolution="1K", count=1)
        config = self.registry["image"]["gemini-3.1-flash-image"]
        with self.assertRaises(GenerationError):
            mod.extract_images("gemini-3.1-flash-image", config, {
                "candidates": [{"content": {"parts": [{"inlineData": {"mimeType": 7, "data": self.png_b64}}]}}]
            }, FakeTransport(self.png))


if __name__ == "__main__":
    unittest.main()
