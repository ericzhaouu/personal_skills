import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'read_wechat.py'
SPEC = importlib.util.spec_from_file_location('read_wechat', MODULE_PATH)
read_wechat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(read_wechat)


class ReadWechatTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.script_path = self.root / 'tikhub.py'
        self.script_path.write_text('# fake script\n', encoding='utf-8')
        self.cache_file = self.root / 'cache.json'
        self.url = 'https://mp.weixin.qq.com/s?__biz=MzA1&mid=111&idx=1&sn=abc'
        self.now = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    def _payload(self, **overrides):
        payload = {
            'code': '200',
            'data': {
                'url': self.url,
                'content': {
                    'title': '短文标题',
                    'content_text': '第一段。第二段。',
                    'nick_name': '作者甲',
                    'user_name': 'gh_demo',
                    'create_time': 1704164645,
                },
            },
            'request_id': 'req-123',
            '_tikhub': {'status': 'ok', 'items': []},
        }
        payload.update(overrides)
        return payload

    def _runner(self, payload=None, *, returncode=0, calls=None):
        payload = self._payload() if payload is None else payload

        def run(command, **kwargs):
            if calls is not None:
                calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, returncode, json.dumps(payload, ensure_ascii=False), '')

        return run

    def _write_cache(self, content_text='abcdef', **source_overrides):
        source = {
            'source_kind': 'wechat',
            'requested_url': self.url,
            'source_url': self.url,
            'source_url_matches_requested': True,
            'title': 't',
            'character_count': len(content_text),
            'retrieved_at': '2024-01-01T00:00:00Z',
            'completeness': 'unassessed',
        }
        source.update(source_overrides)
        payload = {
            'magic': read_wechat.MAGIC,
            'format': read_wechat.CACHE_FORMAT,
            'schemaVersion': read_wechat.SCHEMA_VERSION,
            'purpose': read_wechat.PURPOSE,
            'source': source,
            'content': {'content_text': content_text},
        }
        self.cache_file.write_text(json.dumps(payload, ensure_ascii=True), encoding='utf-8')

    def test_fetch_writes_cache_and_preserves_query(self):
        calls = []
        result = read_wechat.fetch(
            self.url,
            self.cache_file,
            runner=self._runner(calls=calls),
            tikhub_script=self.script_path,
            now=self.now,
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['character_count'], len('第一段。第二段。'))
        self.assertEqual(result['source_url'], self.url)
        self.assertEqual(result['author'], '作者甲')
        self.assertEqual(result['source']['completeness'], 'unassessed')
        self.assertEqual(len(calls), 1)
        command, kwargs = calls[0]
        self.assertEqual(command, [
            sys.executable,
            str(self.script_path),
            'fetch',
            self.url,
            '--compact',
            '--retries',
            '0',
            '--timeout',
            '45',
        ])
        self.assertEqual(kwargs['timeout'], 65)
        cached = json.loads(self.cache_file.read_text(encoding='utf-8'))
        self.assertEqual(cached['source']['requested_url'], self.url)
        self.assertEqual(cached['source']['retrieved_at'], '2024-01-02T03:04:05Z')
        self.assertEqual(cached['content']['content_text'], '第一段。第二段。')

    def test_fetch_marks_mismatched_source_and_drops_unsafe_request_id(self):
        payload = self._payload(
            request_id='sk-secret',
            data={
                'url': 'https://mp.weixin.qq.com/s?__biz=MzA1&mid=111&idx=2&sn=def',
                'content': {
                    'title': '短文标题',
                    'content_text': '短内容',
                    'user_name': 'gh_demo',
                },
            },
        )
        read_wechat.fetch(self.url, self.cache_file, runner=self._runner(payload), tikhub_script=self.script_path, now=self.now)
        cached = json.loads(self.cache_file.read_text(encoding='utf-8'))
        self.assertFalse(cached['source']['source_url_matches_requested'])
        self.assertNotIn('request_id', cached['source'])

    def test_fetch_prevents_overwrite_before_runner(self):
        self.cache_file.write_text('x', encoding='utf-8')
        calls = []
        with self.assertRaises(read_wechat.ControlledError):
            read_wechat.fetch(self.url, self.cache_file, runner=self._runner(calls=calls), tikhub_script=self.script_path)
        self.assertEqual(calls, [])

    def test_fetch_rejects_missing_script_without_runner(self):
        missing = self.root / 'missing.py'
        calls = []
        with self.assertRaises(read_wechat.ControlledError):
            read_wechat.fetch(self.url, self.cache_file, runner=self._runner(calls=calls), tikhub_script=missing)
        self.assertEqual(calls, [])

    def test_fetch_rejects_invalid_url_variants(self):
        bad_urls = [
            'https://evilmp.weixin.qq.com/s?a=1',
            'https://mp.weixin.qq.com.evil.com/s?a=1',
            'https://user@mp.weixin.qq.com/s?a=1',
            'https://mp.weixin.qq.com:443/s?a=1',
            'https://mp.weixin.qq.com/other?a=1',
            'https://mp.weixin.qq.com/s?query=line\nbreak',
        ]
        for bad_url in bad_urls:
            with self.subTest(url=bad_url):
                with self.assertRaises(read_wechat.ControlledError):
                    read_wechat.fetch(bad_url, self.cache_file, runner=self._runner(), tikhub_script=self.script_path)
                self.assertFalse(self.cache_file.exists())

    def test_valid_wechat_fragment_is_not_sent_upstream(self):
        calls = []
        result = read_wechat.fetch(self.url + "#wechat_redirect", self.cache_file,
                                  runner=self._runner(calls=calls), tikhub_script=self.script_path)
        self.assertEqual(calls[0][0][3], self.url)
        self.assertTrue(Path(result["cache_file"]).is_absolute())

    def test_invalid_timestamp_and_code_fail_without_traceback(self):
        payload = self._payload(code="9" * 10000)
        with self.assertRaises(read_wechat.ControlledError):
            read_wechat.fetch(self.url, self.cache_file, runner=self._runner(payload), tikhub_script=self.script_path)
        payload = self._payload()
        payload["data"]["content"]["create_time"] = 10 ** 40
        with self.assertRaises(read_wechat.ControlledError):
            read_wechat.fetch(self.url, self.cache_file, runner=self._runner(payload), tikhub_script=self.script_path)

    def test_unpaired_surrogate_does_not_leave_partial_cache(self):
        payload = self._payload()
        payload["data"]["content"]["content_text"] = "\ud800abc"
        with self.assertRaises(read_wechat.ControlledError):
            read_wechat.fetch(self.url, self.cache_file, runner=self._runner(payload), tikhub_script=self.script_path)
        self.assertFalse(self.cache_file.exists())
        self._write_cache(content_text="\ud800abc")
        with self.assertRaises(read_wechat.ControlledError):
            read_wechat.read(self.cache_file)

    def test_fetch_rejects_bad_upstream_shapes_without_cache(self):
        cases = [
            self._payload(error=True),
            self._payload(code=500),
            self._payload(_tikhub={'status': 'empty'}),
            self._payload(data={}),
            self._payload(data={'url': self.url, 'content': {'title': 'ok', 'content_text': ''}}),
            self._payload(data={'url': self.url, 'content': {'title': 1, 'content_text': 'ok'}}),
            self._payload(data={'url': 'https://evil.com/s?a=1', 'content': {'title': 'ok', 'content_text': 'ok'}}),
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(read_wechat.ControlledError):
                    read_wechat.fetch(self.url, self.cache_file, runner=self._runner(payload), tikhub_script=self.script_path)
                self.assertFalse(self.cache_file.exists())

    def test_fetch_nonzero_exit_has_no_fallback(self):
        calls = []
        with self.assertRaises(read_wechat.ControlledError):
            read_wechat.fetch(
                self.url,
                self.cache_file,
                runner=self._runner(returncode=2, calls=calls),
                tikhub_script=self.script_path,
            )
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.cache_file.exists())

    def test_read_chunks_concatenate_and_handle_edges(self):
        text = '甲乙丙丁戊己庚辛'
        self._write_cache(content_text=text)
        first = read_wechat.read(self.cache_file, offset=0, max_chars=3)
        second = read_wechat.read(self.cache_file, offset=first['next_offset'], max_chars=3)
        third = read_wechat.read(self.cache_file, offset=second['next_offset'], max_chars=3)
        self.assertEqual(first['content_text'] + second['content_text'] + third['content_text'], text)
        self.assertFalse(first['end_reached'])
        self.assertEqual(read_wechat.read(self.cache_file, offset=len(text), max_chars=3), {
            'source': first['source'],
            'offset': len(text),
            'next_offset': len(text),
            'end_reached': True,
            'character_count': len(text),
            'content_text': '',
        })

    def test_read_rejects_bad_bounds_and_inconsistent_count(self):
        self._write_cache(content_text='abcd', character_count=5)
        with self.assertRaises(read_wechat.ControlledError):
            read_wechat.read(self.cache_file)
        self._write_cache(content_text='abcd')
        with self.assertRaises(read_wechat.ControlledError):
            read_wechat.read(self.cache_file, offset=-1)
        with self.assertRaises(read_wechat.ControlledError):
            read_wechat.read(self.cache_file, offset=5)
        with self.assertRaises(read_wechat.ControlledError):
            read_wechat.read(self.cache_file, max_chars=0)

    def test_read_rejects_malformed_or_large_cache(self):
        self.cache_file.write_text('{bad json', encoding='utf-8')
        with self.assertRaises(read_wechat.ControlledError):
            read_wechat.read(self.cache_file)
        self.cache_file.write_text(json.dumps({'magic': 'wrong'}), encoding='utf-8')
        with self.assertRaises(read_wechat.ControlledError):
            read_wechat.read(self.cache_file)
        with self.cache_file.open('wb') as handle:
            handle.truncate(read_wechat.MAX_CACHE_BYTES + 1)
        with self.assertRaises(read_wechat.ControlledError):
            read_wechat.read(self.cache_file)

    def test_linux_cache_permissions_are_private(self):
        if os.name == 'nt':
            self.skipTest('mode bits are not portable on Windows')
        read_wechat.fetch(self.url, self.cache_file, runner=self._runner(), tikhub_script=self.script_path, now=self.now)
        mode = stat.S_IMODE(os.stat(self.cache_file).st_mode)
        self.assertEqual(mode, 0o600)

    def test_cli_error_is_json(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = read_wechat.main(['read', str(self.cache_file)])
        self.assertEqual(exit_code, 1)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload['error'])
        self.assertIn('does not exist', payload['message'])


if __name__ == '__main__':
    unittest.main()
