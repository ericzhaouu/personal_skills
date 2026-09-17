"""Standalone TikHub migration adapters for XHS, WeChat MP, and LinkedIn."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit

from tikhub_http import TikHubError


_XHS_FETCH = "/api/v1/xiaohongshu/app_v2/get_image_note_detail"
_XHS_SEARCH = "/api/v1/xiaohongshu/app_v2/search_notes"
_XHS_USER_POSTS = "/api/v1/xiaohongshu/app_v2/get_user_posted_notes"
_WECHAT_FETCH = "/api/v1/wechat_mp/v2/fetch_article_detail_h5"
_WECHAT_SEARCH = "/api/v1/wechat_search/v2/fetch_search"
_WECHAT_USER_POSTS = "/api/v1/wechat_mp/v2/fetch_account_articles"
_LINKEDIN_FETCH = "/api/v1/linkedin/web_v2/get_user_profile"
_LINKEDIN_USER_POSTS = "/api/v1/linkedin/web_v2/get_user_posts"

_XHS_NOTE_ID_RE = re.compile(r"^[0-9a-f]{24}$", re.IGNORECASE)
_WECHAT_USERNAME_RE = re.compile(r"^[0-9A-Za-z][-_0-9A-Za-z]{1,63}(@[0-9A-Za-z]{1,16})?$")


def _error(code, message, endpoint=""):
    raise TikHubError(code, message, endpoint)


def _require_text(value, name, endpoint=""):
    if not isinstance(value, str) or not value.strip():
        _error(0, "{0} must be a non-empty string.".format(name), endpoint)
    return value.strip()


def _require_count(count):
    if type(count) is not int or not 1 <= count <= 50:
        _error(0, "count must be an integer between 1 and 50.")


def _is_domain(hostname, root):
    return hostname == root or hostname.endswith("." + root)


def _parse_url(target, endpoint, roots, *, allow_http=True, require_https=False):
    url = _require_text(target, "target", endpoint)
    try:
        parts = urlsplit(url)
    except ValueError:
        _error(0, "target must be a valid URL.", endpoint)
    if not parts.scheme or not parts.netloc or parts.hostname is None:
        _error(0, "target must be a valid URL.", endpoint)
    scheme = parts.scheme.lower()
    if require_https:
        if scheme != "https":
            _error(0, "target URL must use https.", endpoint)
    elif allow_http:
        if scheme not in {"http", "https"}:
            _error(0, "target URL must use http or https.", endpoint)
    elif scheme != "https":
        _error(0, "target URL must use https.", endpoint)
    if parts.username is not None or parts.password is not None:
        _error(0, "target URL must not include credentials.", endpoint)
    try:
        port = parts.port
    except ValueError:
        _error(0, "target URL port is invalid.", endpoint)
    if port is not None:
        _error(0, "target URL must not include a custom port.", endpoint)
    hostname = parts.hostname.lower()
    if not any(_is_domain(hostname, root) for root in roots):
        _error(0, "target URL hostname is not valid.", endpoint)
    return parts, hostname


def _json_cursor(state):
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decode_cursor(cursor, endpoint, fields):
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor:
        _error(0, "cursor must be a non-empty JSON string.", endpoint)
    try:
        state = json.loads(cursor)
    except json.JSONDecodeError:
        _error(0, "cursor is not valid JSON.", endpoint)
    if not isinstance(state, dict):
        _error(0, "cursor must decode to an object.", endpoint)
    for key, kind in fields.items():
        value = state.get(key)
        if kind == "int":
            if type(value) is not int or value < 0:
                _error(0, "cursor.{0} must be a non-negative integer.".format(key), endpoint)
        elif kind == "page":
            if type(value) is not int or value < 1:
                _error(0, "cursor.{0} must be an integer >= 1.".format(key), endpoint)
        elif kind == "str":
            if not isinstance(value, str) or not value:
                _error(0, "cursor.{0} must be a non-empty string.".format(key), endpoint)
    return state


def _boolish(value):
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    return None


def _identifier(value):
    if type(value) is int:
        return str(value)
    if isinstance(value, str) and value:
        return value
    return None


def _normalize_http_url(value):
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def _require_root_dict(response, endpoint):
    if not isinstance(response, dict):
        _error("invalid_response", "Response must be an object.", endpoint)
    return response


def _require_dict(value, message, endpoint):
    if not isinstance(value, dict):
        _error("invalid_response", message, endpoint)
    return value


def _require_list(value, message, endpoint):
    if not isinstance(value, list):
        _error("invalid_response", message, endpoint)
    return value


def _result(response, items, pagination, endpoint, warnings=None):
    result = {
        "response": response,
        "items": items,
        "pagination": pagination,
        "endpoint": endpoint,
    }
    if warnings:
        result["warnings"] = warnings
    return result


def _pagination(has_more, next_cursor):
    return {"has_more": has_more, "next_cursor": next_cursor}


def _require_xhs_envelope(response, endpoint):
    response = _require_root_dict(response, endpoint)
    outer = _require_dict(response.get("data"), "XHS response data must be an object.", endpoint)
    inner_code = outer.get("code")
    if inner_code != 0:
        _error("upstream_error", "XHS returned an upstream business error.", endpoint)
    success = outer.get("success")
    if success is not True:
        _error("upstream_error", "XHS reported an unsuccessful result.", endpoint)
    return outer


def _xhs_note_url(note):
    share_info = note.get("share_info")
    if isinstance(share_info, dict):
        url = _normalize_http_url(share_info.get("link"))
        if url:
            return url
    note_id = _identifier(note.get("id") if note.get("id") is not None else note.get("note_id"))
    if note_id is None:
        return None
    return "https://www.xiaohongshu.com/explore/{0}".format(note_id)


def _xhs_author(note):
    user = note.get("user")
    if isinstance(user, dict):
        for key in ("nickname", "name", "red_id"):
            value = user.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _xhs_has_content(note):
    if isinstance(note.get("desc"), str) and note["desc"]:
        return True
    images = note.get("images_list")
    if isinstance(images, list) and images:
        return True
    if isinstance(note.get("video_info_v2"), dict):
        return True
    share_info = note.get("share_info")
    if isinstance(share_info, dict) and isinstance(share_info.get("content"), str) and share_info["content"]:
        return True
    return False


def _normalize_xhs_note(note):
    if not isinstance(note, dict):
        return None
    note_id = _identifier(note.get("id") if note.get("id") is not None else note.get("note_id"))
    if note_id is None:
        return None
    title = note.get("title")
    if not isinstance(title, str):
        title = note.get("display_title")
    if not isinstance(title, str):
        title = note.get("desc")
    if not isinstance(title, str):
        return None
    url = _xhs_note_url(note)
    if not url:
        return None
    normalized = {"id": note_id, "title": title, "url": url}
    author = _xhs_author(note)
    if author:
        normalized["author"] = author
    return normalized


def _xhs_is_video(note):
    if not isinstance(note, dict):
        return False
    if isinstance(note.get("video_info_v2"), dict):
        return True
    note_type = note.get("type")
    return isinstance(note_type, str) and note_type.lower() == "video"


def _xhs_fetch_request(target):
    parts, hostname = _parse_url(
        target, _XHS_FETCH, ("xiaohongshu.com", "xhslink.com", "xhslink.cn")
    )
    path = parts.path or "/"
    if _is_domain(hostname, "xiaohongshu.com"):
        pieces = [piece for piece in path.split("/") if piece]
        note_id = None
        if len(pieces) == 2 and pieces[0] in {"explore", "search_result"}:
            note_id = pieces[1]
        elif len(pieces) == 3 and pieces[0] == "discovery" and pieces[1] == "item":
            note_id = pieces[2]
        if isinstance(note_id, str) and _XHS_NOTE_ID_RE.fullmatch(note_id):
            if parts.query:
                share_url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
                return {"share_text": share_url}, note_id
            return {"note_id": note_id}, note_id
    share_url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    return {"share_text": share_url}, None


def _known_xhs_non_note(card):
    if not isinstance(card, dict):
        return False
    for key in (
        "onebox_dsl", "onebox", "hot_query", "query", "query_word", "word",
        "topic_info", "user_card", "ad_info", "ads_info", "live_info",
    ):
        if key in card:
            return True
    card_type = card.get("card_type")
    if isinstance(card_type, str) and card_type.lower() not in {"note", "normal", "video"}:
        return True
    return False


def _xhs_search_state(cursor):
    if cursor is None:
        return {"page": 1, "search_id": "", "search_session_id": ""}
    return _decode_cursor(cursor, _XHS_SEARCH, {
        "page": "page",
        "search_id": "str",
        "search_session_id": "str",
    })


def _wechat_article_url(target):
    parts, _ = _parse_url(target, _WECHAT_FETCH, ("mp.weixin.qq.com",))
    path = parts.path or "/"
    if path != "/s" and not path.startswith("/s/"):
        _error(0, "WeChat article URL must use mp.weixin.qq.com/s.", _WECHAT_FETCH)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _wechat_username(target):
    username = _require_text(target, "target", _WECHAT_USER_POSTS)
    if username.startswith("__biz") or username.lower().startswith("urn:li:"):
        _error(0, "WeChat username must be a real gh_ or custom account ID.", _WECHAT_USER_POSTS)
    if not _WECHAT_USERNAME_RE.fullmatch(username):
        _error(0, "WeChat username format is invalid.", _WECHAT_USER_POSTS)
    return username


def _linkedin_profile_url(target, endpoint, *, allow_slug):
    text = _require_text(target, "target", endpoint)
    if text.lower().startswith("urn:li:"):
        _error(0, "LinkedIn URNs are not supported here.", endpoint)
    if text.isdigit():
        _error(0, "LinkedIn numeric member IDs are not supported here.", endpoint)
    if "://" not in text:
        if not allow_slug:
            _error(0, "LinkedIn fetch requires a full public profile URL.", endpoint)
        if any(ch in text for ch in "/?#@:") or any(ch.isspace() for ch in text):
            _error(0, "LinkedIn public identifiers must be a single profile slug.", endpoint)
        return "https://www.linkedin.com/in/{0}/".format(text)
    parts, _ = _parse_url(target, endpoint, ("linkedin.com",), require_https=True)
    path = parts.path or "/"
    pieces = [piece for piece in path.split("/") if piece]
    if len(pieces) != 2 or pieces[0] != "in":
        _error(0, "LinkedIn target must be a public /in/ profile URL.", endpoint)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _linkedin_requested_slug(url):
    parts, _ = _parse_url(url, _LINKEDIN_FETCH, ("linkedin.com",), require_https=True)
    pieces = [piece for piece in parts.path.split("/") if piece]
    if len(pieces) != 2 or pieces[0] != "in":
        _error("invalid_response", "LinkedIn response URL is not a public profile.", _LINKEDIN_FETCH)
    return pieces[1].lower()


def _linkedin_author(post):
    poster = post.get("poster")
    if isinstance(poster, dict):
        public_id = poster.get("public_id")
        if isinstance(public_id, str) and public_id:
            return public_id
        first = poster.get("first")
        last = poster.get("last")
        if isinstance(first, str) and isinstance(last, str) and (first or last):
            return (first + " " + last).strip()
    return None


def _normalize_wechat_article(item, default_author=None):
    if not isinstance(item, dict):
        return None
    article_id = _identifier(item.get("app_msg_id") if item.get("app_msg_id") is not None else item.get("docID"))
    if article_id is None:
        return None
    if item.get("app_msg_id") is not None and type(item.get("idx")) is int:
        article_id = "{0}:{1}".format(article_id, item["idx"])
    title = item.get("title")
    url = _normalize_http_url(item.get("url") or item.get("doc_url"))
    if not isinstance(title, str) or not url:
        return None
    normalized = {"id": article_id, "title": title, "url": url}
    source = item.get("source")
    if isinstance(source, dict) and isinstance(source.get("title"), str) and source["title"]:
        normalized["author"] = source["title"]
    elif isinstance(default_author, str) and default_author:
        normalized["author"] = default_author
    return normalized


def _normalize_linkedin_post(post):
    if not isinstance(post, dict):
        return None
    post_id = _identifier(post.get("urn") if post.get("urn") is not None else post.get("share_urn"))
    title = post.get("text")
    if not isinstance(title, str) or not title:
        title = post.get("article_title")
    url = _normalize_http_url(post.get("post_url"))
    if post_id is None or not isinstance(title, str) or not title or not url:
        return None
    normalized = {"id": post_id, "title": title, "url": url}
    author = _linkedin_author(post)
    if author:
        normalized["author"] = author
    return normalized


def _linkedin_user_posts_state(cursor):
    if cursor is None:
        return {"start": 0, "pagination_token": None}
    state = _decode_cursor(cursor, _LINKEDIN_USER_POSTS, {"start": "int", "pagination_token": "str"})
    return {"start": state["start"], "pagination_token": state["pagination_token"]}


def _wechat_search_state(cursor):
    if cursor is None:
        return {"offset": 0, "cursor": None}
    state = _decode_cursor(cursor, _WECHAT_SEARCH, {"offset": "int", "cursor": "str"})
    return {"offset": state["offset"], "cursor": state["cursor"]}


def _run(client, platform, operation, target, *, count=10, cursor=None):
    _require_count(count)
    if operation == "user_posts":
        operation = "user-posts"
    platform_key = {
        "xhs": "xhs",
        "xiaohongshu": "xhs",
        "wechat": "wechat_mp",
        "wechat_mp": "wechat_mp",
        "linkedin": "linkedin",
    }.get(platform)
    if platform_key is None:
        _error(0, "Unsupported platform.")
    if operation not in {"fetch", "search", "user-posts"}:
        _error(0, "Unsupported operation.")

    if platform_key == "xhs":
        if operation == "fetch":
            params, requested_note_id = _xhs_fetch_request(target)
            response = client.request("GET", _XHS_FETCH, params=params)
            envelope = _require_xhs_envelope(response, _XHS_FETCH)
            groups = _require_list(envelope.get("data"), "XHS fetch data.data must be a list.", _XHS_FETCH)
            items = []
            warnings = []
            matched = requested_note_id is None
            for group in groups:
                group = _require_dict(group, "XHS fetch list entries must be objects.", _XHS_FETCH)
                notes = _require_list(group.get("note_list"), "XHS fetch note_list must be a list.", _XHS_FETCH)
                for note in notes:
                    normalized = _normalize_xhs_note(note)
                    if normalized is None or not _xhs_has_content(note):
                        continue
                    if requested_note_id is not None:
                        if str(normalized["id"]).lower() != requested_note_id.lower():
                            continue
                        matched = True
                    items.append(normalized)
                    if _xhs_is_video(note):
                        warnings.append(
                            "XHS video fetch returns cover metadata only; playback URLs are not included."
                        )
            if not items:
                _error("invalid_response", "XHS fetch returned no usable note content.", _XHS_FETCH)
            if not matched:
                _error("content_unavailable", "XHS fetch did not return the requested note.", _XHS_FETCH)
            unique_warnings = []
            for warning in warnings:
                if warning not in unique_warnings:
                    unique_warnings.append(warning)
            return _result(response, items, _pagination(None, None), _XHS_FETCH, unique_warnings)

        if operation == "search":
            keyword = _require_text(target, "target", _XHS_SEARCH)
            state = _xhs_search_state(cursor)
            params = {
                "keyword": keyword,
                "page": state["page"],
                "search_id": state["search_id"],
                "search_session_id": state["search_session_id"],
            }
            response = client.request("GET", _XHS_SEARCH, params=params)
            envelope = _require_xhs_envelope(response, _XHS_SEARCH)
            data = _require_dict(envelope.get("data"), "XHS search data.data must be an object.", _XHS_SEARCH)
            cards = _require_list(data.get("items"), "XHS search items must be a list.", _XHS_SEARCH)
            items = []
            for card in cards:
                if not isinstance(card, dict):
                    _error("invalid_response", "XHS search cards must be objects.", _XHS_SEARCH)
                if isinstance(card.get("note"), dict):
                    note = card["note"]
                elif isinstance(card.get("note_card"), dict):
                    note = card["note_card"]
                elif _known_xhs_non_note(card):
                    continue
                else:
                    note = card
                normalized = _normalize_xhs_note(note)
                if normalized is not None:
                    items.append(normalized)
            if cards and not items:
                _error("invalid_response", "XHS search returned cards but none were usable notes.", _XHS_SEARCH)
            current_page = envelope.get("page")
            next_page = envelope.get("next_page")
            if type(current_page) is not int or current_page < 1:
                _error("invalid_response", "XHS search page must be an integer >= 1.", _XHS_SEARCH)
            next_cursor = None
            has_more = None
            if type(next_page) is int:
                search_id = envelope.get("search_id")
                if not isinstance(search_id, str) or not search_id:
                    search_id = state["search_id"]
                search_session_id = envelope.get("search_session_id")
                if not isinstance(search_session_id, str) or not search_session_id:
                    search_session_id = state["search_session_id"]
                if next_page > current_page:
                    has_more = True
                    if search_id and search_session_id:
                        next_cursor = _json_cursor({
                            "page": next_page,
                            "search_id": search_id,
                            "search_session_id": search_session_id,
                        })
                else:
                    has_more = False
            return _result(response, items, _pagination(has_more, next_cursor), _XHS_SEARCH)

        user_id = _require_text(target, "target", _XHS_USER_POSTS)
        params = {"user_id": user_id}
        if cursor is not None:
            params["cursor"] = _require_text(cursor, "cursor", _XHS_USER_POSTS)
        response = client.request("GET", _XHS_USER_POSTS, params=params)
        envelope = _require_xhs_envelope(response, _XHS_USER_POSTS)
        data = _require_dict(envelope.get("data"), "XHS user-posts data.data must be an object.", _XHS_USER_POSTS)
        notes = _require_list(data.get("notes"), "XHS user-posts notes must be a list.", _XHS_USER_POSTS)
        items = []
        for note in notes:
            normalized = _normalize_xhs_note(note)
            if normalized is not None:
                items.append(normalized)
        if notes and not items:
            _error("invalid_response", "XHS user-posts returned notes but none were usable.", _XHS_USER_POSTS)
        has_more = _boolish(data.get("has_more"))
        if has_more is None:
            _error("invalid_response", "XHS user-posts has_more must be a boolean or 0/1.", _XHS_USER_POSTS)
        warnings = []
        next_cursor = None
        if has_more:
            last_cursor = notes[-1].get("cursor") if notes and isinstance(notes[-1], dict) else None
            if isinstance(last_cursor, str) and last_cursor:
                next_cursor = last_cursor
            if next_cursor is None:
                warnings.append("XHS user-posts reported more results but returned no continuation cursor.")
        return _result(response, items, _pagination(has_more, next_cursor), _XHS_USER_POSTS, warnings)

    if platform_key == "wechat_mp":
        if operation == "fetch":
            url = _wechat_article_url(target)
            body = {"url": url, "raw": False}
            response = client.request("POST", _WECHAT_FETCH, body=body)
            response = _require_root_dict(response, _WECHAT_FETCH)
            data = _require_dict(response.get("data"), "WeChat fetch data must be an object.", _WECHAT_FETCH)
            content = _require_dict(data.get("content"), "WeChat fetch data.content must be an object.", _WECHAT_FETCH)
            title = content.get("title")
            content_text = content.get("content_text")
            article_url = _normalize_http_url(data.get("url")) or url
            if not isinstance(title, str) or not title.strip() or not isinstance(content_text, str) or not content_text.strip():
                _error("invalid_response", "WeChat fetch returned incomplete article content.", _WECHAT_FETCH)
            author = content.get("user_name")
            item = {"id": article_url, "title": title, "url": article_url}
            if isinstance(author, str) and author:
                item["author"] = author
            return _result(response, [item], _pagination(None, None), _WECHAT_FETCH)

        if operation == "search":
            keyword = _require_text(target, "target", _WECHAT_SEARCH)
            if len(keyword) > 100:
                _error(0, "WeChat search keyword must be 1-100 characters.", _WECHAT_SEARCH)
            state = _wechat_search_state(cursor)
            body = {
                "keyword": keyword,
                "business_type": "article",
                "raw": False,
                "offset": state["offset"],
            }
            if state["cursor"] is not None:
                body["cursor"] = state["cursor"]
            response = client.request("POST", _WECHAT_SEARCH, body=body)
            response = _require_root_dict(response, _WECHAT_SEARCH)
            data = _require_dict(response.get("data"), "WeChat search data must be an object.", _WECHAT_SEARCH)
            raw_items = _require_list(data.get("items"), "WeChat search items must be a list.", _WECHAT_SEARCH)
            items = []
            for raw_item in raw_items:
                normalized = _normalize_wechat_article(raw_item)
                if normalized is None:
                    _error("invalid_response", "WeChat search contains an invalid article item.", _WECHAT_SEARCH)
                items.append(normalized)
            has_more = _boolish(data.get("continue_flag"))
            if has_more is None:
                _error("invalid_response", "WeChat search continue_flag must be a boolean or 0/1.", _WECHAT_SEARCH)
            warnings = []
            next_cursor = None
            if has_more:
                next_offset = data.get("offset")
                next_token = data.get("cursor")
                if type(next_offset) is int and isinstance(next_token, str) and next_token:
                    next_cursor = _json_cursor({"offset": next_offset, "cursor": next_token})
                else:
                    warnings.append("WeChat search reported more results but returned no usable continuation cursor.")
            return _result(response, items, _pagination(has_more, next_cursor), _WECHAT_SEARCH, warnings)

        username = _wechat_username(target)
        body = {
            "username": username,
            "item_show_type": 0,
            "page_size": min(count, 100),
            "raw": False,
        }
        if cursor is not None:
            body["offset"] = _require_text(cursor, "cursor", _WECHAT_USER_POSTS)
        response = client.request("POST", _WECHAT_USER_POSTS, body=body)
        response = _require_root_dict(response, _WECHAT_USER_POSTS)
        data = _require_dict(response.get("data"), "WeChat user-posts data must be an object.", _WECHAT_USER_POSTS)
        articles = _require_list(data.get("articles"), "WeChat user-posts articles must be a list.", _WECHAT_USER_POSTS)
        items = []
        account_name = data.get("biz_username")
        for article in articles:
            normalized = _normalize_wechat_article(article, account_name)
            if normalized is None:
                _error("invalid_response", "WeChat user-posts contains an invalid article.", _WECHAT_USER_POSTS)
            items.append(normalized)
        is_end = _boolish(data.get("is_end"))
        if is_end is None:
            _error("invalid_response", "WeChat user-posts is_end must be a boolean or 0/1.", _WECHAT_USER_POSTS)
        has_more = not is_end
        warnings = []
        next_cursor = None
        if has_more:
            token = data.get("next_offset")
            if isinstance(token, str) and token:
                next_cursor = token
            else:
                warnings.append("WeChat user-posts reported more results but returned no next_offset.")
        return _result(response, items, _pagination(has_more, next_cursor), _WECHAT_USER_POSTS, warnings)

    if operation == "search":
        _error(0, "LinkedIn people search is unsupported in the supplied schema.", _LINKEDIN_FETCH)

    if operation == "fetch":
        profile_url = _linkedin_profile_url(target, _LINKEDIN_FETCH, allow_slug=False)
        response = client.request("GET", _LINKEDIN_FETCH, params={"url": profile_url})
        response = _require_root_dict(response, _LINKEDIN_FETCH)
        data = _require_dict(response.get("data"), "LinkedIn fetch data must be an object.", _LINKEDIN_FETCH)
        item_id = _identifier(data.get("id"))
        title = data.get("name")
        content_url = _normalize_http_url(data.get("url"))
        if item_id is None or not isinstance(title, str) or not title or not content_url:
            _error("invalid_response", "LinkedIn fetch returned incomplete profile data.", _LINKEDIN_FETCH)
        requested_slug = _linkedin_requested_slug(profile_url)
        returned_slug = _linkedin_requested_slug(content_url)
        if str(item_id).lower() != requested_slug and returned_slug != requested_slug:
            _error("content_unavailable", "LinkedIn fetch did not return the requested profile.", _LINKEDIN_FETCH)
        return _result(
            response,
            [{"id": item_id, "title": title, "url": content_url}],
            _pagination(None, None),
            _LINKEDIN_FETCH,
        )

    profile_url = _linkedin_profile_url(target, _LINKEDIN_USER_POSTS, allow_slug=True)
    state = _linkedin_user_posts_state(cursor)
    params = {"url": profile_url, "type": "posts", "start": state["start"]}
    if isinstance(state["pagination_token"], str) and state["pagination_token"]:
        params["pagination_token"] = state["pagination_token"]
    response = client.request("GET", _LINKEDIN_USER_POSTS, params=params)
    response = _require_root_dict(response, _LINKEDIN_USER_POSTS)
    data = _require_dict(response.get("data"), "LinkedIn user-posts data must be an object.", _LINKEDIN_USER_POSTS)
    posts = _require_list(data.get("data"), "LinkedIn user-posts data.data must be a list.", _LINKEDIN_USER_POSTS)
    paging = _require_dict(data.get("paging"), "LinkedIn user-posts data.paging must be an object.", _LINKEDIN_USER_POSTS)
    items = []
    for post in posts:
        normalized = _normalize_linkedin_post(post)
        if normalized is None:
            _error("invalid_response", "LinkedIn user-posts contains an invalid post.", _LINKEDIN_USER_POSTS)
        items.append(normalized)
    next_cursor = None
    has_more = None
    warnings = []
    token = paging.get("pagination_token")
    if isinstance(token, str) and token:
        has_more = True
        start = paging.get("start")
        count_value = paging.get("count")
        if type(start) is int and start >= 0 and type(count_value) is int and count_value >= 0:
            next_cursor = _json_cursor({"pagination_token": token, "start": start + count_value})
        else:
            warnings.append("LinkedIn user-posts returned a pagination token without usable start/count metadata.")
    elif "pagination_token" in paging:
        has_more = False
    return _result(response, items, _pagination(has_more, next_cursor), _LINKEDIN_USER_POSTS, warnings)


def run(client, platform, operation, target, *, count=10, cursor=None):
    result = _run(client, platform, operation, target, count=count, cursor=cursor)
    raw_count = len(result["items"])
    if platform in ("xhs", "xiaohongshu"):
        data = result["response"]["data"]["data"]
        if operation == "fetch":
            raw_count = sum(len(group["note_list"]) for group in data)
        elif operation == "search":
            raw_count = len(data["items"])
        else:
            raw_count = len(data["notes"])
    result["raw_page_item_count"] = raw_count
    return result
