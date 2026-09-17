import contextlib
import io
import json
import unittest
from unittest.mock import patch

import tikhub
from tikhub_http import TikHubError


class CliTests(unittest.TestCase):
    def test_flags_work_before_and_after_command(self):
        for argv in (
            ["--compact", "--summary", "search", "-p", "youtube", "-k", "python"],
            ["search", "-p", "youtube", "-k", "python", "--compact", "--summary"],
        ):
            args = tikhub.build_parser().parse_args(argv)
            self.assertTrue(args.compact)
            self.assertTrue(args.summary)

    def test_domain_detection_is_not_query_matching(self):
        for url in (
            "https://example.invalid/?url=youtube.com",
            "https://youtube.com.example.invalid/watch?v=dQw4w9WgXcQ",
            "https://youtube.com@elsewhere.invalid/watch?v=dQw4w9WgXcQ",
            "file:///youtube.com",
        ):
            with self.assertRaises(TikHubError):
                tikhub.detect_platform(url)
        self.assertEqual(tikhub.detect_platform("https://www.youtube.com/shorts/dQw4w9WgXcQ"), "youtube")
        self.assertEqual(tikhub.detect_platform("https://mp.weixin.qq.com/s/public-example"), "wechat_mp")

    def test_count_limits(self):
        for value in ("0", "-1", "51", "abc"):
            with self.assertRaises(tikhub.argparse.ArgumentTypeError):
                tikhub.positive_count(value)
        self.assertEqual(tikhub.positive_count("5"), 5)

    def test_offline_info_never_needs_client(self):
        with patch.object(tikhub, "TikHubClient", side_effect=AssertionError("No network")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(tikhub.main(["platforms", "--compact"]), 0)
        self.assertEqual(len(json.loads(output.getvalue())["platforms"]), 6)

    def test_error_json_and_nonzero_exit_even_with_summary(self):
        with patch.object(tikhub, "execute", side_effect=TikHubError(401, "Unauthorized")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            status = tikhub.main(["fetch", "https://youtu.be/dQw4w9WgXcQ", "--summary"])
        self.assertEqual(status, 1)
        self.assertTrue(json.loads(output.getvalue())["error"])

    def test_raw_data_is_preserved_and_summary_count_is_explicit(self):
        raw = {"code": 200, "data": {"videos": [{"video_id": "example0001"}, {"video_id": "example0002"}]}}
        view = {
            "response": raw,
            "items": [{"id": "example0001", "title": "One", "url": ""},
                      {"id": "example0002", "title": "Two", "url": ""}],
            "pagination": {"has_more": True, "next_cursor": "opaque-next"},
            "endpoint": "/api/v1/youtube/web_v2/get_general_search_v2",
            "raw_page_item_count": 2,
        }
        args = tikhub.build_parser().parse_args(["search", "-p", "youtube", "-k", "python", "-n", "1"])
        with patch.object(tikhub.tikhub_core, "run", return_value=view), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            status = tikhub.execute(args, client=object())
        result = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(result["data"], raw["data"])
        self.assertEqual(result["_tikhub"]["result_count"], 1)
        self.assertEqual(result["_tikhub"]["page_item_count"], 2)
        self.assertTrue(result["_tikhub"]["display_truncated"])
        self.assertNotIn("_tikhub", raw)

    def test_unrepresented_raw_cards_are_marked_truncated(self):
        view = {"response": {"code": 200, "data": {"cards": [{}, {}, {}]}},
                "items": [{"id": "one", "title": "One", "url": ""}],
                "raw_page_item_count": 3, "pagination": {"has_more": True, "next_cursor": "next"},
                "endpoint": "/api/v1/douyin/search/fetch_general_search_v1"}
        args = tikhub.build_parser().parse_args(["search", "-p", "douyin", "-k", "sample"])
        with patch.object(tikhub.tikhub_core, "run", return_value=view), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            tikhub.execute(args, client=object())
        result = json.loads(output.getvalue())["_tikhub"]
        self.assertEqual(result["page_item_count"], 3)
        self.assertEqual(result["normalized_page_item_count"], 1)
        self.assertTrue(result["display_truncated"])


if __name__ == "__main__":
    unittest.main()
