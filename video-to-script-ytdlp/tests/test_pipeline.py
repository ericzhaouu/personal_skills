import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pipeline
from video_common import VideoError, atomic_json, private_directory, read_json, run_external


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.metadata = {"id": "example123", "title": "中文标题测试", "duration": 9,
                         "extractor": "youtube", "uploader": "Test", "language": "en",
                         "is_live": False, "subtitles": {}, "automatic_captions": {}}
        self.transcript = {
            "text": "A fact. A limit.", "segments": [
                {"start": 0.0, "end": 4.0, "text": "A fact."},
                {"start": 4.0, "end": 9.0, "text": "A limit."}],
            "source_type": "manual_subtitles", "language": "en", "warnings": [],
        }

    def args(self, *extras):
        return pipeline.parser().parse_args([
            "run", "https://www.youtube.com/watch?v=example123",
            "--output-dir", str(self.root), *extras,
        ])

    def subtitle_mocks(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        probe = stack.enter_context(patch.object(pipeline.video_acquire, "probe", return_value=self.metadata))
        stack.enter_context(patch.object(pipeline.video_acquire, "select_caption", return_value={"language": "en"}))
        caption = stack.enter_context(patch.object(pipeline.video_acquire, "fetch_caption", return_value=self.transcript))
        asr = stack.enter_context(patch.object(pipeline.video_asr, "transcribe_audio",
                                             side_effect=AssertionError("No paid ASR")))
        return probe, caption, asr

    def test_subtitles_complete_without_credentials_or_asr(self):
        self.subtitle_mocks()
        with patch.dict(os.environ, {}, clear=True):
            job, state, result = pipeline.process_job(self.args())
        self.assertEqual(state["status"], "complete")
        self.assertTrue(result.is_file())
        self.assertIn("manual_subtitles", result.read_text(encoding="utf-8"))
        self.assertIn("[00:00:00–00:00:04]", result.read_text(encoding="utf-8"))
        self.assertEqual(read_json(job / "transcript.raw.json"), self.transcript)

    def test_same_title_same_day_does_not_overwrite(self):
        self.subtitle_mocks()
        first = pipeline.process_job(self.args())[2]
        second = pipeline.process_job(self.args())[2]
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file() and second.is_file())
        first.name.encode("utf-8", errors="strict")

    def test_legacy_translation_is_handoff_not_fabricated_translation(self):
        self.subtitle_mocks()
        args = self.args()
        args.legacy_language = "zh"
        job, state, result = pipeline.process_job(args)
        self.assertEqual(state["status"], "partial")
        self.assertEqual(state["translation"], {"target": "zh", "status": "handoff_required"})
        self.assertIn("NOT generated", result.read_text(encoding="utf-8"))
        self.assertEqual(read_json(job / "transcript.raw.json")["text"], self.transcript["text"])

    def test_format_preserves_raw_and_nonwhitespace(self):
        self.subtitle_mocks()
        original = self.transcript["text"]
        job, state, result = pipeline.process_job(self.args("--format"))
        derivative = Path(state["artifacts"]["formatted"]["path"]).read_text(encoding="utf-8")
        self.assertEqual("".join(derivative.split()), "".join(original.split()))
        self.assertEqual(read_json(job / "transcript.raw.json")["text"], original)
        self.assertNotIn("## 译文", result.read_text(encoding="utf-8"))

    def test_no_caption_requires_explicit_asr_and_preserves_job(self):
        with patch.object(pipeline.video_acquire, "probe", return_value=self.metadata), \
                patch.object(pipeline.video_acquire, "select_caption", return_value=None), \
                patch.object(pipeline.video_acquire, "download_audio") as download:
            with self.assertRaises(VideoError) as error:
                pipeline.process_job(self.args())
        self.assertEqual(error.exception.code, "asr_approval_required")
        download.assert_not_called()
        state = read_json(Path(error.exception.details["job_dir"]) / "job.json")
        self.assertEqual(state["status"], "blocked")

    def test_duration_limit_blocks_before_subtitles_or_audio(self):
        self.metadata["duration"] = 1801
        with patch.object(pipeline.video_acquire, "probe", return_value=self.metadata), \
                patch.object(pipeline.video_acquire, "fetch_caption") as caption:
            with self.assertRaises(VideoError) as error:
                pipeline.process_job(self.args())
        self.assertEqual(error.exception.code, "duration_limit")
        caption.assert_not_called()

    def test_resume_completed_job_makes_no_network_calls(self):
        probe, caption, _ = self.subtitle_mocks()
        job, _, result = pipeline.process_job(self.args())
        args = pipeline.parser().parse_args(["run", "--resume", str(job)])
        _, state, again = pipeline.process_job(args)
        self.assertEqual(again, result)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(caption.call_count, 1)
        self.assertEqual(state["status"], "complete")

    def test_resume_recovers_published_file_before_checkpoint(self):
        probe, _, _ = self.subtitle_mocks()
        job, state, result = pipeline.process_job(self.args("--format"))
        state["artifacts"].pop("result")
        state["status"] = "created"
        atomic_json(job / "job.json", state)
        _, resumed, again = pipeline.process_job(pipeline.parser().parse_args(["run", "--resume", str(job)]))
        self.assertEqual(result, again)
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(probe.call_count, 1)

    def test_resume_recovers_own_interrupted_output_without_overwriting_other_content(self):
        self.subtitle_mocks()
        job, state, result = pipeline.process_job(self.args())
        content = result.read_bytes()
        state["artifacts"].pop("result")
        atomic_json(job / "job.json", state)
        result.write_bytes(content[:20])
        pipeline.process_job(pipeline.parser().parse_args(["run", "--resume", str(job)]))
        self.assertEqual(result.read_bytes(), content)

    def test_subsecond_timing_and_cookie_cleanup(self):
        self.assertEqual(pipeline.timestamp(0.2), "00:00:00.200")
        self.assertEqual(pipeline.timestamp(1.9), "00:00:01.900")
        job = private_directory(self.root / "private")
        for name in ("cookies.txt", "cookies.txt.sha256"):
            (job / name).write_text("synthetic", encoding="utf-8")
        pipeline.remove_cookie_copy(job)
        self.assertFalse(list(job.glob("cookies*")))

    def test_external_output_is_bounded(self):
        with self.assertRaises(VideoError) as failure:
            run_external([sys.executable, "-c", "import sys; sys.stderr.write('x'*2000000)"],
                         cwd=self.root, timeout=10)
        self.assertEqual(failure.exception.code, "output_limit")

    @unittest.skipIf(os.name == "nt", "Creating symlinks may require Windows privileges")
    def test_symlink_ancestor_cannot_redirect_private_directory(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        link = self.root / "linked"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(VideoError):
            private_directory(link / "job")
        self.assertFalse((outside / "job").exists())

    def test_resume_rejects_changed_raw_transcript(self):
        self.subtitle_mocks()
        job, _, _ = pipeline.process_job(self.args())
        raw = read_json(job / "transcript.raw.json")
        raw["text"] = "changed"
        atomic_json(job / "transcript.raw.json", raw)
        with self.assertRaises(VideoError) as error:
            pipeline.process_job(pipeline.parser().parse_args(["run", "--resume", str(job)]))
        self.assertEqual(error.exception.code, "integrity")

    def test_job_lock_blocks_concurrent_and_cleanup_retains_original(self):
        self.subtitle_mocks()
        job, _, _ = pipeline.process_job(self.args())
        media = private_directory(job / "media")
        (media / "audio.wav").write_bytes(b"synthetic")
        captions = private_directory(job / "captions")
        (captions / "original.vtt").write_text("WEBVTT", encoding="utf-8")
        with pipeline.lock_job(job), self.assertRaises(VideoError):
            with pipeline.lock_job(job):
                pass
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline.main(["cleanup", str(job), "--confirm"]), 0)
        self.assertFalse(media.exists())
        self.assertTrue((captions / "original.vtt").exists())
        self.assertTrue((job / "transcript.raw.json").exists())

    def test_external_failure_has_explicit_safe_code(self):
        with self.assertRaises(VideoError) as failure:
            run_external([sys.executable, "-c", "import sys; print('private diagnostic',file=sys.stderr); sys.exit(7)"],
                         cwd=self.root, timeout=10)
        self.assertEqual(failure.exception.details["exit_code"], 7)
        self.assertNotIn("private diagnostic", failure.exception.message)


if __name__ == "__main__":
    unittest.main()
