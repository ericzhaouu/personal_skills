#!/usr/bin/env python3
"""Pin and read public GitHub evidence without cloning or executing repository code."""

import argparse
import base64
import binascii
from contextlib import contextmanager
import json
import re
import signal
import sys
import urllib.error
import urllib.parse
import urllib.request


API = "https://api.github.com"
MAX_RESPONSE_BYTES = 800_000
MAX_FILE_BYTES = 160_000
MAX_ENTRIES = 120
MAX_EXCERPT_LINES = 100
MAX_EXCERPT_CHARS = 10_000
SHA = re.compile(r"[0-9a-f]{40}\Z")
OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
PART = re.compile(r"[A-Za-z0-9_.-]+\Z")
CODE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".sh", ".sql", ".proto",
}
DEADLINES = {"resolve": 40, "ls": 25, "inspect": 75, "read": 25}


class EvidenceError(Exception):
    """A safe, actionable failure; never include raw GitHub response bodies."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise EvidenceError("github_redirect_blocked")


OPENER = urllib.request.build_opener(NoRedirect)


@contextmanager
def deadline(seconds):
    if not hasattr(signal, "setitimer"):
        yield
        return

    def expired(_signum, _frame):
        raise EvidenceError("github_timeout")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def repository(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "github.com"
            or parsed.netloc != "github.com" or parsed.query or parsed.fragment):
        raise EvidenceError("expected_https_github_repository_url")
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2 or not OWNER.fullmatch(parts[0]):
        raise EvidenceError("expected_owner_and_repository")
    name = parts[1].removesuffix(".git")
    if not REPO.fullmatch(name) or name in {".", ".."}:
        raise EvidenceError("invalid_repository_name")
    return parts[0], name


def relative_path(path):
    if (not isinstance(path, str) or not path or len(path) > 300
            or path.startswith("/") or "\\" in path
            or any(not PART.fullmatch(piece) or piece in {".", ".."}
                   for piece in path.split("/"))):
        raise EvidenceError("invalid_repository_path")
    return path


def api_get(path):
    request = urllib.request.Request(
        API + path,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "baymax-repo-evidence/1",
        },
    )
    try:
        with OPENER.open(request, timeout=15) as response:
            if urllib.parse.urlsplit(response.geturl()).netloc != "api.github.com":
                raise EvidenceError("github_redirect_blocked")
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
            raise EvidenceError("github_rate_limited") from None
        raise EvidenceError(f"github_http_{exc.code}") from None
    except (urllib.error.URLError, TimeoutError):
        raise EvidenceError("github_unavailable") from None
    if len(payload) > MAX_RESPONSE_BYTES:
        raise EvidenceError("github_response_too_large")
    try:
        return json.loads(payload)
    except (ValueError, UnicodeError):
        raise EvidenceError("invalid_github_response") from None


def _repo_path(owner, name):
    return f"/repos/{owner}/{name}"


def resolve(url, ref=None, getter=api_get):
    owner, name = repository(url)
    root = _repo_path(owner, name)
    metadata = getter(root)
    if not isinstance(metadata, dict):
        raise EvidenceError("invalid_repository_metadata")
    full_name = metadata.get("full_name")
    default_branch = metadata.get("default_branch")
    if (not isinstance(full_name, str)
            or full_name.lower() != f"{owner}/{name}".lower()
            or not isinstance(default_branch, str) or not default_branch):
        raise EvidenceError("repository_identity_unverified")
    canonical_owner, canonical_name = full_name.split("/")
    root = _repo_path(canonical_owner, canonical_name)
    selected_ref = ref if ref is not None else default_branch
    if (not isinstance(selected_ref, str) or not selected_ref
            or len(selected_ref) > 150
            or any(ord(char) < 33 or ord(char) == 127 for char in selected_ref)):
        raise EvidenceError("invalid_git_ref")
    commit = getter(root + "/commits/" + urllib.parse.quote(selected_ref, safe=""))
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise EvidenceError("commit_unverified")
    tree = getter(root + "/git/trees/" + sha)
    entries = tree.get("tree") if isinstance(tree, dict) else None
    if not isinstance(entries, list):
        raise EvidenceError("root_tree_unavailable")
    visible = [
        {"path": item["path"], "type": item["type"]}
        for item in entries
        if isinstance(item, dict) and isinstance(item.get("path"), str)
        and item.get("type") in {"blob", "tree"}
    ]
    return {
        "repository": f"https://github.com/{full_name}",
        "owner": canonical_owner,
        "name": canonical_name,
        "defaultBranch": default_branch,
        "selectedRef": selected_ref,
        "commitSha": sha,
        "rootEntries": visible[:MAX_ENTRIES],
        "rootEntriesComplete": not tree.get("truncated") and len(visible) <= MAX_ENTRIES,
    }


def list_directory(url, sha, directory, getter=api_get):
    owner, name = repository(url)
    if not SHA.fullmatch(sha):
        raise EvidenceError("commit_sha_required")
    directory = relative_path(directory)
    encoded = urllib.parse.quote(directory, safe="/")
    entries = getter(_repo_path(owner, name) + f"/contents/{encoded}?ref={sha}")
    if not isinstance(entries, list):
        raise EvidenceError("directory_unavailable")
    visible = [
        {"path": item["path"], "type": item["type"]}
        for item in entries
        if isinstance(item, dict) and isinstance(item.get("path"), str)
        and item.get("type") in {"file", "dir"}
    ]
    return {"commitSha": sha, "directory": directory,
            "entries": visible[:MAX_ENTRIES], "complete": len(visible) <= MAX_ENTRIES}


def _kind_path(kind, path):
    path = relative_path(path)
    lower = path.lower()
    suffix = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
    if kind == "readme" and not re.fullmatch(r"readme(?:[_-][a-z]{2})?\.(?:md|rst|txt)", lower):
        raise EvidenceError("readme_path_required")
    directories = lower.split("/")[:-1]
    if kind == "example" and not (
        directories and directories[0] in {"examples", "example", "samples", "demo"}
        or any(part in {"getting-started", "tutorials", "tutorial", "how-to"}
               for part in directories)
        or "example" in lower.rsplit("/", 1)[-1]
    ):
        raise EvidenceError("example_or_docs_path_required")
    if kind in {"implementation", "test"} and suffix not in CODE_SUFFIXES:
        raise EvidenceError("source_code_path_required")
    if kind == "test" and not ("test" in lower or "spec" in lower):
        raise EvidenceError("test_path_required")
    return path


def _excerpt(text):
    lines = text.splitlines()
    selected = []
    length = 0
    for number, line in enumerate(lines[:MAX_EXCERPT_LINES], start=1):
        rendered = f"{number}: {line}"
        if length + len(rendered) > MAX_EXCERPT_CHARS:
            break
        selected.append(rendered)
        length += len(rendered) + 1
    return "\n".join(selected), len(selected) < len(lines), len(lines)


def _read_file(owner, name, sha, path, kind, getter):
    encoded = urllib.parse.quote(path, safe="/")
    entry = getter(_repo_path(owner, name) + f"/contents/{encoded}?ref={sha}")
    if (not isinstance(entry, dict) or entry.get("type") != "file"
            or entry.get("path") != path or entry.get("encoding") != "base64"
            or not isinstance(entry.get("content"), str)
            or not isinstance(entry.get("size"), int)
            or entry["size"] > MAX_FILE_BYTES):
        raise EvidenceError(f"{kind}_file_unavailable_or_too_large")
    try:
        data = base64.b64decode(re.sub(r"\s+", "", entry["content"]), validate=True)
        text = data.decode("utf-8")
    except (binascii.Error, UnicodeError):
        raise EvidenceError(f"{kind}_not_readable_text") from None
    if not text.strip() or len(data) != entry["size"]:
        raise EvidenceError(f"{kind}_empty_or_incomplete")
    return text, len(data), f"https://github.com/{owner}/{name}/blob/{sha}/{encoded}"


def read_range(url, sha, path, start, count, getter=api_get):
    owner, name = repository(url)
    if not SHA.fullmatch(sha):
        raise EvidenceError("commit_sha_required")
    path = relative_path(path)
    if not isinstance(start, int) or start < 1 or not isinstance(count, int) or not 1 <= count <= 100:
        raise EvidenceError("invalid_line_range")
    text, _, permalink = _read_file(owner, name, sha, path, "source", getter)
    lines = text.splitlines()
    if start > len(lines):
        raise EvidenceError("line_range_out_of_bounds")
    selected = []
    length = 0
    for number in range(start, min(len(lines), start + count - 1) + 1):
        rendered = f"{number}: {lines[number - 1]}"
        if length + len(rendered) > MAX_EXCERPT_CHARS:
            break
        selected.append(rendered)
        length += len(rendered) + 1
    if not selected:
        raise EvidenceError("line_too_long")
    return {"commitSha": sha, "path": path, "permalink": permalink,
            "range": f"L{start}-L{start + len(selected) - 1}",
            "totalLines": len(lines), "excerpt": "\n".join(selected),
            "excerptPartial": start + len(selected) - 1 < len(lines)}


def inspect(url, sha, readme, example, implementation, test=None, getter=api_get):
    owner, name = repository(url)
    if not SHA.fullmatch(sha):
        raise EvidenceError("commit_sha_required")
    paths = {
        "readme": _kind_path("readme", readme),
        "example": _kind_path("example", example),
        "implementation": _kind_path("implementation", implementation),
    }
    if test is not None:
        paths["test"] = _kind_path("test", test)
    if len(set(paths.values())) != len(paths):
        raise EvidenceError("evidence_files_must_be_distinct")
    root = _repo_path(owner, name)
    commit = getter(root + "/commits/" + sha)
    if not isinstance(commit, dict) or commit.get("sha") != sha:
        raise EvidenceError("commit_unverified")
    evidence = {}
    for kind, path in paths.items():
        text, size, permalink = _read_file(owner, name, sha, path, kind, getter)
        excerpt, partial, total_lines = _excerpt(text)
        evidence[kind] = {
            "path": path,
            "permalink": permalink,
            "bytes": size,
            "totalLines": total_lines,
            "excerptPartial": partial,
            "excerpt": excerpt,
        }
    return {
        "evidenceReady": True,
        "repository": f"https://github.com/{owner}/{name}",
        "commitSha": sha,
        "readScope": "first 100 lines / 10000 chars per file; request more for partial excerpts",
        "evidence": evidence,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    item = commands.add_parser("resolve", help="Pin the default branch and list root paths")
    item.add_argument("url")
    item.add_argument("--ref", help="Branch, tag or commit chosen by the user")
    item = commands.add_parser("ls", help="List a directory at a pinned commit")
    item.add_argument("url")
    item.add_argument("--sha", required=True)
    item.add_argument("--dir", required=True)
    item = commands.add_parser("inspect", help="Read three distinct files at a pinned commit")
    item.add_argument("url")
    item.add_argument("--sha", required=True)
    item.add_argument("--readme", required=True)
    item.add_argument("--example", required=True)
    item.add_argument("--implementation", required=True)
    item.add_argument("--test")
    item = commands.add_parser("read", help="Read a bounded line range at a pinned commit")
    item.add_argument("url")
    item.add_argument("--sha", required=True)
    item.add_argument("--path", required=True)
    item.add_argument("--start", type=int, default=1)
    item.add_argument("--count", type=int, default=80)
    args = parser.parse_args(argv)
    try:
        with deadline(DEADLINES[args.command]):
            if args.command == "resolve":
                result = resolve(args.url, args.ref)
            elif args.command == "ls":
                result = list_directory(args.url, args.sha, args.dir)
            elif args.command == "inspect":
                result = inspect(args.url, args.sha, args.readme, args.example,
                                 args.implementation, args.test)
            else:
                result = read_range(args.url, args.sha, args.path, args.start, args.count)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except EvidenceError as exc:
        print(json.dumps({"ok": False, "code": str(exc)}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
