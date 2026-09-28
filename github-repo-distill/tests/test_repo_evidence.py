import base64
import importlib.util
from pathlib import Path
import signal
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "repo_evidence.py"
spec = importlib.util.spec_from_file_location("repo_evidence", SOURCE)
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)

SHA = "1" * 40
URL = "https://github.com/example/Project"


def fixture_file(path, text):
    data = text.encode("utf-8")
    encoded = base64.b64encode(data).decode("ascii")
    encoded = "\n".join(encoded[n:n + 60] for n in range(0, len(encoded), 60))
    return {"type": "file", "path": path, "encoding": "base64",
            "content": encoded, "size": len(data)}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.responses = {
            "/repos/example/Project": {
                "full_name": "example/Project", "default_branch": "main"},
            "/repos/example/Project/commits/main": {"sha": SHA},
            "/repos/example/Project/commits/v1": {"sha": SHA},
            "/repos/example/Project/commits/" + SHA: {"sha": SHA},
            "/repos/example/Project/git/trees/" + SHA: {
                "tree": [{"path": "README.md", "type": "blob"},
                         {"path": "examples", "type": "tree"},
                         {"path": "src", "type": "tree"}]},
            "/repos/example/Project/contents/examples?ref=" + SHA: [
                {"path": "examples/demo.py", "type": "file"}],
            "/repos/example/Project/contents/README.md?ref=" + SHA:
                fixture_file("README.md", "# Example\nUse this for decisions.\n"),
            "/repos/example/Project/contents/examples/demo.py?ref=" + SHA:
                fixture_file("examples/demo.py", "from src.core import do_work\nprint(do_work())\n"),
            "/repos/example/Project/contents/src/core.py?ref=" + SHA:
                fixture_file("src/core.py", "def do_work():\n    return 'value'\n"),
            "/repos/example/Project/contents/tests/test_core.py?ref=" + SHA:
                fixture_file("tests/test_core.py", "def test_core():\n    assert True\n"),
        }

    def get(self, path):
        self.calls.append(path)
        return self.responses[path]

    def test_resolve_pins_default_branch_and_lists_root(self):
        result = evidence.resolve(URL, getter=self.get)
        self.assertEqual(result["commitSha"], SHA)
        self.assertEqual(result["selectedRef"], "main")
        self.assertTrue(result["rootEntriesComplete"])
        self.assertEqual(len(result["rootEntries"]), 3)

    def test_explicit_ref_is_pinned(self):
        result = evidence.resolve(URL, "v1", getter=self.get)
        self.assertEqual(result["selectedRef"], "v1")
        self.assertIn("/repos/example/Project/commits/v1", self.calls)

    def test_inspect_requires_three_distinct_file_contents_at_one_commit(self):
        result = evidence.inspect(
            URL, SHA, "README.md", "examples/demo.py", "src/core.py",
            "tests/test_core.py", getter=self.get)
        self.assertTrue(result["evidenceReady"])
        self.assertEqual(set(result["evidence"]), {"readme", "example", "implementation", "test"})
        self.assertIn("def do_work", result["evidence"]["implementation"]["excerpt"])
        self.assertIn("/blob/" + SHA + "/src/core.py",
                      result["evidence"]["implementation"]["permalink"])
        self.assertTrue(all("?ref=" + SHA in path for path in self.calls if "/contents/" in path))

    def test_rejects_wrong_sha_and_changed_file(self):
        with self.assertRaisesRegex(evidence.EvidenceError, "commit_sha_required"):
            evidence.inspect(URL, "latest", "README.md", "examples/demo.py",
                             "src/core.py", getter=self.get)
        key = "/repos/example/Project/contents/src/core.py?ref=" + SHA
        self.responses[key] = fixture_file("src/different.py", "print('wrong')")
        with self.assertRaisesRegex(evidence.EvidenceError, "implementation_file_unavailable"):
            evidence.inspect(URL, SHA, "README.md", "examples/demo.py",
                             "src/core.py", getter=self.get)

    def test_no_directory_listing_masquerades_as_source(self):
        self.assertEqual(evidence.list_directory(
            URL, SHA, "examples", getter=self.get)["entries"][0]["path"],
            "examples/demo.py")
        with self.assertRaisesRegex(evidence.EvidenceError, "example_or_docs_path_required"):
            evidence.inspect(URL, SHA, "README.md", "src/core.py",
                             "tests/test_core.py", getter=self.get)

    def test_oversized_and_empty_sources_fail(self):
        key = "/repos/example/Project/contents/examples/demo.py?ref=" + SHA
        self.responses[key]["size"] = evidence.MAX_FILE_BYTES + 1
        with self.assertRaisesRegex(evidence.EvidenceError, "example_file_unavailable"):
            evidence.inspect(URL, SHA, "README.md", "examples/demo.py",
                             "src/core.py", getter=self.get)
        self.responses[key] = fixture_file("examples/demo.py", "")
        with self.assertRaisesRegex(evidence.EvidenceError, "example_empty_or_incomplete"):
            evidence.inspect(URL, SHA, "README.md", "examples/demo.py",
                             "src/core.py", getter=self.get)

    def test_rejects_unsafe_urls_and_paths(self):
        bad = [
            "http://github.com/example/Project",
            "https://github.com.evil.test/example/Project",
            "https://user@github.com/example/Project",
            "https://github.com:443/example/Project",
            "https://github.com/example/Project?token=x",
            "https://github.com/example/Project/tree/main",
        ]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(evidence.EvidenceError):
                evidence.repository(url)
        for path in ("../secret", "/etc/passwd", "a//b", "a\\b", "a/./b"):
            with self.subTest(path=path), self.assertRaises(evidence.EvidenceError):
                evidence.relative_path(path)

    def test_partial_excerpt_is_marked(self):
        text = "\n".join("line" for _ in range(110))
        excerpt, partial, total = evidence._excerpt(text)
        self.assertTrue(partial)
        self.assertEqual(total, 110)
        self.assertTrue(excerpt.startswith("1: line"))

    def test_read_range_from_pinned_file(self):
        result = evidence.read_range(URL, SHA, "examples/demo.py", 2, 5, getter=self.get)
        self.assertEqual(result["range"], "L2-L2")
        self.assertIn("2: print(do_work())", result["excerpt"])
        with self.assertRaisesRegex(evidence.EvidenceError, "line_range_out_of_bounds"):
            evidence.read_range(URL, SHA, "examples/demo.py", 99, 5, getter=self.get)

    @unittest.skipUnless(hasattr(signal, "setitimer"), "POSIX signal timer required")
    def test_deadline_stops_stalled_lookup(self):
        with self.assertRaisesRegex(evidence.EvidenceError, "github_timeout"):
            with evidence.deadline(0.01):
                time.sleep(0.2)


if __name__ == "__main__":
    unittest.main()
