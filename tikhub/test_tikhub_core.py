import json
import unittest

from tikhub_core import run
from tikhub_http import TikHubError


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, path, params=None, body=None):
        self.calls.append({
            "method": method,
            "path": path,
            "params": params,
            "body": body,
        })
        return self.response


class TikHubCoreTests(unittest.TestCase):
    def test_douyin_fetch_direct_video_url(self):
        raw = {
            "code": 200,
            "data": {
                "status_code": 0,
                "aweme_detail": {
                    "aweme_id": "7372484719365098803",
                    "desc": "hello",
                    "author": {"nickname": "alice"},
                },
            },
        }
        client = FakeClient(raw)
        result = run(client, "douyin", "fetch", "https://www.douyin.com/video/7372484719365098803")
        self.assertIs(result["response"], raw)
        self.assertEqual(result["endpoint"], "/api/v1/douyin/web/fetch_one_video")
        self.assertEqual(result["items"], [{
            "id": "7372484719365098803",
            "title": "hello",
            "url": "https://www.douyin.com/video/7372484719365098803",
            "author": "alice",
        }])
        self.assertEqual(client.calls[0]["params"], {"aweme_id": "7372484719365098803"})

    def test_douyin_fetch_short_share_url_uses_share_endpoint(self):
        raw = {
            "data": {
                "status_code": 0,
                "aweme_detail": {
                    "aweme_id": "7372484719365098803",
                    "desc": "hello",
                    "share_url": "https://www.douyin.com/video/7372484719365098803",
                },
            },
        }
        client = FakeClient(raw)
        run(client, "douyin", "fetch", "https://v.douyin.com/abc123/")
        self.assertEqual(client.calls[0]["path"], "/api/v1/douyin/web/fetch_one_video_by_share_url")
        self.assertEqual(client.calls[0]["params"], {"share_url": "https://v.douyin.com/abc123/"})

    def test_douyin_search_builds_cursor_state_from_search_request_id(self):
        raw = {
            "data": {
                "status_code": 0,
                "data": [{
                    "aweme_info": {
                        "aweme_id": "1234567890123456789",
                        "desc": "clip",
                        "share_url": "https://www.douyin.com/video/1234567890123456789",
                    }
                }],
                "has_more": 1,
                "cursor": 20,
                "backtrace": "bt",
                "extra": {"search_request_id": "srid"},
            },
        }
        client = FakeClient(raw)
        result = run(client, "douyin", "search", "cats")
        self.assertEqual(client.calls[0]["body"], {
            "keyword": "cats",
            "cursor": 0,
            "search_id": "",
            "backtrace": "",
        })
        self.assertEqual(json.loads(result["pagination"]["next_cursor"]), {
            "backtrace": "bt",
            "cursor": 20,
            "search_id": "srid",
        })

    def test_douyin_search_rejects_invalid_cursor_json(self):
        client = FakeClient({})
        with self.assertRaises(TikHubError):
            run(client, "douyin", "search", "cats", cursor="oops")
        self.assertEqual(client.calls, [])

    def test_douyin_search_uses_impression_id_when_search_request_id_empty(self):
        raw = {"data": {"status_code": 0, "data": [], "has_more": 1, "cursor": 20,
                        "backtrace": "opaque-trace", "extra": {"search_request_id": ""},
                        "log_pb": {"impr_id": "actual-search-id"}}}
        result = run(FakeClient(raw), "douyin", "search", "sample")
        state = json.loads(result["pagination"]["next_cursor"])
        self.assertEqual(state["search_id"], "actual-search-id")
        client = FakeClient(raw)
        run(client, "douyin", "search", "sample", cursor=result["pagination"]["next_cursor"])
        self.assertEqual(client.calls[0]["body"]["search_id"], "actual-search-id")
        self.assertEqual(client.calls[0]["body"]["backtrace"], "opaque-trace")

    def test_douyin_user_posts_caps_count_and_maps_pagination(self):
        raw = {
            "data": {
                "status_code": 0,
                "aweme_list": [{
                    "aweme_id": "1234567890123456789",
                    "desc": "clip",
                    "share_url": "https://www.douyin.com/video/1234567890123456789",
                }],
                "has_more": 1,
                "max_cursor": 99,
            },
        }
        client = FakeClient(raw)
        result = run(client, "douyin", "user-posts", "sec_user", count=50, cursor="7")
        self.assertEqual(client.calls[0]["params"], {
            "sec_user_id": "sec_user",
            "count": 20,
            "max_cursor": 7,
        })
        self.assertEqual(result["pagination"], {"has_more": True, "next_cursor": "99"})

    def test_youtube_fetch_supports_shorts_url(self):
        raw = {
            "data": {
                "video_id": "dQw4w9WgXcQ",
                "title": "never gonna",
                "author": "Rick",
                "video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            },
        }
        client = FakeClient(raw)
        result = run(client, "youtube", "fetch", "https://www.youtube.com/shorts/dQw4w9WgXcQ")
        self.assertEqual(client.calls[0]["params"], {"video_id": "dQw4w9WgXcQ", "need_format": "true"})
        self.assertEqual(result["items"][0]["author"], "Rick")

    def test_youtube_search_keeps_general_search_collections(self):
        raw = {
            "data": {
                "videos": [{
                    "video_id": "dQw4w9WgXcQ",
                    "title": "video",
                    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                }],
                "shorts": [{
                    "video_id": "abcdefghijk",
                    "title": "short",
                    "url": "https://www.youtube.com/watch?v=abcdefghijk",
                    "author": "Shorts",
                }],
                "channels": [{
                    "channel_id": "UCabcdefghijk1234567890",
                    "title": "channel",
                    "url": "https://www.youtube.com/channel/UCabcdefghijk1234567890",
                }],
                "playlists": [{
                    "playlist_id": "PL123",
                    "title": "playlist",
                    "url": "https://www.youtube.com/playlist?list=PL123",
                    "author": "Maker",
                }],
                "continuation_token": "next-token",
            },
        }
        client = FakeClient(raw)
        result = run(client, "youtube", "search", "python")
        self.assertEqual([item["id"] for item in result["items"]], [
            "dQw4w9WgXcQ",
            "abcdefghijk",
            "UCabcdefghijk1234567890",
            "PL123",
        ])
        self.assertEqual(result["pagination"], {"has_more": True, "next_cursor": "next-token"})

    def test_youtube_user_posts_validates_channel_id(self):
        client = FakeClient({})
        with self.assertRaises(TikHubError):
            run(client, "youtube", "user-posts", "@handle")
        self.assertEqual(client.calls, [])

    def test_reddit_fetch_supports_redd_it(self):
        raw = {
            "data": {
                "postsInfoByIds": [{
                    "id": "t3_abc123",
                    "postTitle": "post",
                    "permalink": "/r/test/comments/abc123/post/",
                    "authorInfo": {"name": "bob"},
                }],
            },
        }
        client = FakeClient(raw)
        result = run(client, "reddit", "fetch", "https://redd.it/abc123")
        self.assertEqual(client.calls[0]["params"], {"post_id": "t3_abc123", "need_format": "true"})
        self.assertEqual(result["items"][0]["url"], "https://www.reddit.com/r/test/comments/abc123/post/")

    def test_reddit_search_extracts_known_wrapped_post_shape(self):
        raw = {
            "data": {
                "search": {
                    "dynamic": {
                        "components": {
                            "main": {
                                "edges": [{
                                    "node": {
                                        "presentation": {"kind": "post"},
                                        "children": [{
                                            "post": {
                                                "id": "t3_abc123",
                                                "postTitle": "wrapped",
                                                "permalink": "/r/test/comments/abc123/post/",
                                                "authorInfo": {"name": "bob"},
                                            }
                                        }],
                                    }
                                }],
                                "pageInfo": {"hasNextPage": True, "endCursor": "after-token"},
                            }
                        }
                    }
                }
            }
        }
        client = FakeClient(raw)
        result = run(client, "reddit", "search", "python")
        self.assertEqual(result["items"], [{
            "id": "t3_abc123",
            "title": "wrapped",
            "url": "https://www.reddit.com/r/test/comments/abc123/post/",
            "author": "bob",
        }])
        self.assertEqual(result["pagination"], {"has_more": True, "next_cursor": "after-token"})

    def test_reddit_search_unknown_wrapper_is_invalid_response(self):
        raw = {
            "data": {
                "search": {
                    "dynamic": {
                        "components": {
                            "main": {
                                "edges": [{"node": {"children": [{"presentation": {"kind": "filter"}}]}}],
                                "pageInfo": {"hasNextPage": False, "endCursor": ""},
                            }
                        }
                    }
                }
            }
        }
        client = FakeClient(raw)
        with self.assertRaises(TikHubError) as ctx:
            run(client, "reddit", "search", "python")
        self.assertEqual(ctx.exception.payload["code"], "invalid_response")

    def test_reddit_search_returns_all_posts_in_one_component(self):
        posts = [
            {"post": {"id": "t3_abc123", "postTitle": "First",
                      "permalink": "/r/example/comments/abc123/"}},
            {"post": {"id": "t3_def456", "postTitle": "Second",
                      "permalink": "/r/example/comments/def456/"}},
        ]
        raw = {"data": {"search": {"dynamic": {"components": {"main": {
            "edges": [{"node": {"children": posts}}],
            "pageInfo": {"hasNextPage": False, "endCursor": ""},
        }}}}}}
        result = run(FakeClient(raw), "reddit", "search", "public example")
        self.assertEqual([item["id"] for item in result["items"]], ["t3_abc123", "t3_def456"])

    def test_fetch_rejects_mismatched_identity(self):
        raw = {"data": {"video_id": "abcdefghijk", "title": "Wrong video"}}
        with self.assertRaises(TikHubError):
            run(FakeClient(raw), "youtube", "fetch", "https://youtu.be/dQw4w9WgXcQ")

    def test_douyin_metadata_only_is_not_content(self):
        raw = {"data": {"status_code": 0, "aweme_detail": {"aweme_id": "7372484719365098803"}}}
        with self.assertRaises(TikHubError):
            run(FakeClient(raw), "douyin", "fetch", "https://www.douyin.com/video/7372484719365098803")

    def test_reddit_user_posts_maps_edges(self):
        raw = {
            "data": {
                "postFeed": {
                    "elements": {
                        "edges": [{
                            "node": {
                                "id": "t3_abc123",
                                "postTitle": "post",
                                "url": "https://www.reddit.com/r/test/comments/abc123/post/",
                                "authorInfo": {"name": "spez"},
                            }
                        }],
                        "pageInfo": {"hasNextPage": False, "endCursor": "unused"},
                    }
                }
            }
        }
        client = FakeClient(raw)
        result = run(client, "reddit", "user-posts", "spez")
        self.assertEqual(result["items"][0]["author"], "spez")
        self.assertEqual(result["pagination"], {"has_more": False, "next_cursor": None})

    def test_unavailable_content_raises_explicit_error(self):
        raw = {"data": {"status_code": 0, "filter_detail": {"reason": "gone"}}}
        client = FakeClient(raw)
        with self.assertRaises(TikHubError) as ctx:
            run(client, "douyin", "fetch", "https://www.douyin.com/video/7372484719365098803")
        self.assertEqual(ctx.exception.payload["code"], "content_unavailable")


if __name__ == "__main__":
    unittest.main()
