import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch, Mock
import urllib.error
import urllib.request

import build as b

BASE = 'https://example.com/repo/main/config.json'


def site(**kw):
    return {'key': 'same', 'name': '同名', 'type': 3, 'api': 'csp_Test', **kw}


def loader_for(doc):
    return lambda url: (json.dumps(doc, ensure_ascii=False).encode(), url)


def result(sites, sid='a', spider='./jar/main.jar', **globals):
    return b.expand_source({'id': sid, 'url': BASE}, loader_for({'spider': spider, 'sites': sites, **globals}))


class ParsingTests(unittest.TestCase):
    def test_utf8_bom_and_gb18030(self):
        for encoding in ('utf-8-sig', 'gb18030'):
            doc, found = b.decode_json('{"sites":[],"name":"中文"}'.encode(encoding))
            self.assertEqual(doc['name'], '中文')
            self.assertEqual(found, encoding)

    def test_bad_json_and_encoding(self):
        for data in (b'nope', b'[]', b'\xff\xff', b'{"a":NaN}'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                b.decode_json(data)

    def test_oversize(self):
        with self.assertRaises(b.BuildError):
            b.decode_json(b' ' * (b.MAX_BYTES + 1))

    def test_invalid_sites_fail_closed(self):
        for doc in ({'sites': []}, {'sites': {}}, {'sites': [None]}, {'sites': [{}]}, {'urls': []}, {}):
            with self.subTest(doc=doc), self.assertRaises(b.BuildError):
                b.expand_source({'id': 'a', 'url': BASE}, loader_for(doc))

    def test_fallback_retains_real_origin(self):
        def load(url):
            if url == BASE:
                raise urllib.error.HTTPError(url, 404, 'missing', None, None)
            return loader_for({'sites': [site(type=1, api='./api.json')]})(url)
        fallback = 'https://raw.githubusercontent.com/x/y/main/fun.json'
        meta, docs, _ = b.expand_source({'id': 'a', 'url': BASE, 'fallbacks': [fallback]}, load)
        self.assertTrue(meta['fallback'])
        self.assertEqual(meta['requested_url'], BASE)
        self.assertEqual(meta['used_url'], fallback)
        self.assertEqual(meta['attempts'][0]['reason'], 'HTTP 404')
        self.assertEqual(meta['sha256'], docs[0][1]['sha256'])

    def test_redirect_origin_used_for_paths(self):
        final = 'https://example.org/actual/config.json'
        res = b.expand_source({'id': 'a', 'url': BASE}, lambda u: (json.dumps({'spider': './a.jar', 'sites': [site()]}).encode(), final))
        tv, *_ = b.merge([res])
        self.assertEqual(tv['sites'][0]['jar'], 'https://example.org/actual/a.jar')

    def test_multirepo_deduplicates_repeated_child(self):
        docs = {BASE: {'urls': [{'url': './child.json'}, {'url': './child.json'}]},
                'https://example.com/repo/main/child.json': {'sites': [site(type=1, api='https://api.example.org/')]}}
        meta, leaves, raw = b.expand_source({'id': 'a', 'url': BASE}, lambda url: (json.dumps(docs[url]).encode(), url))
        self.assertEqual(meta['upstream_sites'], 1)
        self.assertEqual(len(raw), 2)

    def test_multirepo_cycle_fails(self):
        with self.assertRaises(b.BuildError):
            b.expand_source({'id': 'a', 'url': BASE}, loader_for({'urls': [{'url': BASE}]}))

    def test_multirepo_depth_limit(self):
        def load(url):
            n = int(url.rsplit('/', 1)[-1])
            return json.dumps({'urls': [{'url': str(n + 1)}]}).encode(), url
        with self.assertRaises(b.BuildError):
            b.expand_source({'id': 'a', 'url': 'https://example.com/0'}, load)

    def test_multirepo_count_limit(self):
        def load(url):
            doc = {'urls': [{'url': f'./{i}.json'} for i in range(25)]} if url == BASE else {'sites': [site(type=1, api='https://api.example.org/')]}
            return json.dumps(doc).encode(), url
        with self.assertRaises(b.BuildError):
            b.expand_source({'id': 'a', 'url': BASE}, load)

    def test_private_credentialed_malformed_urls(self):
        for url in ('file:///etc/passwd', 'http://localhost/a', 'http://127.0.0.1/a', 'https://10.0.0.1/a',
                    'http://[::1]/', 'https://u:secret@example.com/a', 'https://example.com:bad/a', 'https://example.com/a\nX', 'https://example.com/..\\a'):
            with self.subTest(url=url), self.assertRaises(b.BuildError):
                b.safe_url(url)


class PathTests(unittest.TestCase):
    def test_relative_paths(self):
        for path in ('./jar/a.jar', './js/a.js', './lib/a.js', './py/a.py', './json/a.json', '../a.json', '/a.json', 'jar/a.jar', './json/noextension'):
            with self.subTest(path=path):
                self.assertEqual(b.file_reference(path, BASE), urllib.parse.urljoin(BASE, path))

    def test_md5_preserved(self):
        suffix = ';md5;' + 'aB12' * 8
        self.assertEqual(b.file_reference('./a.jar' + suffix, BASE, 'jar'), 'https://example.com/repo/main/a.jar' + suffix)
        with self.assertRaises(b.BuildError):
            b.file_reference('./a.jar;md5;wrong', BASE, 'jar')

    def test_nested_and_delimited(self):
        data = {'token': './lib/token.json', 'json': './json/a.json', 'userScript': './js/a.user.js',
                'nested': [{'ext': './py/b.py'}], 'description': 'keep ./this literal',
                'type': '电影#日本', 'regex': './keep.js'}
        new = b.resolve_nested(data, BASE)
        self.assertEqual(new['token'], 'https://example.com/repo/main/lib/token.json')
        self.assertTrue(new['nested'][0]['ext'].startswith('https://'))
        self.assertEqual(new['regex'], data['regex'])
        self.assertEqual(new['description'], data['description'])
        self.assertEqual(b.file_reference('./a.json$$$./b.txt$$$db$$$1', BASE), 'https://example.com/repo/main/a.json$$$https://example.com/repo/main/b.txt$$$db$$$1')

    def test_inline_json_only_changes_recognized_fields(self):
        value = '{"userScript":"./js/a.js","match":"./do-not-change"}'
        new = json.loads(b.file_reference(value, BASE))
        self.assertTrue(new['userScript'].startswith('https://'))
        self.assertEqual(new['match'], './do-not-change')

    def test_labels_url_query_and_embedded_parameters_unchanged(self):
        for value in ('proxy', '18+', '1', 'null$$$https://x.test$$$proxy', '免嗅:1,分类url:https://x.test/./a',
                      'https://example.com/a?b=2&a=1', 'some label ./js/a.js', '{"分类":"./文字"}'):
            with self.subTest(value=value):
                self.assertEqual(b.file_reference(value, BASE), value)

    def test_csp_and_file_loader_inherit_original_jar(self):
        for api in ('csp_Test', './lib/a.js', './py/a.py'):
            tv, *_ = b.merge([result([site(api=api)])])
            self.assertEqual(tv['sites'][0]['jar'], 'https://example.com/repo/main/jar/main.jar')

    def test_explicit_jar_respected(self):
        tv, *_ = b.merge([result([site(jar='./other.jar')])])
        self.assertEqual(tv['sites'][0]['jar'], 'https://example.com/repo/main/other.jar')

    def test_missing_jar_fails(self):
        with self.assertRaises(b.BuildError):
            b.merge([result([site()], spider=None)])


class MergeTests(unittest.TestCase):
    def test_same_name_different_api_preserved(self):
        tv, _, _, report = b.merge([result([site(api='https://a.example/'), site(api='https://b.example/')])])
        self.assertEqual(len(tv['sites']), 2)
        self.assertEqual(len(report['name_collisions']), 1)
        self.assertEqual(len({s['name'] for s in tv['sites']}), 2)

    def test_same_class_different_jar_preserved(self):
        tv, _, _, report = b.merge([result([site()], 'a', './a.jar'), result([site()], 'b', './b.jar')])
        self.assertEqual(len(tv['sites']), 2)
        self.assertTrue(any(g['different_jars'] for g in report['kept_variants']))

    def test_ext_header_jar_flags_and_context_preserved(self):
        variants = [site(), site(ext='1'), site(header={'User-Agent':'A'}), site(jar='./b.jar'), site(searchable=0)]
        tv, *_ = b.merge([result(variants)])
        self.assertEqual(len(tv['sites']), len(variants))
        tv, *_ = b.merge([result([site()], 'a', rules=[{'x':'a'}]), result([site()], 'b', rules=[{'x':'b'}])])
        self.assertEqual(len(tv['sites']), 2)

    def test_exact_duplicate_trace_and_conservation(self):
        tv, _, _, report = b.merge([result([site(), site(key='different', name='different')])])
        self.assertEqual(len(tv['sites']), 1)
        self.assertEqual(report['removed_duplicates'][0]['kept_key'], tv['sites'][0]['key'])
        self.assertEqual(2, len(tv['sites']) + len(report['removed_duplicates']))

    def test_engine_variants_preserved(self):
        tv, *_ = b.merge([result([site(), site(api='./a.py'), site(api='./a.js')])])
        self.assertEqual(len(tv['sites']), 3)

    def test_stable_keys_unique_and_order_independent(self):
        sites = [site(api='csp_A'), site(api='csp_B')]
        a, *_ = b.merge([result(sites)])
        c, *_ = b.merge([result(list(reversed(sites)))])
        self.assertEqual({s['key'] for s in a['sites']}, {s['key'] for s in c['sites']})
        self.assertEqual(len({s['key'] for s in a['sites']}), 2)

    def test_tools_and_searchable_unchanged(self):
        tv, content, _, report = b.merge([result([site(searchable=0), site(key='push', api='csp_Push')])])
        self.assertEqual(len(tv['sites']), 2)
        self.assertEqual(len(content['sites']), 1)
        self.assertEqual(content['sites'][0]['searchable'], 0)
        self.assertEqual(len(report['hidden_tools']), 1)

    def test_global_network_fields_not_combined(self):
        tv, *_ = b.merge([result([site()], hosts=['evil'], proxy=['x'], doh=['y'])])
        self.assertEqual(set(tv), {'sites'})

    def test_dependencies_and_http_inventory(self):
        tv, _, _, report = b.merge([result([site(api='./a.py', ext={'url':'http://api.example.org/a', 'description':'http://another.example.org/'})])])
        self.assertEqual(len(report['dependencies']), 2)
        self.assertEqual(len(report['http_references']), 2)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = Path.cwd()
        os.chdir(self.temp.name)
        self.specs = [{'id': sid, 'url': f'https://example.com/{sid}/config.json'} for sid in ('clun', 'wang', 'qist', 'pray')]

    def tearDown(self):
        os.chdir(self.old)
        self.temp.cleanup()

    def load(self, url):
        return json.dumps({'spider':'./main.jar', 'sites':[site(), site(key='api', type=1, api='https://api.example.org/')]}).encode(), url

    def test_four_sources_complete_manifest_report_and_hashes(self):
        report, changed = b.build(self.specs, 'public', loader=self.load)
        self.assertTrue(changed)
        self.assertEqual(len(report['sources']), 4)
        self.assertEqual(report['counts']['raw'], 8)
        self.assertEqual(report['counts']['raw'], report['counts']['retained'] + report['counts']['removed'])
        b.validate_output('public')
        self.assertEqual(len(list(Path('.snapshots').glob('*/checksums.json'))), 1)

    def test_single_failure_does_not_touch_public(self):
        b.build(self.specs, 'public', loader=self.load)
        before = {p.name: p.read_bytes() for p in Path('public').iterdir()}
        def failed(url):
            if '/qist/' in url:
                raise TimeoutError()
            return self.load(url)
        with self.assertRaises(b.BuildError):
            b.build(self.specs, 'public', 'public', failed)
        self.assertEqual(before, {p.name: p.read_bytes() for p in Path('public').iterdir()})
        self.assertTrue(Path('failure.json').exists())

    def test_first_failure_no_partial_output(self):
        with self.assertRaises(b.BuildError):
            b.build(self.specs, 'public', loader=lambda u: (_ for _ in ()).throw(TimeoutError()))
        self.assertFalse(Path('public').exists())

    def test_no_content_change_does_not_republish(self):
        b.build(self.specs, 'public', loader=self.load)
        before = Path('public/report.json').read_bytes()
        _, changed = b.build(self.specs, 'public', 'public', self.load)
        self.assertFalse(changed)
        self.assertEqual(before, Path('public/report.json').read_bytes())

    def test_large_single_source_drop_blocked(self):
        def many(url):
            return json.dumps({'spider':'./a.jar','sites':[site(key=str(i),api=f'csp_{i}') for i in range(10)]}).encode(),url
        b.build(self.specs, 'public', loader=many)
        before = Path('public/report.json').read_bytes()
        with self.assertRaises(b.BuildError):
            b.build(self.specs, 'public', 'public', self.load)
        self.assertEqual(before, Path('public/report.json').read_bytes())

    def test_corrupt_last_good_fails_closed(self):
        b.build(self.specs, 'public', loader=self.load)
        Path('public/tvbox.json').write_text('{"sites":[]}', encoding='utf-8')
        with self.assertRaises(b.BuildError):
            b.build(self.specs, 'public', 'public', self.load)

    def test_duplicate_source_ids_rejected(self):
        with self.assertRaises(b.BuildError):
            b.build([self.specs[0], self.specs[0]], 'public', loader=self.load)

    def test_report_escapes_upstream_html(self):
        def load(url):
            return json.dumps({'spider':'./a.jar','sites':[site(name='<script>alert(1)</script>')]}).encode(),url
        b.build(self.specs, 'public', loader=load)
        text = Path('public/report.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;', text)


class Response:
    status = 200
    url = BASE
    headers = {}
    def __init__(self, data):
        self.body = io.BytesIO(data)
    def read(self, n):
        return self.body.read(n)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


class FetchTests(unittest.TestCase):
    @patch('build.safe_url', side_effect=lambda url, **kw:url)
    @patch('build.urllib.request.build_opener')
    def test_limited_timeout_retries(self, opener, safe):
        opener.return_value.open.side_effect = TimeoutError()
        with patch('build.time.sleep'), self.assertRaises(TimeoutError):
            b.fetch(BASE)
        self.assertEqual(opener.return_value.open.call_count, b.RETRIES)
        self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], b.TIMEOUT)

    @patch('build.safe_url', side_effect=lambda url, **kw:url)
    @patch('build.urllib.request.build_opener')
    def test_404_fails(self, opener, safe):
        opener.return_value.open.side_effect = urllib.error.HTTPError(BASE,404,'missing',None,None)
        with patch('build.time.sleep'), self.assertRaises(urllib.error.HTTPError):
            b.fetch(BASE)

    @patch('build.safe_url', side_effect=lambda url, **kw:url)
    @patch('build.urllib.request.build_opener')
    def test_size_limit_stream_and_header(self, opener, safe):
        for advertised in (0, b.MAX_BYTES + 1):
            response = Response(b' ' * (b.MAX_BYTES + 1))
            response.headers = {'Content-Length': str(advertised)}
            opener.return_value.open.return_value = response
            with self.subTest(advertised=advertised), self.assertRaises(b.BuildError):
                b.fetch(BASE)

    @patch('build.safe_url', side_effect=lambda url, **kw:url)
    def test_302_resolves_to_final(self, safe):
        req = urllib.request.Request(BASE)
        redirected = b.Redirects().redirect_request(req,None,302,'found',{},'https://example.org/actual/config.json')
        self.assertEqual(redirected.full_url,'https://example.org/actual/config.json')

    def test_redirect_private_and_downgrade_rejected(self):
        req = urllib.request.Request(BASE)
        with self.assertRaises(b.BuildError):
            b.Redirects().redirect_request(req,None,302,'found',{},'http://127.0.0.1/a')
        with patch('build.safe_url', side_effect=lambda url,**kw:url), self.assertRaises(b.BuildError):
            b.Redirects().redirect_request(req,None,302,'found',{},'http://example.org/a')


if __name__ == '__main__':
    unittest.main()
