import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

import build
from scripts import publish_gate


class PublicationGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = Path.cwd()
        os.chdir(self.temp.name)
        self.report, _ = build.build([{'id':'test','url':'https://example.org/config.json'}],
                                    'saved',loader=lambda u:(b'{"sites":[{"key":"one","name":"Demo","type":1,"api":"https://api.example.org/"}]}',u))

    def tearDown(self):
        os.chdir(self.old)
        self.temp.cleanup()

    def response(self, version):
        response = Mock()
        response.status = 200
        response.read.return_value = json.dumps({'version':version}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def decide(self, **kwargs):
        options = {'changed':False,'enabled':True,'output':'public','last_good':'saved',
                   'page_url':'https://example.org/repo/'}
        options.update(kwargs)
        return publish_gate.decide(**options)

    def test_unchanged_already_published_does_not_republish(self):
        with patch('scripts.publish_gate.urllib.request.urlopen',return_value=self.response(self.report['version'])):
            result = self.decide()
        self.assertFalse(result['publish_needed'])
        self.assertFalse(Path('public').exists())

    def test_deployment_failed_after_snapshot_next_run_retries_complete_snapshot(self):
        with patch('scripts.publish_gate.urllib.request.urlopen',return_value=self.response('a'*64)):
            result = self.decide()
        self.assertTrue(result['publish_needed'])
        self.assertEqual(result['reason'],'complete snapshot not yet published')
        self.assertEqual(build.validate_output('public')['version'],self.report['version'])
        self.assertEqual(Path('public/content.json').read_bytes(),Path('saved/content.json').read_bytes())

    def test_pages_missing_republishes_valid_complete_snapshot(self):
        error = urllib.error.HTTPError('https://example.org/repo/report.json',404,'missing',None,None)
        with patch('scripts.publish_gate.urllib.request.urlopen',side_effect=error):
            self.assertTrue(self.decide()['publish_needed'])
        build.validate_output('public')

    def test_transient_errors_do_not_trigger_unnecessary_publication(self):
        for error in (TimeoutError(),urllib.error.HTTPError('https://example.org',503,'unavailable',None,None)):
            with self.subTest(error=error),patch('scripts.publish_gate.urllib.request.urlopen',side_effect=error),self.assertRaises(build.BuildError):
                self.decide()
            self.assertFalse(Path('public').exists())

    def test_invalid_remote_marker_fails_closed(self):
        for version in (None,'invalid',123):
            with self.subTest(version=version),patch('scripts.publish_gate.urllib.request.urlopen',return_value=self.response(version)),self.assertRaises(build.BuildError):
                self.decide()
            self.assertFalse(Path('public').exists())

    def test_pages_disabled_does_not_access_network(self):
        with patch('scripts.publish_gate.urllib.request.urlopen') as request:
            self.assertFalse(self.decide(enabled=False)['publish_needed'])
        request.assert_not_called()

    def test_new_content_and_explicit_restore_request_publication(self):
        with patch('scripts.publish_gate.urllib.request.urlopen') as request:
            self.assertTrue(self.decide(changed=True,output='saved')['publish_needed'])
        request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
