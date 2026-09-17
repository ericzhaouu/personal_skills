#!/usr/bin/env python3
"""TikHub public-content CLI. Raw API data is preserved; summaries are additive."""

import argparse
import json
import sys
from urllib.parse import urlsplit

import tikhub_core
import tikhub_migration
from tikhub_http import TikHubClient, TikHubError


VERSION = "2.0.0"
PLATFORMS = {
    "douyin": "Douyin",
    "youtube": "YouTube",
    "xiaohongshu": "Xiaohongshu",
    "reddit": "Reddit",
    "wechat_mp": "WeChat Official Accounts",
    "linkedin": "LinkedIn",
}
ALIASES = {
    "抖音": "douyin",
    "小红书": "xiaohongshu",
    "xhs": "xiaohongshu",
    "微信公众号": "wechat_mp",
    "微信": "wechat_mp",
    "wechat": "wechat_mp",
}
DOMAINS = {
    "douyin": ("douyin.com", "iesdouyin.com"),
    "youtube": ("youtube.com", "youtu.be"),
    "xiaohongshu": ("xiaohongshu.com", "xhslink.com", "xhslink.cn"),
    "reddit": ("reddit.com", "redd.it"),
    "wechat_mp": ("mp.weixin.qq.com",),
    "linkedin": ("linkedin.com",),
}
USER_IDS = {
    "douyin": "sec_user_id (MS4w...)",
    "youtube": "channel_id (UC...)",
    "xiaohongshu": "user_id",
    "reddit": "username without u/",
    "wechat_mp": "Actual gh_ identifier or custom WeChat username; not __biz",
    "linkedin": "public profile username",
}


def normalize_platform(value):
    normalized = value.strip().lower()
    return ALIASES.get(normalized, normalized)


def detect_platform(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        raise TikHubError(0, "Invalid content URL.") from None
    if (parsed.scheme not in ("http", "https") or not host
            or parsed.username is not None or parsed.password is not None
            or port not in (None, 80, 443)):
        raise TikHubError(0, "Use a public HTTP(S) platform URL without credentials.")
    for platform, domains in DOMAINS.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return platform
    raise TikHubError(0, "Unsupported content URL hostname.")


def positive_count(value):
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("count must be an integer") from None
    if not 1 <= count <= 50:
        raise argparse.ArgumentTypeError("count must be between 1 and 50")
    return count


def output_options(parser, *, subordinate=False):
    default = argparse.SUPPRESS if subordinate else False
    parser.add_argument("--compact", action="store_true", default=default,
                        help="Compact JSON; accepted before or after the command")
    parser.add_argument("--summary", "-s", action="store_true", default=default,
                        help="Print validated item summaries instead of raw JSON")
    parser.add_argument("--timeout", type=float,
                        default=argparse.SUPPRESS if subordinate else 45,
                        help="Per-request timeout, 30-60 seconds (default: 45)")
    parser.add_argument("--retries", type=int,
                        default=argparse.SUPPRESS if subordinate else 1,
                        help="Transient retries, 0-2 (default: 1); never retry authorization errors")


def build_parser():
    parser = argparse.ArgumentParser(prog="tikhub", description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    output_options(parser)
    commands = parser.add_subparsers(dest="command")
    fetch = commands.add_parser("fetch", help="Fetch public content/profile from a platform URL")
    fetch.add_argument("url")
    output_options(fetch, subordinate=True)
    search = commands.add_parser("search", help="Search public platform content")
    search.add_argument("--platform", "-p", required=True)
    search.add_argument("--keyword", "-k", required=True)
    search.add_argument("--count", "-n", type=positive_count, default=10,
                        help="Maximum summary items; raw API page remains intact (default: 10)")
    search.add_argument("--cursor", "-c",
                        help="Opaque next_cursor from _tikhub.pagination; never invent tokens")
    output_options(search, subordinate=True)
    posts = commands.add_parser("user-posts", help="Get one page of a public account's posts")
    posts.add_argument("--platform", "-p", required=True)
    posts.add_argument("--user-id", "-u", required=True)
    posts.add_argument("--count", "-n", type=positive_count, default=20,
                       help="Maximum summary items; server page hint where supported (default: 20)")
    posts.add_argument("--cursor", "-c", help="Previous _tikhub.pagination.next_cursor")
    output_options(posts, subordinate=True)
    platforms = commands.add_parser("platforms", help="List integrated platforms")
    output_options(platforms, subordinate=True)
    info = commands.add_parser("info", help="Show integration limits and identifier requirements")
    info.add_argument("platform", nargs="?")
    output_options(info, subordinate=True)
    return parser


def platform_info(platform):
    if platform not in PLATFORMS:
        raise TikHubError(0, "Unknown platform. Use the platforms command.")
    return {
        "platform": platform,
        "name": PLATFORMS[platform],
        "operations": ["fetch", "user-posts"] if platform == "linkedin" else ["fetch", "search", "user-posts"],
        "unsupported_operations": ["search (people; absent from current API contract)"] if platform == "linkedin" else [],
        "user_id": USER_IDS[platform],
        "fetch_kind": "public profile" if platform == "linkedin" else "public content",
        "availability": "Provider-dependent; see dated validation matrix in SKILL.md",
        "pagination": "One API page per command. Reuse next_cursor with unchanged query/account.",
        "count": "Limits _tikhub.items and --summary, not the preserved raw API data.",
    }


def emit(value, compact):
    print(json.dumps(value, ensure_ascii=False,
                     separators=(",", ":") if compact else None,
                     indent=None if compact else 2))


def print_summary(result):
    view = result["_tikhub"]
    print("{} {}: {} item(s), {} in API page".format(
        view["platform"], view["operation"], view["result_count"], view["page_item_count"]))
    for index, item in enumerate(view["items"], 1):
        print("{}. {}".format(index, item.get("title") or item["id"]))
        if item.get("url"):
            print("   " + item["url"])
    if view["display_truncated"]:
        print("Summary truncated; raw JSON retains the full page. Next cursor starts after that full page.")
    pagination = view["pagination"]
    if pagination.get("has_more") and not pagination.get("next_cursor"):
        print("More results reported, but no usable continuation was returned.")
    if pagination.get("next_cursor"):
        print("next_cursor: " + pagination["next_cursor"])
    for warning in view.get("warnings", []):
        print("Warning: " + warning)


def execute(args, client=None):
    if args.command in ("info", "platforms"):
        selected = normalize_platform(args.platform) if getattr(args, "platform", None) else None
        value = platform_info(selected) if selected else {
            "version": VERSION,
            "platforms": [platform_info(platform) for platform in PLATFORMS],
            "aliases": ALIASES,
        }
        emit(value, args.compact)
        return 0
    operation = args.command
    if operation == "fetch":
        platform = detect_platform(args.url)
        target = args.url
    else:
        platform = normalize_platform(args.platform)
        if platform not in PLATFORMS:
            raise TikHubError(0, "Unknown platform. Use the platforms command.")
        target = args.keyword if operation == "search" else args.user_id
        if not target.strip():
            raise TikHubError(0, "Keyword or account identifier must not be blank.")
    adapter = tikhub_core if platform in ("douyin", "youtube", "reddit") else tikhub_migration
    client = client if client is not None else TikHubClient(timeout=args.timeout, retries=args.retries)
    count = getattr(args, "count", 1)
    view = adapter.run(client, platform, operation, target, count=count,
                       cursor=getattr(args, "cursor", None))
    response = view["response"]
    items = view["items"]
    if not isinstance(response, dict) or not isinstance(items, list):
        raise TikHubError(0, "Invalid internal operation result.")
    raw_count = view.get("raw_page_item_count")
    if type(raw_count) is not int or raw_count < len(items):
        raise TikHubError(0, "Missing or inconsistent API page cardinality.")
    result = {
        **response,
        "_tikhub": {
            "version": VERSION,
            "platform": platform,
            "operation": operation,
            "endpoint": view["endpoint"],
            "status": "ok" if items else "empty",
            "page_item_count": raw_count,
            "normalized_page_item_count": len(items),
            "result_count": min(len(items), count),
            "display_truncated": raw_count > min(len(items), count),
            "items": items[:count],
            "pagination": view["pagination"],
            "warnings": view.get("warnings", []),
        },
    }
    if args.summary:
        print_summary(result)
    else:
        emit(result, args.compact)
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        return execute(args)
    except TikHubError as error:
        emit(error.payload, args.compact)
        return error.exit_code


if __name__ == "__main__":
    sys.exit(main())
