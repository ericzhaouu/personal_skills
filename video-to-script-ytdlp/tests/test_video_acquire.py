from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import types
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "video_acquire.py"
WORK_ROOT = ROOT / ".test-work"


sys.path.insert(0, str(ROOT / "scripts"))
from video_common import VideoError as FakeVideoError


def fake_run_external(argv, *, cwd, timeout, max_output=8 * 1024 * 1024):
    raise AssertionError("Tests must provide a runner")


def load_video_acquire():
    sys.modules.pop("video_acquire_under_test", None)
    spec = importlib.util.spec_from_file_location("video_acquire_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class VideoAcquireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.video_acquire = load_video_acquire()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="video-acquire-test-")
        self.work_dir = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def make_cookie(self, name="source.cookies.txt", content=b"sessionid=abc\n"):
        path = self.work_dir / name
        path.write_bytes(content)
        return path

    def test_probe_copies_cookies_and_uses_safe_command(self):
        cookie = self.make_cookie()
        expected = {
            "id": "abc123",
            "title": "Hello",
            "uploader": "Uploader",
            "duration": 12.5,
            "webpage_url": "https://www.youtube.com/watch?v=abc123",
            "extractor": "youtube",
            "language": "en-US",
            "is_live": False,
            "subtitles": {"en": [{"ext": "vtt", "url": "https://signed.example/caption"}]},
            "automatic_captions": {},
        }
        calls = []

        def runner(argv, *, cwd, timeout, max_output):
            calls.append((argv, Path(cwd), timeout, max_output))
            return json.dumps(expected)

        result = self.video_acquire.probe(
            "https://www.youtube.com/watch?v=abc123",
            self.work_dir / "job",
            cookie_file=cookie,
            ytdlp="C:\\tools\\yt-dlp.exe",
            runner=runner,
        )

        copied = self.work_dir / "job" / "cookies.txt"
        digest = hashlib.sha256(cookie.read_bytes()).hexdigest()
        self.assertEqual(result["title"], "Hello")
        self.assertTrue(copied.exists())
        self.assertEqual(copied.read_bytes(), cookie.read_bytes())
        self.assertEqual((self.work_dir / "job" / "cookies.txt.sha256").read_text(encoding="ascii"), digest)
        argv = calls[0][0]
        self.assertEqual(argv[0], "C:\\tools\\yt-dlp.exe")
        self.assertIn("--ignore-config", argv)
        self.assertIn("--no-playlist", argv)
        self.assertIn("--skip-download", argv)
        self.assertIn("--dump-single-json", argv)
        self.assertIn("--", argv)
        self.assertIn("--no-remote-components", argv)
        self.assertEqual(argv[-1], "https://www.youtube.com/watch?v=abc123")
        self.assertNotIn("sessionid=abc", "\n".join(argv))
        self.assertNotIn("signed.example", repr(calls))

    def test_probe_rejects_invalid_urls_and_missing_cookies(self):
        with self.assertRaises(FakeVideoError) as bad_host:
            self.video_acquire.probe("https://youtube.com.evil.example/watch?v=abc", self.work_dir / "job", runner=lambda *args, **kwargs: "")
        self.assertEqual(bad_host.exception.code, "invalid_url")
        with self.assertRaises(FakeVideoError) as bad_option:
            self.video_acquire.probe("--output hacked", self.work_dir / "job", runner=lambda *args, **kwargs: "")
        self.assertEqual(bad_option.exception.code, "invalid_url")
        with self.assertRaises(FakeVideoError) as missing_cookie:
            self.video_acquire.probe(
                "https://youtu.be/abc123",
                self.work_dir / "job",
                cookie_file=self.work_dir / "missing.txt",
                runner=lambda *args, **kwargs: "",
            )
        self.assertEqual(missing_cookie.exception.code, "invalid_cookie_file")

    def test_select_caption_prefers_requested_language_auto_before_unrelated_manual(self):
        metadata = {
            "language": "zh-CN",
            "subtitles": {"en": [{"ext": "vtt", "name": "English"}]},
            "automatic_captions": {"zh-CN": [{"ext": "json3", "name": "Chinese (auto-generated)"}]},
        }
        chosen = self.video_acquire.select_caption(metadata, source_language="zh-CN")
        self.assertEqual(
            chosen,
            {
                "language": "zh-cn",
                "download_language": "zh-CN",
                "format": "json3",
                "source_type": "automatic_subtitles",
            },
        )

    def test_select_caption_prefers_manual_and_ignores_translated_when_possible(self):
        metadata = {
            "language": "en",
            "subtitles": {
                "en-orig": [{"ext": "vtt", "name": "English"}],
                "fr": [{"ext": "vtt", "name": "French translated subtitles"}],
            },
            "automatic_captions": {"en": [{"ext": "json3", "name": "English (auto-generated)"}]},
        }
        chosen = self.video_acquire.select_caption(metadata)
        self.assertEqual(chosen["download_language"], "en-orig")
        self.assertEqual(chosen["source_type"], "manual_subtitles")

    def test_foreign_or_auto_translated_track_is_not_original(self):
        metadata = {"language": "en", "subtitles": {"fr": [{"ext": "vtt"}]},
                    "automatic_captions": {"en": [{"ext": "vtt", "url": "https://example.com/captions?tlang=en"}]}}
        self.assertIsNone(self.video_acquire.select_caption(metadata))

    def test_adjacent_intentional_repeat_is_retained(self):
        segments = [{"start": 0, "end": 1, "text": "Yes"},
                    {"start": 1, "end": 2, "text": "Yes"}]
        self.assertEqual(len(self.video_acquire._dedupe_overlapping_segments(segments)), 2)

    def test_fetch_caption_parses_json3_and_dedupes_rolling_captions(self):
        metadata = {"id": "abc123"}
        track = {
            "language": "en",
            "download_language": "en",
            "format": "json3",
            "source_type": "automatic_subtitles",
        }

        def runner(argv, *, cwd, timeout, max_output):
            captions_dir = Path(cwd) / "captions"
            captions_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "events": [
                    {"tStartMs": 0, "dDurationMs": 1200, "segs": [{"utf8": "Hello"}]},
                    {"tStartMs": 800, "dDurationMs": 1200, "segs": [{"utf8": "Hello world"}]},
                    {"tStartMs": 2500, "dDurationMs": 1000, "segs": [{"utf8": "Hello world"}]},
                ]
            }
            (captions_dir / "abc123.en.json3").write_text(json.dumps(payload), encoding="utf-8")
            return ""

        result = self.video_acquire.fetch_caption(
            "https://youtu.be/abc123",
            metadata,
            track,
            self.work_dir / "job",
            runner=runner,
        )
        self.assertEqual(result["text"], "Hello world\nHello world")
        self.assertEqual(len(result["segments"]), 2)
        self.assertEqual(result["segments"][0]["start"], 0.0)
        self.assertEqual(result["segments"][0]["end"], 2.0)

    def test_fetch_caption_parses_vtt_and_srt(self):
        metadata = {"id": "abc123"}
        vtt_track = {
            "language": "en",
            "download_language": "en",
            "format": "vtt",
            "source_type": "manual_subtitles",
        }
        srt_track = {
            "language": "en",
            "download_language": "en",
            "format": "srt",
            "source_type": "manual_subtitles",
        }

        def vtt_runner(argv, *, cwd, timeout, max_output):
            target = Path(cwd) / "captions" / "abc123.en.vtt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "WEBVTT\n\nNOTE skip\nignore\n\n00:00:00.000 --> 00:00:01.500\n<v Speaker>Hello<br>world</v>\n",
                encoding="utf-8",
            )
            return ""

        def srt_runner(argv, *, cwd, timeout, max_output):
            target = Path(cwd) / "captions" / "abc123.en.srt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\n<i>Hello</i>\n\n2\n00:00:02,000 --> 00:00:03,000\nworld\n",
                encoding="utf-8",
            )
            return ""

        vtt = self.video_acquire.fetch_caption("https://youtu.be/abc123", metadata, vtt_track, self.work_dir / "vtt", runner=vtt_runner)
        srt = self.video_acquire.fetch_caption("https://youtu.be/abc123", metadata, srt_track, self.work_dir / "srt", runner=srt_runner)
        self.assertEqual(vtt["segments"][0]["text"], "Hello\nworld")
        self.assertEqual(srt["text"], "Hello\nworld")

    def test_fetch_caption_rejects_empty_or_malformed_captions(self):
        metadata = {"id": "abc123"}
        track = {
            "language": "en",
            "download_language": "en",
            "format": "vtt",
            "source_type": "manual_subtitles",
        }

        def empty_runner(argv, *, cwd, timeout, max_output):
            target = Path(cwd) / "captions" / "abc123.en.vtt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("WEBVTT\n\n", encoding="utf-8")
            return ""

        def bad_runner(argv, *, cwd, timeout, max_output):
            target = Path(cwd) / "captions" / "abc123.en.vtt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("WEBVTT\n\nbad timestamp\ncaption\n", encoding="utf-8")
            return ""

        with self.assertRaises(FakeVideoError) as empty_exc:
            self.video_acquire.fetch_caption("https://youtu.be/abc123", metadata, track, self.work_dir / "empty", runner=empty_runner)
        self.assertEqual(empty_exc.exception.code, "caption_empty")
        with self.assertRaises(FakeVideoError) as bad_exc:
            self.video_acquire.fetch_caption("https://youtu.be/abc123", metadata, track, self.work_dir / "bad", runner=bad_runner)
        self.assertEqual(bad_exc.exception.code, "invalid_caption_format")

    def test_download_audio_returns_exact_file_inside_media_dir(self):
        metadata = {"id": "abc123"}

        def runner(argv, *, cwd, timeout, max_output):
            media_dir = Path(cwd) / "media"
            media_dir.mkdir(parents=True, exist_ok=True)
            target = media_dir / "abc123.m4a"
            target.write_bytes(b"x" * 32)
            return str(target) + "\n"

        result = self.video_acquire.download_audio(
            "https://www.bilibili.com/video/BV1xx",
            metadata,
            self.work_dir / "job",
            max_bytes=128,
            runner=runner,
        )
        self.assertEqual(result.name, "abc123.m4a")
        self.assertTrue(result.is_file())

    def test_download_audio_rejects_escape_multiple_or_oversized_files(self):
        metadata = {"id": "abc123"}

        def escape_runner(argv, *, cwd, timeout, max_output):
            outside = self.work_dir / "outside.m4a"
            outside.write_bytes(b"abc")
            return str(outside) + "\n"

        def multi_runner(argv, *, cwd, timeout, max_output):
            media_dir = Path(cwd) / "media"
            media_dir.mkdir(parents=True, exist_ok=True)
            first = media_dir / "abc123.m4a"
            second = media_dir / "abc123.webm"
            first.write_bytes(b"1")
            second.write_bytes(b"2")
            return str(first) + "\n" + str(second) + "\n"

        def large_runner(argv, *, cwd, timeout, max_output):
            media_dir = Path(cwd) / "media"
            media_dir.mkdir(parents=True, exist_ok=True)
            target = media_dir / "abc123.m4a"
            target.write_bytes(b"x" * 64)
            return str(target) + "\n"

        with self.assertRaises(FakeVideoError) as escape_exc:
            self.video_acquire.download_audio("https://youtu.be/abc123", metadata, self.work_dir / "escape", runner=escape_runner)
        self.assertEqual(escape_exc.exception.code, "download_missing")
        with self.assertRaises(FakeVideoError) as multi_exc:
            self.video_acquire.download_audio("https://youtu.be/abc123", metadata, self.work_dir / "multi", runner=multi_runner)
        self.assertEqual(multi_exc.exception.code, "download_missing")
        with self.assertRaises(FakeVideoError) as large_exc:
            self.video_acquire.download_audio("https://youtu.be/abc123", metadata, self.work_dir / "large", max_bytes=16, runner=large_runner)
        self.assertEqual(large_exc.exception.code, "download_too_large")


if __name__ == "__main__":
    unittest.main()
