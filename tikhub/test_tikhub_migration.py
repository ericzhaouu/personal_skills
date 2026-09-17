import json
import unittest

from tikhub_http import TikHubError
from tikhub_migration import run


class FakeClient:
    def __init__(self, response=None):
        self.response = response
        self.calls = []

    def request(self, method, path, params=None, body=None):
        self.calls.append({
            "method": method,
            "path": path,
            "params": params,
            "body": body,
        })
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class TikHubMigrationTests(unittest.TestCase):
    def test_xhs_fetch_preserves_direct_url_token_and_warns_on_video(self):
        raw = {
            "code": 200,
            "data": {
                "code": 0,
                "success": True,
                "data": [{
                    "note_list": [{
                        "id": "697c0eee000000000a03c308",
                        "title": "Hello",
                        "desc": "Body",
                        "type": "video",
                        "video_info_v2": {"image": {}},
                        "share_info": {"link": "https://www.xiaohongshu.com/explore/697c0eee000000000a03c308"},
                        "user": {"nickname": "alice"},
                    }]
                }],
            },
        }
        client = FakeClient(raw)
        result = run(
            client,
            "xhs",
            "fetch",
            "https://www.xiaohongshu.com/explore/697c0eee000000000a03c308?xsec_token=abc",
        )
        self.assertIs(result["response"], raw)
        self.assertEqual(client.calls[0], {
            "method": "GET",
            "path": "/api/v1/xiaohongshu/app_v2/get_image_note_detail",
            "params": {"share_text": "https://www.xiaohongshu.com/explore/697c0eee000000000a03c308?xsec_token=abc"},
            "body": None,
        })
        self.assertEqual(result["items"], [{
            "id": "697c0eee000000000a03c308",
            "title": "Hello",
            "url": "https://www.xiaohongshu.com/explore/697c0eee000000000a03c308",
            "author": "alice",
        }])
        self.assertIn("cover metadata only", result["warnings"][0])

    def test_xhs_fetch_short_share_url_preserves_query(self):
        raw = {
            "code": 200,
            "data": {
                "code": 0,
                "success": True,
                "data": [{
                    "note_list": [{
                        "id": "697c0eee000000000a03c308",
                        "title": "Hello",
                        "desc": "Body",
                        "images_list": [{"url": "https://img"}],
                        "share_info": {"link": "https://www.xiaohongshu.com/explore/697c0eee000000000a03c308"},
                    }]
                }],
            },
        }
        client = FakeClient(raw)
        run(client, "xiaohongshu", "fetch", "http://xhslink.com/a/b?xsec_token=xyz")
        self.assertEqual(client.calls[0]["params"], {"share_text": "http://xhslink.com/a/b?xsec_token=xyz"})

    def test_xhs_fetch_rejects_nested_business_error(self):
        client = FakeClient({
            "code": 200,
            "data": {"code": 0, "success": False, "msg": "service error"},
        })
        with self.assertRaises(TikHubError) as ctx:
            run(client, "xhs", "fetch", "http://xhslink.com/a")
        self.assertEqual(ctx.exception.payload["endpoint"], "/api/v1/xiaohongshu/app_v2/get_image_note_detail")

    def test_xhs_search_builds_cursor_and_skips_known_non_note_cards(self):
        raw = {
            "code": 200,
            "data": {
                "code": 0,
                "success": True,
                "data": {
                    "items": [
                        {"note": {
                            "id": "6a53010a000000000702cd2f",
                            "title": "Search hit",
                            "desc": "Text",
                            "images_list": [{"url": "https://img"}],
                            "share_info": {"link": "https://www.xiaohongshu.com/explore/6a53010a000000000702cd2f"},
                            "user": {"nickname": "bob"},
                        }},
                        {"onebox_dsl": {"id": "ignored"}},
                    ],
                },
                "search_id": "sid",
                "search_session_id": "ssid",
                "page": 1,
                "next_page": 2,
            },
        }
        client = FakeClient(raw)
        result = run(client, "xhs", "search", "美食推荐")
        self.assertEqual(client.calls[0]["params"], {
            "keyword": "美食推荐",
            "page": 1,
            "search_id": "",
            "search_session_id": "",
        })
        self.assertEqual(result["items"][0]["id"], "6a53010a000000000702cd2f")
        self.assertEqual(json.loads(result["pagination"]["next_cursor"]), {
            "page": 2,
            "search_id": "sid",
            "search_session_id": "ssid",
        })

    def test_xhs_search_nonempty_invalid_cards_raise(self):
        client = FakeClient({
            "code": 200,
            "data": {
                "code": 0,
                "success": True,
                "data": {"items": [{"unexpected": True}]},
                "search_id": "sid",
                "search_session_id": "ssid",
                "page": 1,
                "next_page": 2,
            },
        })
        with self.assertRaises(TikHubError) as ctx:
            run(client, "xhs", "search", "cats")
        self.assertEqual(ctx.exception.payload["code"], "invalid_response")

    def test_xhs_user_posts_uses_last_note_cursor_and_warning_when_missing(self):
        raw = {
            "code": 200,
            "data": {
                "code": 0,
                "success": True,
                "data": {
                    "notes": [{
                        "id": "68b39115000000001c037dad",
                        "title": "One",
                        "desc": "Body",
                        "cursor": "cursor-1",
                        "images_list": [{"url": "https://img"}],
                        "user": {"nickname": "alice"},
                    }, {
                        "id": "63c8b257000000001f00d554",
                         "title": "Two",
                         "desc": "Body",
                         "cursor": "cursor-2",
                        "images_list": [{"url": "https://img2"}],
                        "user": {"nickname": "alice"},
                    }],
                    "has_more": True,
                },
            },
        }
        client = FakeClient(raw)
        result = run(client, "xhs", "user-posts", "61b46d790000000010008153", cursor="prev")
        self.assertEqual(client.calls[0]["params"], {"user_id": "61b46d790000000010008153", "cursor": "prev"})
        self.assertEqual(result["pagination"], {"has_more": True, "next_cursor": "cursor-2"})

        raw["data"]["data"]["notes"][-1].pop("cursor")
        client = FakeClient(raw)
        result = run(client, "xhs", "user-posts", "61b46d790000000010008153")
        self.assertEqual(result["pagination"], {"has_more": True, "next_cursor": None})
        self.assertIn("continuation cursor", result["warnings"][0])

    def test_wechat_fetch_uses_h5_endpoint_and_requires_content_text(self):
        url = "https://mp.weixin.qq.com/s/TSNQKkRpN1qbKsT7BvzqIw?foo=bar"
        raw = {
            "code": 200,
            "data": {
                "url": url,
                "content": {
                    "title": "Headline",
                    "content_text": "Body",
                    "user_name": "gh_123456789abc",
                },
            },
        }
        client = FakeClient(raw)
        result = run(client, "wechat_mp", "fetch", url)
        self.assertEqual(client.calls[0], {
            "method": "POST",
            "path": "/api/v1/wechat_mp/v2/fetch_article_detail_h5",
            "params": None,
            "body": {"url": url, "raw": False},
        })
        self.assertEqual(result["items"], [{
            "id": url,
            "title": "Headline",
            "url": url,
            "author": "gh_123456789abc",
        }])

        bad = FakeClient({"code": 200, "data": {"url": url, "content": {"title": "Headline"}}})
        with self.assertRaises(TikHubError) as ctx:
            run(bad, "wechat_mp", "fetch", url)
        self.assertEqual(ctx.exception.payload["code"], "invalid_response")

    def test_wechat_search_article_pagination_state(self):
        raw = {
            "code": 200,
            "data": {
                "continue_flag": 1,
                "offset": 38,
                "cursor": "opaque",
                "items": [{
                    "title": "人民日报",
                    "desc": "desc",
                    "docID": "1234567890123456789",
                    "doc_url": "https://mp.weixin.qq.com/s?doc=1",
                    "source": {"title": "人民日报"},
                }],
            },
        }
        client = FakeClient(raw)
        result = run(client, "wechat", "search", "人民日报")
        self.assertEqual(client.calls[0]["body"], {
            "keyword": "人民日报",
            "business_type": "article",
            "raw": False,
            "offset": 0,
        })
        self.assertEqual(result["items"], [{
            "id": "1234567890123456789",
            "title": "人民日报",
            "url": "https://mp.weixin.qq.com/s?doc=1",
            "author": "人民日报",
        }])
        self.assertEqual(json.loads(result["pagination"]["next_cursor"]), {
            "offset": 38,
            "cursor": "opaque",
        })

    def test_wechat_search_empty_list_is_valid(self):
        client = FakeClient({
            "code": 200,
            "data": {
                "continue_flag": 0,
                "offset": 0,
                "cursor": None,
                "items": [],
            },
        })
        result = run(client, "wechat_mp", "search", "人民日报")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["pagination"], {"has_more": False, "next_cursor": None})

    def test_wechat_user_posts_caps_page_size_but_returns_full_page(self):
        raw = {
            "code": 200,
            "data": {
                "biz_username": "gh_363b924965e9",
                "is_end": 0,
                "next_offset": "NEXT",
                "articles": [{
                    "title": "One",
                    "url": "http://mp.weixin.qq.com/s?__biz=a&mid=1&idx=1",
                    "app_msg_id": 1,
                    "idx": 1,
                }, {
                    "title": "Two",
                    "url": "http://mp.weixin.qq.com/s?__biz=a&mid=2&idx=1",
                    "app_msg_id": 2,
                    "idx": 1,
                }],
            },
        }
        client = FakeClient(raw)
        result = run(client, "wechat_mp", "user-posts", "gh_363b924965e9", count=50, cursor="OFFSET")
        self.assertEqual(client.calls[0]["body"], {
            "username": "gh_363b924965e9",
            "item_show_type": 0,
            "page_size": 50,
            "raw": False,
            "offset": "OFFSET",
        })
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["pagination"], {"has_more": True, "next_cursor": "NEXT"})

    def test_wechat_user_posts_rejects_biz_masquerade(self):
        client = FakeClient({})
        with self.assertRaises(TikHubError):
            run(client, "wechat_mp", "user-posts", "__biz=MjM5MjAxNDM4MA==")
        self.assertEqual(client.calls, [])

    def test_linkedin_fetch_validates_profile_identity(self):
        raw = {
            "code": 200,
            "data": {
                "id": "williamhgates",
                "name": "Bill Gates",
                "url": "https://be.linkedin.com/in/Williamhgates",
            },
        }
        client = FakeClient(raw)
        result = run(client, "linkedin", "fetch", "https://www.linkedin.com/in/williamhgates/")
        self.assertEqual(client.calls[0]["params"], {"url": "https://www.linkedin.com/in/williamhgates/"})
        self.assertEqual(result["items"], [{
            "id": "williamhgates",
            "title": "Bill Gates",
            "url": "https://be.linkedin.com/in/Williamhgates",
        }])

    def test_linkedin_user_posts_accepts_slug_and_tracks_start_token(self):
        raw = {
            "code": 200,
            "data": {
                "data": [{
                    "text": "AI gives us a once-in-a-generation opportunity.",
                    "urn": "7505661830481121281",
                    "post_url": "https://www.linkedin.com/posts/example",
                    "posted": "2025-09-17 12:00:00",
                    "poster": {"public_id": "williamhgates"},
                }],
                "paging": {"count": 50, "start": 0, "pagination_token": "TOKEN"},
            },
        }
        client = FakeClient(raw)
        result = run(client, "linkedin", "user-posts", "williamhgates")
        self.assertEqual(client.calls[0]["params"], {
            "url": "https://www.linkedin.com/in/williamhgates/",
            "type": "posts",
            "start": 0,
        })
        self.assertEqual(result["items"], [{
            "id": "7505661830481121281",
            "title": "AI gives us a once-in-a-generation opportunity.",
            "url": "https://www.linkedin.com/posts/example",
            "author": "williamhgates",
        }])
        self.assertEqual(json.loads(result["pagination"]["next_cursor"]), {
            "pagination_token": "TOKEN",
            "start": 50,
        })

    def test_linkedin_user_posts_without_token_is_honest(self):
        client = FakeClient({
            "code": 200,
            "data": {
                "data": [],
                "paging": {"count": 50, "start": 0, "pagination_token": ""},
            },
        })
        result = run(client, "linkedin", "user-posts", "https://www.linkedin.com/in/williamhgates/")
        self.assertEqual(result["pagination"], {"has_more": False, "next_cursor": None})

    def test_linkedin_search_is_unsupported_without_calling_client(self):
        client = FakeClient({})
        with self.assertRaises(TikHubError) as ctx:
            run(client, "linkedin", "search", "Bill Gates")
        self.assertEqual(ctx.exception.payload["code"], 0)
        self.assertEqual(client.calls, [])

    def test_linkedin_user_posts_rejects_urn_and_credential_urls(self):
        client = FakeClient({})
        with self.assertRaises(TikHubError):
            run(client, "linkedin", "user-posts", "urn:li:member:123")
        with self.assertRaises(TikHubError):
            run(client, "linkedin", "fetch", "https://user:pass@www.linkedin.com/in/williamhgates/")
        self.assertEqual(client.calls, [])

    def test_null_shapes_raise_invalid_response(self):
        client = FakeClient({"code": 200, "data": None})
        with self.assertRaises(TikHubError) as ctx:
            run(client, "linkedin", "user-posts", "williamhgates")
        self.assertEqual(ctx.exception.payload["code"], "invalid_response")


if __name__ == "__main__":
    unittest.main()
