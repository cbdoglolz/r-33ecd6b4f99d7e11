import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import urllib.error

import build
from scripts import check_dependency_urls, verify_pages


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = Path.cwd()
        os.chdir(self.temp.name)
        self.report, _ = build.build([{'id':'test','url':'https://example.org/a.json'}],'public',loader=lambda url:(b'{"sites":[{"key":"a","name":"a","type":1,"api":"https://api.example.org/"}]}',url))

    def tearDown(self):
        os.chdir(self.old)
        self.temp.cleanup()

    def response(self, req, **kwargs):
        name = req.full_url.rsplit('/',1)[1]
        response = Mock()
        response.status = 200
        response.headers = {'Content-Type':'application/json; charset=utf-8'}
        response.read.return_value = Path('public',name).read_bytes()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_pages_json_type_hashes_and_expected_version(self):
        with patch('scripts.verify_pages.urllib.request.urlopen',side_effect=self.response):
            result = verify_pages.verify('https://example.org/repo/',self.report['version'])
        self.assertEqual(len(result['http_checks']),4)
        self.assertTrue(all(c['status']==200 for c in result['http_checks']))

    def test_pages_wrong_version_fails(self):
        with patch('scripts.verify_pages.urllib.request.urlopen',side_effect=self.response),self.assertRaises(build.BuildError):
            verify_pages.verify('https://example.org/repo/','wrong')

    def test_pages_html_200_fails(self):
        def response(req,**kw):
            resp = self.response(req,**kw)
            resp.headers['Content-Type'] = 'text/html'
            return resp
        with patch('scripts.verify_pages.urllib.request.urlopen',side_effect=response),self.assertRaises(build.BuildError):
            verify_pages.verify('https://example.org/repo/')

    def test_pages_404_fails(self):
        with patch('scripts.verify_pages.urllib.request.urlopen',side_effect=urllib.error.HTTPError('https://example.org',404,'missing',None,None)),self.assertRaises(urllib.error.HTTPError):
            verify_pages.verify('https://example.org/repo/')

    def test_dependency_head_never_reads_body_or_logs_redirect_query(self):
        response = Mock()
        response.status = 200
        response.url = 'https://cdn.example.org/file.jar?tempauth=sensitive-signature'
        response.headers = {'Content-Type':'application/java-archive'}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch('scripts.check_dependency_urls.safe_url',side_effect=lambda u,**kw:u),patch('scripts.check_dependency_urls.urllib.request.build_opener',return_value=opener):
            result = check_dependency_urls.check({'url':'https://example.org/a.jar'})
        self.assertNotIn('sensitive-signature',json.dumps(result))
        self.assertNotIn('tempauth',json.dumps(result))
        response.read.assert_not_called()
        self.assertEqual(opener.open.call_args.args[0].method,'HEAD')


if __name__ == '__main__':
    unittest.main()
