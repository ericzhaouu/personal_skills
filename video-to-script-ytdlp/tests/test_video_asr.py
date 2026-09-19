from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import urllib.error
import uuid
import wave
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import video_asr
from video_common import VideoError, read_json


class _Response:
    def __init__(self, payload, headers=None):
        self._buffer = io.BytesIO(payload)
        self.headers = headers or {}

    def read(self, amount=-1):
        return self._buffer.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _RecordingOpener:
    def __init__(self, payloads=None, error=None):
        self.payloads = list(payloads or [])
        self.error = error
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        payload = self.payloads.pop(0)
        return _Response(json.dumps({"text": payload}).encode("utf-8"), headers={"x-request-id": "req-1"})


class _SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def transcribe(self, audio_file, *, language=None):
        self.calls.append((Path(audio_file).name, language))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class VideoAsrTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="video-asr-test-")
        self.work_root = Path(self.temporary.name)
        self.job_dir = self.work_root / "job"
        self.job_dir.mkdir(mode=0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def make_wav(self, path, *, seconds, tail_frames=0):
        total_frames = seconds * video_asr.EXPECTED_SAMPLE_RATE + tail_frames
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(video_asr.EXPECTED_CHANNELS)
            handle.setsampwidth(video_asr.EXPECTED_SAMPLE_WIDTH)
            handle.setframerate(video_asr.EXPECTED_SAMPLE_RATE)
            handle.writeframes((b"\x01\x00" * total_frames))

    def write_config(self, payload):
        path = self.work_root / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_azure_transcriber_uses_env_headers_and_optional_language(self):
        audio_path = self.work_root / "audio.wav"
        self.make_wav(audio_path, seconds=1)
        opener = _RecordingOpener(payloads=["hello", "world"])
        client = video_asr.AzureTranscriber(
            opener=opener,
            env={
                "VIDEO_ASR_BASE_URL": "https://unit.openai.azure.com/openai/deployments/demo",
                "VIDEO_ASR_API_KEY": "super-secret",
            },
        )

        self.assertEqual(client.transcribe(audio_path), "hello")
        self.assertEqual(client.transcribe(audio_path, language="en"), "world")

        first_request = opener.requests[0][0]
        second_request = opener.requests[1][0]
        self.assertEqual(first_request.get_method(), "POST")
        self.assertIn("/audio/transcriptions?api-version=2024-06-01", first_request.full_url)
        self.assertEqual(first_request.headers["Api-key"], "super-secret")
        self.assertNotIn(b"super-secret", first_request.data)
        self.assertIn(b'filename="audio.wav"', first_request.data)
        self.assertNotIn(b'name="language"', first_request.data)
        self.assertIn(b'name="language"', second_request.data)
        self.assertNotIn(b'"translation"', second_request.data)

    def test_azure_transcriber_supports_legacy_env_secret_ref_and_sanitizes_http_errors(self):
        audio_path = self.work_root / "audio.wav"
        self.make_wav(audio_path, seconds=1)
        config_path = self.write_config({
            "tools": {
                "media": {
                    "audio": {
                        "enabled": True,
                        "baseUrl": "https://unit.cognitiveservices.azure.com/openai/deployments/demo",
                        "headers": {"api-key": {"env": "FAKE_ASR_KEY"}},
                    }
                }
            }
        })
        error = urllib.error.HTTPError(
            "https://unit.cognitiveservices.azure.com/openai/deployments/demo/audio/transcriptions?api-version=2024-06-01",
            403,
            "Forbidden",
            {"x-request-id": "req-403"},
            io.BytesIO(b'{"error":"hidden"}'),
        )
        client = video_asr.AzureTranscriber(
            config_path=config_path,
            opener=_RecordingOpener(error=error),
            env={"FAKE_ASR_KEY": "legacy-secret"},
        )

        with self.assertRaises(VideoError) as raised:
            client.transcribe(audio_path)

        self.assertEqual(raised.exception.code, "permission_denied")
        self.assertEqual(raised.exception.details["http_status"], 403)
        self.assertEqual(raised.exception.details["request_id"], "req-403")
        self.assertNotIn("hidden", str(raised.exception))

    def test_azure_transcriber_rejects_unsupported_secret_reference(self):
        audio_path = self.work_root / "audio.wav"
        self.make_wav(audio_path, seconds=1)
        config_path = self.write_config({
            "baseUrl": "https://unit.openai.azure.com/openai/deployments/demo",
            "headers": {"api-key": {"vault": "unsupported"}},
        })
        client = video_asr.AzureTranscriber(config_path=config_path, opener=_RecordingOpener(payloads=["unused"]), env={})

        with self.assertRaises(VideoError) as raised:
            client.transcribe(audio_path)

        self.assertEqual(raised.exception.code, "configuration_error")

    def test_transcribe_audio_retries_only_failed_chunks(self):
        audio_path = self.work_root / "two-seconds.wav"
        self.make_wav(audio_path, seconds=2)
        first_client = _SequenceClient(["alpha", VideoError("try again", code="operation_failed")])
        second_client = _SequenceClient(["beta"])

        with mock.patch.object(video_asr, "CHUNK_SECONDS", 1):
            with self.assertRaises(VideoError) as raised:
                video_asr.transcribe_audio(audio_path, self.job_dir, source_language="zh", client=first_client)
            self.assertEqual(raised.exception.details["chunk_index"], 2)

            result = video_asr.transcribe_audio(audio_path, self.job_dir, source_language="zh", client=second_client)

        self.assertEqual(first_client.calls, [("chunk-0001.wav", "zh"), ("chunk-0002.wav", "zh")])
        self.assertEqual(second_client.calls, [("chunk-0002.wav", "zh")])
        self.assertEqual(result["text"], "alpha\n\nbeta")
        self.assertEqual(result["source_type"], "asr")
        self.assertEqual(result["language"], "zh")
        self.assertEqual(result["warnings"], [video_asr.WARNING_TEXT])
        self.assertNotIn("translation", result)
        self.assertEqual([segment["text"] for segment in result["segments"]], ["alpha", "beta"])

    def test_long_normalized_audio_is_split_before_per_request_cap(self):
        path = self.work_root / "long.wav"
        self.make_wav(path, seconds=2)
        client = _SequenceClient(["first", "second"])
        with mock.patch.object(video_asr, "CHUNK_SECONDS", 1), \
                mock.patch.object(video_asr, "MAX_ASR_FILE_BYTES", 40000):
            result = video_asr.transcribe_audio(path, self.job_dir, client=client)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result["text"], "first\n\nsecond")

    def test_transcribe_audio_requires_explicit_retry_after_unknown_outcome(self):
        audio_path = self.work_root / "audio.wav"
        self.make_wav(audio_path, seconds=1)
        uncertain = _SequenceClient([VideoError("network lost", code="unknown_outcome")])

        with mock.patch.object(video_asr, "CHUNK_SECONDS", 1):
            with self.assertRaises(VideoError) as first_error:
                video_asr.transcribe_audio(audio_path, self.job_dir, client=uncertain)
            self.assertEqual(first_error.exception.code, "unknown_outcome")

            blocked_client = _SequenceClient(["should not run"])
            with self.assertRaises(VideoError) as second_error:
                video_asr.transcribe_audio(audio_path, self.job_dir, client=blocked_client)
            self.assertEqual(second_error.exception.code, "unknown_outcome")
            self.assertEqual(blocked_client.calls, [])

            resumed_client = _SequenceClient(["done"])
            result = video_asr.transcribe_audio(audio_path, self.job_dir, retry_uncertain=True, client=resumed_client)

        self.assertEqual(resumed_client.calls, [("chunk-0001.wav", None)])
        self.assertEqual(result["text"], "done")

    def test_transcribe_audio_rejects_changed_input_checkpoint(self):
        audio_path = self.work_root / "audio.wav"
        self.make_wav(audio_path, seconds=1)
        client = _SequenceClient(["stable"])

        with mock.patch.object(video_asr, "CHUNK_SECONDS", 1):
            video_asr.transcribe_audio(audio_path, self.job_dir, client=client)
            self.make_wav(audio_path, seconds=2)
            with self.assertRaises(VideoError) as raised:
                video_asr.transcribe_audio(audio_path, self.job_dir, client=_SequenceClient(["unused"]))

        self.assertEqual(raised.exception.code, "invalid_state")

    def test_transcribe_audio_rejects_corrupt_completed_artifacts(self):
        audio_path = self.work_root / "audio.wav"
        self.make_wav(audio_path, seconds=1)
        client = _SequenceClient(["complete"])

        with mock.patch.object(video_asr, "CHUNK_SECONDS", 1):
            video_asr.transcribe_audio(audio_path, self.job_dir, client=client)
            state_path = self.job_dir / "asr" / "state.json"
            state = read_json(state_path)
            result_path = self.job_dir / state["chunks"][0]["result_path"]
            result_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(VideoError) as raised:
                video_asr.transcribe_audio(audio_path, self.job_dir, client=_SequenceClient(["unused"]))

        self.assertEqual(raised.exception.code, "invalid_state")


if __name__ == "__main__":
    unittest.main()
