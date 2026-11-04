import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build
from scripts import snapshot


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = Path.cwd()
        os.chdir(self.temp.name)
        specs = [{'id': 'test', 'url': 'https://example.com/a.json'}]
        build.build(specs, 'public', loader=lambda url: (b'{"sites":[{"key":"a","name":"A","type":1,"api":"https://api.example.com/"}]}', url))

    def tearDown(self):
        os.chdir(self.old)
        self.temp.cleanup()

    def api_data(self, corrupt=False):
        report = build.validate_output('public')
        checks = {n: hashlib.sha256(Path('public',n).read_bytes()).hexdigest() for n in build.PUBLIC_FILES}
        def api(path, *args, **kwargs):
            if '/git/ref/' in path:
                return {'object': {'sha':'a'*40}}
            if '/contents/snapshot.json' in path:
                return json.dumps({'version':report['version'], 'public_checksums':checks}).encode()
            name = path.split('/public/')[1].split('?')[0]
            if corrupt and name == 'tvbox.json':
                return b'{}'
            return Path('public',name).read_bytes()
        return api

    def test_first_run_without_snapshot(self):
        with patch.object(snapshot, 'api', side_effect=FileNotFoundError()):
            self.assertFalse(snapshot.recover('u/r','.last-good/public'))
        self.assertFalse(Path('.last-good/public').exists())

    def test_full_recovery_pinned_to_one_commit(self):
        with patch.object(snapshot, 'api', side_effect=self.api_data()) as api:
            self.assertTrue(snapshot.recover('u/r','.last-good/public'))
        self.assertEqual(build.validate_output('public')['version'],build.validate_output('.last-good/public')['version'])
        for call in api.call_args_list[1:]:
            self.assertIn('?ref='+'a'*40,call.args[0])

    def test_corrupt_snapshot_blocks_recovery(self):
        with patch.object(snapshot,'api',side_effect=self.api_data(corrupt=True)), self.assertRaises((build.BuildError,KeyError)):
            snapshot.recover('u/r','.last-good/public')

    def test_invalid_restore_ref(self):
        for ref in ('main', '../../main', 'a'*39, 'x'*40, '; touch pwn'):
            with self.subTest(ref=ref), self.assertRaises(build.BuildError):
                snapshot.get_ref('u/r',ref)

    def test_immutable_git_snapshot_contains_raw_and_public(self):
        calls = []
        def api(path, method='GET', payload=None, **kwargs):
            calls.append((path,method,payload))
            if '/git/ref/' in path:
                raise FileNotFoundError()
            if '/git/blobs' in path:
                return {'sha':'b'*40}
            if '/git/trees' in path:
                paths = {x['path'] for x in payload['tree']}
                self.assertIn('public/content.json',paths)
                self.assertIn('raw/test-00.json',paths)
                self.assertIn('snapshot.json',paths)
                return {'sha':'c'*40}
            if '/git/commits' in path:
                self.assertEqual(payload['parents'],[])
                return {'sha':'d'*40}
            return {}
        with patch.object(snapshot,'api',side_effect=api):
            sha = snapshot.persist('u/r')
        self.assertEqual(sha,'d'*40)
        self.assertEqual(calls[-1][2]['ref'],'refs/heads/snapshots')

    def test_api_errors_not_mistaken_for_first_run(self):
        with patch.object(snapshot,'api',side_effect=build.BuildError('403')), self.assertRaises(build.BuildError):
            snapshot.recover('u/r','.last-good/public')


if __name__ == '__main__':
    unittest.main()
