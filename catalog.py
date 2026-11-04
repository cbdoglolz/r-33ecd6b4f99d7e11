"""Presentation and durable identities for a generic configuration catalog."""
from collections import Counter, defaultdict
import copy

ENGINE_ORDER = {'API': 0, 'CSP': 1, 'JS': 2, 'Python': 3, '其他': 4}


def organize(entries, previous_manifest=None):
    """Preserve identities using exact matches first; never pair ambiguous duplicates."""
    previous_manifest = previous_manifest or {}
    previous_sites = previous_manifest.get('sites', [])
    registry = copy.deepcopy(previous_manifest.get('identity_registry', previous_sites))
    previous_by_key = {row['key']: row for row in registry}
    if len(previous_by_key) != len(registry):
        raise ValueError('历史站点标识清单有重复 key')
    groups = defaultdict(list)
    current = defaultdict(list)
    for row in registry:
        row.setdefault('active', True)
        groups[row['source_id'], row['original_key']].append(row)
    for entry in entries:
        current[entry[1]['source_id'], entry[1]['original_key']].append(entry)
    assigned, claimed, events = {}, set(), []

    def assign(entry, old, reason):
        site, context = entry
        draft = site['key']
        assigned[draft] = old['key']
        claimed.add(old['key'])
        events.append({'key': old['key'], 'source_id': context['source_id'],
                       'original_key': context['original_key'], 'method': reason,
                       'behavior_changed': old.get('behavior_sha256') != context['behavior_sha256']})

    # Reserve ALL exact matches before handling any changed configuration.
    for group, items in current.items():
        for entry in items:
            context = entry[1]
            exact = [r for r in groups[group] if r['key'] not in claimed and
                     r.get('behavior_sha256') == context['behavior_sha256']]
            if len(exact) == 1:
                assign(entry, exact[0], 'exact_behavior')

    for group, items in current.items():
        pending = [e for e in items if e[0]['key'] not in assigned]
        available = [r for r in groups[group] if r['key'] not in claimed and r['active']]
        # A duplicate upstream key is only matched by API/engine if the pair is unique.
        old_counts = Counter((r.get('api'), r.get('engine')) for r in available)
        new_counts = Counter((e[1].get('api'), e[1].get('engine')) for e in pending)
        for entry in pending:
            anchor = entry[1].get('api'), entry[1].get('engine')
            candidates = [r for r in available if r['key'] not in claimed and
                          (r.get('api'), r.get('engine')) == anchor]
            if old_counts[anchor] == new_counts[anchor] == len(candidates) == 1:
                assign(entry, candidates[0], 'unique_api_and_engine')
        pending = [e for e in pending if e[0]['key'] not in assigned]
        available = [r for r in available if r['key'] not in claimed]
        # A single logical upstream key may change its API/JAR/parameters without losing identity.
        if len(items) == 1 and len(pending) == len(available) == 1:
            assign(pending[0], available[0], 'unique_source_key')

    key_map = {}
    for site, context in entries:
        draft = site['key']
        key = assigned.get(draft, draft)
        if draft not in assigned:
            if key in previous_by_key or key in claimed:
                raise ValueError('新站点标识与历史记录冲突，拒绝猜测对应关系')
            claimed.add(key)
            events.append({'key': key, 'source_id': context['source_id'],
                           'original_key': context['original_key'],
                           'method': 'new_or_ambiguous', 'behavior_changed': False})
        key_map[draft] = key
        site['key'] = context['key'] = key
        label = context.get('source_label') or context['source_id']
        site['name'] = f'{context["original_name"].strip()} [{label}/{context["engine"]}]'
        context['name'] = site['name']

    # Keep disambiguation short and stable; don't put full API URLs on the TV screen.
    names = defaultdict(list)
    for entry in entries:
        names[entry[0]['name']].append(entry)
    for group in names.values():
        if len(group) > 1:
            for site, context in group:
                site['name'] += ' #' + site['key'][-6:]
                context['name'] = site['name']

    source_order = {sid: i for i, sid in enumerate(dict.fromkeys(c['source_id'] for _, c in entries))}
    old_positions = {row['key']: i for i, row in enumerate(previous_sites)}
    positions = {site['key']: i for i, (site, _) in enumerate(entries)}
    entries.sort(key=lambda e: (source_order[e[1]['source_id']], ENGINE_ORDER[e[1]['engine']],
                                old_positions.get(e[0]['key'], len(old_positions) + positions[e[0]['key']])))

    # Tombstones prevent retired IDs from being silently reassigned and permit exact reappearance.
    for row in registry:
        row['active'] = False
    registry_by_key = {row['key']: row for row in registry}
    fields = ('key', 'source_id', 'original_key', 'api', 'engine', 'behavior_sha256')
    for _, context in entries:
        row = {k: context[k] for k in fields}
        row['active'] = True
        registry_by_key[row['key']] = row
    registry = sorted(registry_by_key.values(), key=lambda row: row['key'])
    old_active = {r['key'] for r in previous_manifest.get('identity_registry', previous_sites) if r.get('active', True)}
    active = {s['key'] for s, _ in entries}
    summary = {'preserved': sum(e['method'] != 'new_or_ambiguous' for e in events),
               'new': sum(e['method'] == 'new_or_ambiguous' for e in events),
               'retired': len(old_active - active), 'events': events,
               'ordering': '来源优先级 → API/CSP/JS/Python → 原有顺序；新入口追加到同组',
               'note': '配置标识稳定性不等于播放器收藏或实际播放已验收'}
    return entries, registry, key_map, summary


def compare(previous, current, previous_manifest=None, current_manifest=None):
    """An exhaustive item-level release record, including display/order changes."""
    old_sites = (previous or {}).get('sites', [])
    new_sites = current['sites']
    old = {s['key']: s for s in old_sites}
    new = {s['key']: s for s in new_sites}
    old_order = {s['key']: i for i, s in enumerate(old_sites)}
    new_order = {s['key']: i for i, s in enumerate(new_sites)}
    contexts = {s['key']: s for s in (previous_manifest or {}).get('sites', [])}
    contexts.update({s['key']: s for s in current_manifest or []})

    def item(site):
        return {'key': site['key'], 'name': site['name'], 'type': site['type'],
                'source_id': contexts.get(site['key'], {}).get('source_id')}

    result = {'added': [item(s) for s in new_sites if s['key'] not in old],
              'removed': [item(s) for s in old_sites if s['key'] not in new], 'updated': []}
    for site in new_sites:
        key = site['key']
        if key not in old:
            continue
        fields = sorted(k for k in set(site) | set(old[key]) if site.get(k) != old[key].get(k))
        if old_order[key] != new_order[key]:
            fields.append('order')
        if fields:
            result['updated'].append({**item(site), 'previous_name': old[key]['name'],
                                      'changed_fields': fields, 'previous_position': old_order[key],
                                      'position': new_order[key]})
    return result
