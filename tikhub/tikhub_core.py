"""Standalone TikHub adapters for Douyin, YouTube, and Reddit."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from tikhub_http import TikHubError


_COUNT_RE = re.compile(r"^[0-9]+$")
_DOUYIN_ID_RE = re.compile(r"^[0-9]{6,32}$")
_YOUTUBE_VIDEO_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_CHANNEL_RE = re.compile(r"^UC[A-Za-z0-9_-]{2,}$")
_REDDIT_POST_RE = re.compile(r"^[a-z0-9]{1,17}$")
_REDDIT_FULL_POST_RE = re.compile(r"^t3_[a-z0-9]{1,17}$")
_REDDIT_USER_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")

_DOUYIN_FETCH = "/api/v1/douyin/web/fetch_one_video"
_DOUYIN_FETCH_SHARE = "/api/v1/douyin/web/fetch_one_video_by_share_url"
_DOUYIN_SEARCH = "/api/v1/douyin/search/fetch_general_search_v1"
_DOUYIN_USER_POSTS = "/api/v1/douyin/app/v3/fetch_user_post_videos"
_YOUTUBE_FETCH = "/api/v1/youtube/web_v2/get_video_info_v2"
_YOUTUBE_SEARCH = "/api/v1/youtube/web_v2/get_general_search_v2"
_YOUTUBE_USER_POSTS = "/api/v1/youtube/web_v2/get_channel_videos"
_REDDIT_FETCH = "/api/v1/reddit/app/fetch_post_details"
_REDDIT_SEARCH = "/api/v1/reddit/app/fetch_dynamic_search"
_REDDIT_USER_POSTS = "/api/v1/reddit/app/fetch_user_posts"


def _error(code, message, endpoint=""):
    raise TikHubError(code, message, endpoint)


def _require_count(count):
    if type(count) is not int or not 1 <= count <= 50:
        _error(0, "count must be an integer between 1 and 50.")


def _require_text(value, name):
    if not isinstance(value, str) or not value.strip():
        _error(0, "{0} must be a non-empty string.".format(name))
    return value.strip()


def _is_domain(hostname, root):
    return hostname == root or hostname.endswith("." + root)


def _parse_url(target, platform):
    url = _require_text(target, "target")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        _error(0, "target must be a valid http or https URL.")
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.hostname is None:
        _error(0, "target must be a valid http or https URL.")
    if parts.username is not None or parts.password is not None:
        _error(0, "target URL must not include credentials.")
    if port not in (None, 80, 443):
        _error(0, "target URL must not use a custom port.")
    hostname = parts.hostname.lower()
    allowed = {
        "douyin": _is_domain(hostname, "douyin.com") or _is_domain(hostname, "iesdouyin.com"),
        "youtube": _is_domain(hostname, "youtube.com") or hostname == "youtu.be",
        "reddit": _is_domain(hostname, "reddit.com") or _is_domain(hostname, "redd.it"),
    }
    if not allowed.get(platform):
        _error(0, "target URL hostname is not valid for {0}.".format(platform))
    return parts, hostname


def _json_cursor(state):
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _boolish(value):
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    return None


def _normalize_url(url, fallback):
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    return fallback


def _douyin_content_url(aweme):
    if not isinstance(aweme, dict):
        return None
    share_url = aweme.get("share_url")
    if isinstance(share_url, str) and share_url.startswith(("http://", "https://")):
        return share_url
    share_info = aweme.get("share_info")
    if isinstance(share_info, dict):
        nested = share_info.get("share_url")
        if isinstance(nested, str) and nested.startswith(("http://", "https://")):
            return nested
    aweme_id = aweme.get("aweme_id")
    if isinstance(aweme_id, str) and _DOUYIN_ID_RE.fullmatch(aweme_id):
        return "https://www.douyin.com/video/{0}".format(aweme_id)
    return None


def _normalize_douyin_aweme(aweme):
    if not isinstance(aweme, dict):
        return None
    aweme_id = aweme.get("aweme_id")
    if not isinstance(aweme_id, str) or not _DOUYIN_ID_RE.fullmatch(aweme_id):
        return None
    title = aweme.get("desc")
    if not isinstance(title, str):
        title = aweme.get("caption") if isinstance(aweme.get("caption"), str) else ""
    url = _douyin_content_url(aweme)
    if not url:
        return None
    item = {"id": aweme_id, "title": title, "url": url}
    author = aweme.get("author")
    if isinstance(author, dict) and isinstance(author.get("nickname"), str) and author["nickname"]:
        item["author"] = author["nickname"]
    return item


def _normalize_youtube_video(item):
    if not isinstance(item, dict):
        return None
    video_id = item.get("video_id")
    if not isinstance(video_id, str) or not _YOUTUBE_VIDEO_RE.fullmatch(video_id):
        return None
    title = item.get("title")
    if not isinstance(title, str):
        return None
    url = _normalize_url(item.get("url") or item.get("video_url"),
                         "https://www.youtube.com/watch?v={0}".format(video_id))
    normalized = {"id": video_id, "title": title, "url": url}
    author = item.get("author")
    if isinstance(author, str) and author:
        normalized["author"] = author
    return normalized


def _normalize_youtube_channel(item):
    if not isinstance(item, dict):
        return None
    channel_id = item.get("channel_id", item.get("id"))
    if not isinstance(channel_id, str) or not _YOUTUBE_CHANNEL_RE.fullmatch(channel_id):
        return None
    title = item.get("title")
    if not isinstance(title, str):
        for key in ("name", "author", "channel_name"):
            if isinstance(item.get(key), str):
                title = item[key]
                break
    if not isinstance(title, str):
        return None
    url = _normalize_url(item.get("url"),
                         "https://www.youtube.com/channel/{0}".format(channel_id))
    normalized = {"id": channel_id, "title": title, "url": url}
    author = item.get("author")
    if isinstance(author, str) and author:
        normalized["author"] = author
    return normalized


def _normalize_youtube_playlist(item):
    if not isinstance(item, dict):
        return None
    playlist_id = item.get("playlist_id", item.get("id"))
    if not isinstance(playlist_id, str) or not playlist_id:
        return None
    title = item.get("title")
    if not isinstance(title, str):
        return None
    url = _normalize_url(item.get("url"),
                         "https://www.youtube.com/playlist?list={0}".format(playlist_id))
    normalized = {"id": playlist_id, "title": title, "url": url}
    for key in ("author", "channel_name", "channel_title"):
        if isinstance(item.get(key), str) and item[key]:
            normalized["author"] = item[key]
            break
    return normalized


def _reddit_content_url(item):
    url = item.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    permalink = item.get("permalink")
    if isinstance(permalink, str) and permalink.startswith("/"):
        return "https://www.reddit.com" + permalink
    return None


def _normalize_reddit_post(item):
    if not isinstance(item, dict):
        return None
    post_id = item.get("id")
    if not isinstance(post_id, str) or not _REDDIT_FULL_POST_RE.fullmatch(post_id):
        return None
    title = item.get("postTitle", item.get("title"))
    if not isinstance(title, str):
        return None
    url = _reddit_content_url(item)
    if not url:
        return None
    normalized = {"id": post_id, "title": title, "url": url}
    author_info = item.get("authorInfo")
    if isinstance(author_info, dict) and isinstance(author_info.get("name"), str) and author_info["name"]:
        normalized["author"] = author_info["name"]
    elif isinstance(item.get("author"), str) and item["author"]:
        normalized["author"] = item["author"]
    return normalized


def _douyin_fetch_request(target):
    parts, hostname = _parse_url(target, "douyin")
    path = parts.path or "/"
    if hostname == "v.douyin.com":
        pieces = [piece for piece in path.split("/") if piece]
        if not pieces:
            _error(0, "Douyin share URL is missing its token.", _DOUYIN_FETCH_SHARE)
        return _DOUYIN_FETCH_SHARE, {"share_url": urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))}, None
    pieces = [piece for piece in path.split("/") if piece]
    aweme_id = None
    if len(pieces) >= 2 and pieces[0] in {"video", "note"} and _DOUYIN_ID_RE.fullmatch(pieces[1]):
        aweme_id = pieces[1]
    elif len(pieces) >= 3 and pieces[0] == "share" and pieces[1] == "video" and _DOUYIN_ID_RE.fullmatch(pieces[2]):
        aweme_id = pieces[2]
    if aweme_id is None:
        _error(0, "Unsupported Douyin URL.", _DOUYIN_FETCH)
    return _DOUYIN_FETCH, {"aweme_id": aweme_id}, None


def _youtube_video_id_from_url(target):
    parts, hostname = _parse_url(target, "youtube")
    if hostname == "youtu.be":
        candidate = parts.path.lstrip("/").split("/", 1)[0]
        if _YOUTUBE_VIDEO_RE.fullmatch(candidate):
            return candidate
        _error(0, "Unsupported youtu.be URL.", _YOUTUBE_FETCH)
    pieces = [piece for piece in parts.path.split("/") if piece]
    if parts.path == "/watch":
        candidate = parse_qs(parts.query).get("v", [""])[0]
    elif len(pieces) >= 2 and pieces[0] in {"shorts", "embed", "live"}:
        candidate = pieces[1]
    else:
        candidate = ""
    if not _YOUTUBE_VIDEO_RE.fullmatch(candidate):
        _error(0, "Unsupported YouTube URL.", _YOUTUBE_FETCH)
    return candidate


def _reddit_post_id_from_url(target):
    parts, hostname = _parse_url(target, "reddit")
    if hostname in {"i.redd.it", "v.redd.it"}:
        _error(0, "Reddit asset URLs are not supported.", _REDDIT_FETCH)
    pieces = [piece for piece in parts.path.split("/") if piece]
    bare_id = None
    if hostname == "redd.it" and len(pieces) >= 1:
        bare_id = pieces[0]
    elif "comments" in pieces:
        index = pieces.index("comments")
        if index + 1 < len(pieces):
            bare_id = pieces[index + 1]
    if not isinstance(bare_id, str) or not _REDDIT_POST_RE.fullmatch(bare_id):
        _error(0, "Unsupported Reddit post URL.", _REDDIT_FETCH)
    return "t3_" + bare_id


def _douyin_status_data(response, endpoint):
    data = response.get("data")
    if not isinstance(data, dict):
        _error("invalid_response", "Response data must be an object.", endpoint)
    if data.get("status_code") != 0:
        _error("invalid_response", "Douyin response status_code must be 0.", endpoint)
    if "filter_detail" in data and data.get("filter_detail") is not None:
        _error("content_unavailable", "Douyin content is unavailable.", endpoint)
    return data


def _douyin_search_cursor(cursor):
    if cursor is None:
        return {"cursor": 0, "search_id": "", "backtrace": ""}
    if not isinstance(cursor, str) or not cursor:
        _error(0, "Douyin search cursor must be a JSON string.", _DOUYIN_SEARCH)
    try:
        state = json.loads(cursor)
    except json.JSONDecodeError:
        _error(0, "Douyin search cursor is not valid JSON.", _DOUYIN_SEARCH)
    if not isinstance(state, dict):
        _error(0, "Douyin search cursor must decode to an object.", _DOUYIN_SEARCH)
    raw_cursor = state.get("cursor")
    search_id = state.get("search_id")
    backtrace = state.get("backtrace")
    if type(raw_cursor) is not int or raw_cursor < 0:
        _error(0, "Douyin search cursor.cursor must be a non-negative integer.", _DOUYIN_SEARCH)
    if not isinstance(search_id, str) or not search_id:
        _error(0, "Douyin search cursor.search_id must be a non-empty string.", _DOUYIN_SEARCH)
    if not isinstance(backtrace, str):
        _error(0, "Douyin search cursor.backtrace must be a string.", _DOUYIN_SEARCH)
    return {"cursor": raw_cursor, "search_id": search_id, "backtrace": backtrace}


def _reddit_search_posts(node):
    candidates = []
    if isinstance(node, dict):
        candidates.append(node)
        for key in ("post", "data", "node"):
            value = node.get(key)
            if isinstance(value, dict):
                candidates.append(value)
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    candidates.append(child)
                    for key in ("post", "data", "node"):
                        value = child.get(key)
                        if isinstance(value, dict):
                            candidates.append(value)
                    nested = child.get("children")
                    if isinstance(nested, list):
                        for grandchild in nested:
                            if isinstance(grandchild, dict):
                                candidates.append(grandchild)
                                for key in ("post", "data", "node"):
                                    value = grandchild.get(key)
                                    if isinstance(value, dict):
                                        candidates.append(value)
    output = []
    seen = set()
    for candidate in candidates:
        normalized = _normalize_reddit_post(candidate)
        if normalized is not None and normalized["id"] not in seen:
            seen.add(normalized["id"])
            output.append(normalized)
    return output


def _pagination(has_more, next_cursor):
    return {"has_more": has_more, "next_cursor": next_cursor}


def _run(client, platform, operation, target, *, count=10, cursor=None):
    _require_count(count)
    if platform not in {"douyin", "youtube", "reddit"}:
        _error(0, "Unsupported platform.")
    if operation == "user_posts":
        operation = "user-posts"
    if operation not in {"fetch", "search", "user-posts"}:
        _error(0, "Unsupported operation.")

    if platform == "douyin":
        if operation == "fetch":
            endpoint, params, body = _douyin_fetch_request(target)
            response = client.request("GET", endpoint, params=params, body=body)
            data = _douyin_status_data(response, endpoint)
            aweme = data.get("aweme_detail")
            if not isinstance(aweme, dict):
                _error("content_unavailable", "Douyin fetch returned no video content.", endpoint)
            item = _normalize_douyin_aweme(aweme)
            if item is None or (not item["title"].strip()
                                and not any(aweme.get(key) for key in ("video", "images", "image_post_info"))):
                _error("content_unavailable", "Douyin fetch returned incomplete video content.", endpoint)
            if "aweme_id" in params and item["id"] != params["aweme_id"]:
                _error("invalid_response", "Douyin returned a different video identity.", endpoint)
            return {"response": response, "items": [item], "pagination": _pagination(None, None), "endpoint": endpoint}

        if operation == "search":
            keyword = _require_text(target, "target")
            state = _douyin_search_cursor(cursor)
            body = {
                "keyword": keyword,
                "cursor": state["cursor"],
                "search_id": state["search_id"],
                "backtrace": state["backtrace"],
            }
            response = client.request("POST", _DOUYIN_SEARCH, body=body)
            data = _douyin_status_data(response, _DOUYIN_SEARCH)
            cards = data.get("data")
            if not isinstance(cards, list):
                _error("invalid_response", "Douyin search data.data must be a list.", _DOUYIN_SEARCH)
            items = []
            for card in cards:
                if not isinstance(card, dict):
                    _error("invalid_response", "Douyin search items must be objects.", _DOUYIN_SEARCH)
                aweme = card.get("aweme_info") if isinstance(card.get("aweme_info"), dict) else card
                normalized = _normalize_douyin_aweme(aweme)
                if normalized is not None:
                    items.append(normalized)
            if cards and not items:
                _error("invalid_response", "Douyin returned cards but no recognized content items.", _DOUYIN_SEARCH)
            has_more = _boolish(data.get("has_more"))
            if has_more is None:
                _error("invalid_response", "Douyin search has_more must be a boolean or 0/1.", _DOUYIN_SEARCH)
            next_cursor = None
            if has_more:
                next_value = data.get("cursor")
                backtrace = data.get("backtrace")
                search_id = None
                extra = data.get("extra")
                if isinstance(extra, dict) and isinstance(extra.get("search_request_id"), str) and extra["search_request_id"]:
                    search_id = extra["search_request_id"]
                if not search_id:
                    log_pb = data.get("log_pb")
                    if isinstance(log_pb, dict) and isinstance(log_pb.get("impr_id"), str):
                        search_id = log_pb["impr_id"]
                if type(next_value) is int and next_value >= 0 and isinstance(backtrace, str) and search_id:
                    next_cursor = _json_cursor({
                        "backtrace": backtrace,
                        "cursor": next_value,
                        "search_id": search_id,
                    })
            return {"response": response, "items": items, "pagination": _pagination(has_more, next_cursor), "endpoint": _DOUYIN_SEARCH}

        user_id = _require_text(target, "target")
        max_cursor = 0
        if cursor is not None:
            if not isinstance(cursor, str) or not _COUNT_RE.fullmatch(cursor):
                _error(0, "Douyin user-posts cursor must be a decimal string.", _DOUYIN_USER_POSTS)
            max_cursor = int(cursor)
        params = {"sec_user_id": user_id, "count": min(count, 20), "max_cursor": max_cursor}
        response = client.request("GET", _DOUYIN_USER_POSTS, params=params)
        data = _douyin_status_data(response, _DOUYIN_USER_POSTS)
        aweme_list = data.get("aweme_list")
        if not isinstance(aweme_list, list):
            _error("invalid_response", "Douyin user-posts data.aweme_list must be a list.", _DOUYIN_USER_POSTS)
        items = []
        for aweme in aweme_list:
            normalized = _normalize_douyin_aweme(aweme)
            if normalized is None:
                _error("invalid_response", "Douyin user-posts contains an invalid aweme.", _DOUYIN_USER_POSTS)
            items.append(normalized)
        has_more = _boolish(data.get("has_more"))
        if has_more is None:
            _error("invalid_response", "Douyin user-posts has_more must be a boolean or 0/1.", _DOUYIN_USER_POSTS)
        next_cursor = None
        if has_more and type(data.get("max_cursor")) is int and data["max_cursor"] >= 0:
            next_cursor = str(data["max_cursor"])
        return {"response": response, "items": items, "pagination": _pagination(has_more, next_cursor), "endpoint": _DOUYIN_USER_POSTS}

    if platform == "youtube":
        if operation == "fetch":
            video_id = _youtube_video_id_from_url(target)
            params = {"video_id": video_id, "need_format": "true"}
            response = client.request("GET", _YOUTUBE_FETCH, params=params)
            data = response.get("data")
            if not isinstance(data, dict):
                _error("invalid_response", "YouTube fetch data must be an object.", _YOUTUBE_FETCH)
            item = _normalize_youtube_video(data)
            if item is None or not item["title"].strip():
                _error("content_unavailable", "YouTube fetch returned no usable video content.", _YOUTUBE_FETCH)
            if item["id"] != video_id:
                _error("invalid_response", "YouTube returned a different video identity.", _YOUTUBE_FETCH)
            return {"response": response, "items": [item], "pagination": _pagination(None, None), "endpoint": _YOUTUBE_FETCH}

        if operation == "search":
            keyword = _require_text(target, "target")
            params = {"keyword": keyword}
            if cursor is not None:
                if not isinstance(cursor, str) or not cursor:
                    _error(0, "YouTube search cursor must be a non-empty string.", _YOUTUBE_SEARCH)
                params["continuation_token"] = cursor
            response = client.request("GET", _YOUTUBE_SEARCH, params=params)
            data = response.get("data")
            if not isinstance(data, dict):
                _error("invalid_response", "YouTube search data must be an object.", _YOUTUBE_SEARCH)
            if not any(key in data for key in ("videos", "shorts", "channels", "playlists")):
                _error("invalid_response", "YouTube search is missing its result collections.", _YOUTUBE_SEARCH)
            items = []
            for key, normalizer in (
                ("videos", _normalize_youtube_video),
                ("shorts", _normalize_youtube_video),
                ("channels", _normalize_youtube_channel),
                ("playlists", _normalize_youtube_playlist),
            ):
                collection = data.get(key, [])
                if not isinstance(collection, list):
                    _error("invalid_response", "YouTube search {0} must be a list.".format(key), _YOUTUBE_SEARCH)
                for entry in collection:
                    normalized = normalizer(entry)
                    if normalized is None:
                        _error("invalid_response", "YouTube search contains an invalid {0} item.".format(key[:-1]), _YOUTUBE_SEARCH)
                    items.append(normalized)
            next_cursor = data.get("continuation_token") if isinstance(data.get("continuation_token"), str) and data.get("continuation_token") else None
            return {"response": response, "items": items, "pagination": _pagination(bool(next_cursor), next_cursor), "endpoint": _YOUTUBE_SEARCH}

        channel_id = _require_text(target, "target")
        if not _YOUTUBE_CHANNEL_RE.fullmatch(channel_id):
            _error(0, "YouTube user-posts target must be a UC channel ID.", _YOUTUBE_USER_POSTS)
        params = {"channel_id": channel_id, "need_format": "true"}
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor:
                _error(0, "YouTube user-posts cursor must be a non-empty string.", _YOUTUBE_USER_POSTS)
            params["continuation_token"] = cursor
        response = client.request("GET", _YOUTUBE_USER_POSTS, params=params)
        data = response.get("data")
        if not isinstance(data, dict):
            _error("invalid_response", "YouTube user-posts data must be an object.", _YOUTUBE_USER_POSTS)
        videos = data.get("videos")
        if not isinstance(videos, list):
            _error("invalid_response", "YouTube user-posts data.videos must be a list.", _YOUTUBE_USER_POSTS)
        channel = data.get("channel")
        channel_name = channel.get("name") if isinstance(channel, dict) and isinstance(channel.get("name"), str) else None
        items = []
        for video in videos:
            normalized = _normalize_youtube_video(video)
            if normalized is None:
                _error("invalid_response", "YouTube user-posts contains an invalid video item.", _YOUTUBE_USER_POSTS)
            if "author" not in normalized and channel_name:
                normalized["author"] = channel_name
            items.append(normalized)
        next_cursor = data.get("continuation_token") if isinstance(data.get("continuation_token"), str) and data.get("continuation_token") else None
        return {"response": response, "items": items, "pagination": _pagination(bool(next_cursor), next_cursor), "endpoint": _YOUTUBE_USER_POSTS}

    if operation == "fetch":
        post_id = _reddit_post_id_from_url(target)
        params = {"post_id": post_id, "need_format": "true"}
        response = client.request("GET", _REDDIT_FETCH, params=params)
        data = response.get("data")
        if not isinstance(data, dict):
            _error("invalid_response", "Reddit fetch data must be an object.", _REDDIT_FETCH)
        posts = data.get("postsInfoByIds")
        if not isinstance(posts, list):
            _error("invalid_response", "Reddit fetch data.postsInfoByIds must be a list.", _REDDIT_FETCH)
        items = []
        for post in posts:
            normalized = _normalize_reddit_post(post)
            if normalized is None:
                _error("invalid_response", "Reddit fetch contains an invalid post.", _REDDIT_FETCH)
            items.append(normalized)
        if not items:
            _error("content_unavailable", "Reddit fetch returned no post content.", _REDDIT_FETCH)
        if len(items) != 1 or items[0]["id"] != post_id:
            _error("invalid_response", "Reddit returned a different post identity.", _REDDIT_FETCH)
        return {"response": response, "items": items, "pagination": _pagination(None, None), "endpoint": _REDDIT_FETCH}

    if operation == "search":
        query = _require_text(target, "target")
        params = {
            "query": query,
            "safe_search": "strict",
            "allow_nsfw": "0",
            "need_format": "true",
        }
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor:
                _error(0, "Reddit search cursor must be a non-empty string.", _REDDIT_SEARCH)
            params["after"] = cursor
        response = client.request("GET", _REDDIT_SEARCH, params=params)
        data = response.get("data")
        if not isinstance(data, dict):
            _error("invalid_response", "Reddit search data must be an object.", _REDDIT_SEARCH)
        components = data.get("search")
        if not isinstance(components, dict):
            _error("invalid_response", "Reddit search is missing search data.", _REDDIT_SEARCH)
        dynamic = components.get("dynamic")
        if not isinstance(dynamic, dict):
            _error("invalid_response", "Reddit search is missing dynamic data.", _REDDIT_SEARCH)
        wrapper = dynamic.get("components")
        if not isinstance(wrapper, dict):
            _error("invalid_response", "Reddit search is missing components.", _REDDIT_SEARCH)
        main = wrapper.get("main")
        if not isinstance(main, dict):
            _error("invalid_response", "Reddit search is missing main results.", _REDDIT_SEARCH)
        edges = main.get("edges")
        page_info = main.get("pageInfo")
        if not isinstance(edges, list) or not isinstance(page_info, dict):
            _error("invalid_response", "Reddit search main results are malformed.", _REDDIT_SEARCH)
        items = []
        for edge in edges:
            if not isinstance(edge, dict):
                _error("invalid_response", "Reddit search edges must be objects.", _REDDIT_SEARCH)
            items.extend(_reddit_search_posts(edge.get("node")))
        if edges and not items:
            _error("invalid_response", "Reddit search shape is not a known post result wrapper.", _REDDIT_SEARCH)
        has_more = page_info.get("hasNextPage")
        if type(has_more) is not bool:
            _error("invalid_response", "Reddit search pageInfo.hasNextPage must be boolean.", _REDDIT_SEARCH)
        end_cursor = page_info.get("endCursor")
        if has_more and (not isinstance(end_cursor, str) or not end_cursor):
            _error("invalid_response", "Reddit search pageInfo.endCursor is missing.", _REDDIT_SEARCH)
        return {"response": response, "items": items, "pagination": _pagination(has_more, end_cursor if has_more else None), "endpoint": _REDDIT_SEARCH}

    username = _require_text(target, "target")
    if not _REDDIT_USER_RE.fullmatch(username):
        _error(0, "Reddit user-posts target must be a bare username.", _REDDIT_USER_POSTS)
    params = {"username": username, "need_format": "true"}
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor:
            _error(0, "Reddit user-posts cursor must be a non-empty string.", _REDDIT_USER_POSTS)
        params["after"] = cursor
    response = client.request("GET", _REDDIT_USER_POSTS, params=params)
    data = response.get("data")
    if not isinstance(data, dict):
        _error("invalid_response", "Reddit user-posts data must be an object.", _REDDIT_USER_POSTS)
    post_feed = data.get("postFeed")
    if not isinstance(post_feed, dict):
        _error("invalid_response", "Reddit user-posts is missing postFeed.", _REDDIT_USER_POSTS)
    elements = post_feed.get("elements")
    if not isinstance(elements, dict):
        _error("invalid_response", "Reddit user-posts is missing postFeed.elements.", _REDDIT_USER_POSTS)
    edges = elements.get("edges")
    page_info = elements.get("pageInfo")
    if not isinstance(edges, list) or not isinstance(page_info, dict):
        _error("invalid_response", "Reddit user-posts elements are malformed.", _REDDIT_USER_POSTS)
    items = []
    for edge in edges:
        if not isinstance(edge, dict) or not isinstance(edge.get("node"), dict):
            _error("invalid_response", "Reddit user-posts edges must contain node objects.", _REDDIT_USER_POSTS)
        normalized = _normalize_reddit_post(edge["node"])
        if normalized is None:
            _error("invalid_response", "Reddit user-posts contains an invalid post.", _REDDIT_USER_POSTS)
        items.append(normalized)
    has_more = page_info.get("hasNextPage")
    if type(has_more) is not bool:
        _error("invalid_response", "Reddit user-posts pageInfo.hasNextPage must be boolean.", _REDDIT_USER_POSTS)
    end_cursor = page_info.get("endCursor")
    if has_more and (not isinstance(end_cursor, str) or not end_cursor):
        _error("invalid_response", "Reddit user-posts pageInfo.endCursor is missing.", _REDDIT_USER_POSTS)
    return {"response": response, "items": items, "pagination": _pagination(has_more, end_cursor if has_more else None), "endpoint": _REDDIT_USER_POSTS}


def run(client, platform, operation, target, *, count=10, cursor=None):
    result = _run(client, platform, operation, target, count=count, cursor=cursor)
    data = result["response"]["data"]
    raw_count = len(result["items"])
    if platform == "douyin" and operation == "search":
        raw_count = len(data["data"])
    elif platform == "youtube" and operation == "search":
        raw_count = sum(len(data.get(key, [])) for key in ("videos", "shorts", "channels", "playlists"))
    result["raw_page_item_count"] = raw_count
    return result
