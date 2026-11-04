import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

import build
import catalog


BASE = 'https://example.org/config.json'


def source(sites, sid='a', label=None, jar='./main.jar'):
    spec = {'id': sid, 'url': BASE}
    if label:
        spec['label'] = label
    return build.expand_source(spec, lambda url: (json.dumps({'sites':sites, 'spider':jar}).encode(), url))


def site(key='one', **kwargs):
    return {'key':key, 'name':'普通影视', 'type':3, 'api':'csp_Demo', **kwargs}


def merge(sites, previous=None, **kwargs):
    tv, content, contexts, details = build.merge([source(sites, **kwargs)], previous)
    manifest = {'sites':contexts, 'identity_registry':details['identity_registry']}
    return tv, manifest, details


class IdentityTests(unittest.TestCase):
    def test_legacy_manifest_migrates_without_changing_ids(self):
        tv, manifest, _ = merge([site(),site('two',api='./demo.py')])
        legacy = {'sites':copy.deepcopy(manifest['sites'])}
        old_keys = ['legacy-one-123456','legacy-two-654321']
        for context,key in zip(legacy['sites'],old_keys):
            context['key'] = key
        new, state, report = merge([site(),site('two',api='./demo.py')],legacy,label='一源')
        self.assertEqual({s['key'] for s in new['sites']},set(old_keys))
        self.assertEqual(report['identity']['preserved'],2)
        self.assertEqual(report['identity']['new'],0)
        self.assertTrue(all('[一源/' in s['name'] for s in new['sites']))

    def test_unique_site_parameters_jar_and_api_can_change_without_new_id(self):
        first, manifest, _ = merge([site()])
        for new_site,jar in ((site(ext='changed'), './main.jar'),
                             (site(header={'User-Agent':'new'}),'./main.jar'),
                             (site(), './updated.jar'),
                             (site(api='./new.py'),'./main.jar')):
            with self.subTest(site=new_site,jar=jar):
                new, _, details = merge([new_site],manifest,jar=jar)
                self.assertEqual(first['sites'][0]['key'],new['sites'][0]['key'])
                self.assertEqual(details['identity']['preserved'],1)

    def test_exact_matches_are_reserved_before_changed_duplicate(self):
        first, manifest, _ = merge([site(ext='one'),site(ext='two')])
        new, _, details = merge([site(ext='changed'),site(ext='two')],manifest)
        old_exact = next(s for s in first['sites'] if s['ext']=='two')
        new_exact = next(s for s in new['sites'] if s['ext']=='two')
        changed = next(s for s in new['sites'] if s['ext']=='changed')
        self.assertEqual(old_exact['key'],new_exact['key'])
        self.assertNotEqual(changed['key'],old_exact['key'])
        self.assertEqual(changed['key'],next(s['key'] for s in first['sites'] if s['ext']=='one'))
        self.assertEqual(details['identity']['new'],0)

    def test_ambiguous_same_key_same_api_variants_not_arbitrarily_paired(self):
        first, manifest, _ = merge([site(ext='one'),site(ext='two')])
        new, _, details = merge([site(ext='three'),site(ext='four')],manifest)
        self.assertFalse({s['key'] for s in first['sites']} & {s['key'] for s in new['sites']})
        self.assertEqual(details['identity']['retired'],2)
        self.assertEqual(details['identity']['new'],2)

    def test_duplicate_key_unique_api_anchor_preserves_both_ids(self):
        original = [site(api='csp_One'),site(api='csp_Two')]
        first, manifest, _ = merge(original)
        new, _, details = merge([dict(s,ext='updated') for s in reversed(original)],manifest)
        self.assertEqual({s['api']:s['key'] for s in first['sites']},{s['api']:s['key'] for s in new['sites']})
        self.assertEqual(details['identity']['preserved'],2)

    def test_retired_exact_identity_can_return(self):
        first, manifest, _ = merge([site(),site('keep',api='csp_Keep')])
        _, retired, _ = merge([site('keep',api='csp_Keep')],manifest)
        self.assertEqual(sum(not r['active'] for r in retired['identity_registry']),1)
        returned, _, _ = merge([site(),site('keep',api='csp_Keep')],retired)
        self.assertEqual({s['key'] for s in first['sites']},{s['key'] for s in returned['sites']})

    def test_order_grouping_and_existing_order_survive_upstream_reorder(self):
        original = [site('py',api='./demo.py'),site('api',type=1,api='https://api.example.org/'),site('csp'),site('js',api='./demo.js')]
        first, manifest, _ = merge(original)
        self.assertEqual([row['engine'] for row in manifest['sites']],['API','CSP','JS','Python'])
        new, _, _ = merge(list(reversed(original)),manifest)
        self.assertEqual([s['key'] for s in first['sites']],[s['key'] for s in new['sites']])

    def test_same_group_existing_entries_precede_new(self):
        _, manifest, _ = merge([site('old')])
        new, state, _ = merge([site('new',api='csp_New'),site('old')],manifest)
        self.assertEqual([c['original_key'] for c in state['sites']],['old','new'])

    def test_duplicate_names_short_unique_stable_labels(self):
        first, manifest, _ = merge([site(),site(api='csp_Another')],label='一源')
        new, _, _ = merge([site(ext='updated'),site(api='csp_Another')],manifest,label='一源')
        self.assertEqual(len({s['name'] for s in first['sites']}),2)
        self.assertEqual({s['name'] for s in first['sites']},{s['name'] for s in new['sites']})
        self.assertFalse(any('https://' in s['name'] for s in new['sites']))

    def test_duplicate_trace_points_to_final_preserved_key(self):
        _, manifest, _ = merge([site()])
        manifest['sites'][0]['key'] = manifest['identity_registry'][0]['key'] = 'historic-key'
        tv, _, report = merge([site(),site('alias')],manifest)
        self.assertEqual(tv['sites'][0]['key'],'historic-key')
        self.assertEqual(report['removed_duplicates'][0]['kept_key'],'historic-key')

    def test_registry_only_stores_current_identity_fields(self):
        _, first, _ = merge([site()])
        _, second, _ = merge([site()],first)
        self.assertEqual(first,second)


class ChangeRecordTests(unittest.TestCase):
    def test_complete_change_record_includes_every_item_and_order(self):
        old = {'sites':[{'key':str(i),'name':f'Item {i}','type':1,'api':'https://old.example.org/'} for i in range(100)]}
        current = {'sites':[dict(s,api='https://new.example.org/') for s in reversed(old['sites'][1:])] + [{'key':'new','name':'New','type':1}]}
        result = catalog.compare(old,current)
        self.assertEqual(len(result['updated']),99)
        self.assertEqual(len(result['removed']),1)
        self.assertEqual(len(result['added']),1)
        self.assertEqual(result['removed'][0]['key'],'0')
        self.assertTrue(all('api' in row['changed_fields'] for row in result['updated']))


class CheckRecordTests(unittest.TestCase):
    def test_no_change_retains_generation_but_records_fresh_check(self):
        with tempfile.TemporaryDirectory() as temp:
            old_cwd = Path.cwd()
            os.chdir(temp)
            try:
                specs = [{'id':'a','label':'一源','url':BASE}]
                def load(url):
                    return json.dumps({'sites':[site()],'spider':'./main.jar'}).encode(),url
                first, _ = build.build(specs,'public',loader=load)
                before = Path('public/report.json').read_bytes()
                second, changed = build.build(specs,'public','public',load)
                state = json.loads(Path('build-state.json').read_text(encoding='utf-8'))
                self.assertFalse(changed)
                self.assertEqual(first['version'],second['version'])
                self.assertEqual(before,Path('public/report.json').read_bytes())
                self.assertEqual(state['status'],'success')
                self.assertEqual(state['source_checks'][0]['status'],'ok')
                self.assertEqual(state['verification']['device_playback'],'not tested')
            finally:
                os.chdir(old_cwd)


if __name__ == '__main__':
    unittest.main()
