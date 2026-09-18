import argparse
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate
from image_transport import GenerationError, extract_request_id
from PIL import Image


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="relayrouter-test-")
        self.addCleanup(self.work.cleanup)
        self.registry = generate.load_registry()
        image = Image.new("RGB", (4, 4), "orange")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        self.encoded = base64.b64encode(buffer.getvalue()).decode()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.stdout))
        self.enterContext(contextlib.redirect_stderr(self.stderr))

    def args(self, *extra):
        return generate.build_parser().parse_args([
            "--media", "image", "--prompt", "public geometry", "--size", "1:1",
            "--output-dir", str(Path(self.work.name) / "output"), *extra,
        ])

    def test_environment_only_key_and_saved_output_contract(self):
        calls = []
        outer = self

        class Transport:
            def __init__(self, base, token, **options):
                calls.append({"base": base, "token": token, "options": options})
            def request_json(self, path, body):
                calls.append({"path": path, "body": body})
                return {"data": [{"b64_json": outer.encoded}]}

        with patch.dict(os.environ, {"VECENGINE_API_TOKEN": "test-value"}, clear=True):
            result = generate.generate(self.args(), self.registry, transport_factory=Transport)
        self.assertEqual(calls[0]["token"], "test-value")
        self.assertEqual(calls[1]["body"]["model"], generate.DEFAULT_IMAGE_MODEL)
        self.assertEqual(result["model"], generate.DEFAULT_IMAGE_MODEL)
        self.assertEqual(len(result["files"]), 1)
        self.assertTrue(Path(result["files"][0]).is_file())
        self.assertIn("SAVED: ", self.stdout.getvalue())
        self.assertNotIn("test-value", self.stdout.getvalue() + self.stderr.getvalue())
        summary = json.loads(self.stdout.getvalue().splitlines()[-1])
        self.assertEqual(summary["prompt"], "public geometry")
        self.assertEqual(summary["files"], result["files"])

    def test_missing_key_never_uses_other_provider_credentials(self):
        with patch.dict(os.environ, {"VOLCANO_ENGINE_API_KEY": "unrelated"}, clear=True):
            with self.assertRaises(GenerationError) as failure:
                generate.generate(self.args(), self.registry,
                                  transport_factory=lambda *args, **kwargs: self.fail("No network"))
        self.assertEqual(failure.exception.category, "authentication")
        self.assertFalse((Path(self.work.name) / "output").exists())

    def test_command_line_token_is_not_accepted(self):
        with self.assertRaises(SystemExit) as failure:
            self.args("--token", "not-a-real-key")
        self.assertEqual(failure.exception.code, 2)

    def test_fallback_has_exact_order_once_per_model(self):
        attempts = []
        outer = self

        class Transport:
            def __init__(self, *args, **kwargs):
                pass
            def request_json(self, path, body):
                model = body.get("model", "gemini-3.1-flash-image")
                attempts.append(model)
                if model != generate.MODEL_ORDER[-1]:
                    raise GenerationError("Unavailable", status=503, can_fallback=True)
                return {"code": 0, "data": [{"b64_json": outer.encoded}]}

        with patch.dict(os.environ, {"VECENGINE_API_TOKEN": "test-value"}, clear=True):
            result = generate.generate(self.args(), self.registry, transport_factory=Transport)
        self.assertEqual(attempts, list(generate.MODEL_ORDER))
        self.assertEqual(result["model"], generate.MODEL_ORDER[-1])

    def test_unknown_outcome_and_explicit_selection_do_not_retry(self):
        for explicit, failure in (
            ((), GenerationError("Uncertain", category="unknown_outcome")),
            (("--model", "gpt-image-2.5-sunburst"),
             GenerationError("Unavailable", status=503, can_fallback=True)),
        ):
            calls = []
            class Transport:
                def __init__(self, *args, **kwargs):
                    pass
                def request_json(self, path, body):
                    calls.append(path)
                    raise failure
            with patch.dict(os.environ, {"VECENGINE_API_TOKEN": "test-value"}, clear=True):
                with self.assertRaises(GenerationError):
                    generate.generate(self.args(*explicit), self.registry, transport_factory=Transport)
            self.assertEqual(len(calls), 1)

    def test_removed_model_rejected_before_network(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(GenerationError) as failure:
            generate.generate(self.args("--model", "gpt-image-2"), self.registry,
                              transport_factory=lambda *args, **kwargs: self.fail("No network"))
        self.assertEqual(failure.exception.category, "input")

    def test_model_list_needs_no_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(generate.main(["--media", "image", "--list-models"]), 0)
        listed = self.stdout.getvalue()
        positions = [listed.index(model + " ") for model in generate.MODEL_ORDER]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("gpt-image-2.5-flare [family=gpt-images] (default)", listed)

    def test_request_id_suffix_does_not_leak_credential(self):
        value = {"error": {"message": "Invalid request (request id: req-public-42)"}}
        self.assertEqual(extract_request_id(value), "req-public-42")
        self.assertIsNone(extract_request_id({"request_id": "sk-credential-value"}))
        self.assertIsNone(extract_request_id({"request_id": "actual-token"}, token="actual-token"))


if __name__ == "__main__":
    unittest.main()
