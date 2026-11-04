"""Merge untrusted FongMi configuration data. Never loads upstream executable code."""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import datetime as dt
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = '1.0.0'
MAX_BYTES = 4 * 1024 * 1024
MAX_CONFIGS = 24
MAX_DEPTH = 3
TIMEOUT = 15
RETRIES = 2
FILE_KEYS = {'api', 'jar', 'spider', 'ext', 'url', 'json', 'token', 'userscript',
             'script', 'file', 'path', 'config', 'rule', 'rules', 'src'}
FILE_EXT = re.compile(r'\.(?:jar|js|mjs|py|so|json|txt|xml|conf|yaml|yml)(?:$|[?;#])', re.I)
RELATIVE = re.compile(r'^(?:\./|\.\./|/(?!/)|(?:jar|js|lib|py|json)/)')
URL_RE = re.compile(r'https?://[^\s"\'<>]+')
TOOLS = {'csp_Push': '推送入口', 'csp_PushShare': '资源分享工具', 'csp_Config': '配置工具',
         'csp_AList': '云盘文件管理', 'csp_Alist': '云盘文件管理'}
TOOL_KEYS = {'push_agent': '推送入口', 'lf_js_search': '搜索辅助页面',
             'lf_js_lf_live': '直播入口', 'lf_js_p2p': '搜索辅助页面'}
PUBLIC_FILES = ('content.json', 'tvbox.json', 'report.json', 'manifest.json', 'report.html', 'index.html', '.nojekyll')


class BuildError(ValueError):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def safe_url(url, resolve=False):
    if not isinstance(url, str) or any(ord(c) < 32 for c in url) or '\\' in url:
        raise BuildError('URL 含控制字符或反斜线')
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise BuildError('配置 URL 必须是无凭据的 HTTP(S) 地址')
    try:
        port = p.port
    except ValueError as exc:
        raise BuildError('URL 端口非法') from exc
    try:
        addresses = [ipaddress.ip_address(p.hostname)]
    except ValueError:
        if resolve:
            addresses = [ipaddress.ip_address(x[4][0]) for x in socket.getaddrinfo(p.hostname, port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)]
        else:
            addresses = []
    if p.hostname.lower() == 'localhost' or p.hostname.lower().endswith('.local') or any(not a.is_global for a in addresses):
        raise BuildError('拒绝抓取内网/本机配置地址')
    return urllib.parse.urlunsplit((p.scheme, p.netloc, urllib.parse.quote(p.path, safe='/%:@!$&\'()*+,;=-._~'),
                                    urllib.parse.quote(p.query, safe='=&%/:?@!$\'()*+,;+-._~'), ''))


class Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = safe_url(newurl, resolve=True)
        if req.full_url.startswith('https://') and target.startswith('http://'):
            raise BuildError('拒绝配置 HTTPS 降级重定向')
        return super().redirect_request(req, fp, code, msg, headers, target)


def decode_json(data):
    if len(data) > MAX_BYTES:
        raise BuildError('配置体积超限')
    last = None
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            text = data.decode(encoding, errors='strict')
            doc = json.loads(text, parse_constant=lambda x: (_ for _ in ()).throw(BuildError('非标准 JSON 常量')))
            if not isinstance(doc, dict):
                raise BuildError('配置必须为 JSON 对象')
            return doc, encoding
        except (UnicodeError, json.JSONDecodeError) as e:
            last = e
    raise BuildError(f'配置编码或 JSON 非法: {type(last).__name__}')


def fetch(url):
    url = safe_url(url, resolve=True)
    opener = urllib.request.build_opener(Redirects())
    for attempt in range(RETRIES):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': f'FongMiConfigAggregator/{VERSION}', 'Accept': 'application/json,text/plain', 'Accept-Encoding': 'identity'})
            with opener.open(request, timeout=TIMEOUT) as response:
                if response.status != 200:
                    raise BuildError(f'HTTP {response.status}')
                if int(response.headers.get('Content-Length', 0)) > MAX_BYTES:
                    raise BuildError('配置体积超限')
                start = time.monotonic()
                pieces, size = [], 0
                while True:
                    piece = response.read(min(65536, MAX_BYTES + 1 - size))
                    if not piece:
                        break
                    size += len(piece)
                    if size > MAX_BYTES or time.monotonic() - start > TIMEOUT * 2:
                        raise BuildError('配置体积或读取时限超限')
                    pieces.append(piece)
                return b''.join(pieces), safe_url(response.url)
        except (OSError, urllib.error.URLError):
            if attempt + 1 == RETRIES:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise BuildError('抓取失败')


def error_reason(exc):
    # Do not include response bodies, headers, cookies or arbitrary upstream error text.
    if isinstance(exc, urllib.error.HTTPError):
        return f'HTTP {exc.code}'
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return '连接/读取超时'
    if isinstance(exc, BuildError):
        return str(exc)[:180]
    return type(exc).__name__


def expand_source(spec, loader=fetch):
    documents, raw, visited, active, attempts = [], {}, set(), set(), []
    count = 0

    def visit(url, depth):
        nonlocal count
        if depth > MAX_DEPTH:
            raise BuildError('多仓递归深度超限')
        url = safe_url(url)
        if url in active:
            raise BuildError('多仓引用成环')
        if url in visited:
            return
        count += 1
        if count > MAX_CONFIGS:
            raise BuildError('多仓配置数量超限')
        active.add(url)
        data, used = loader(url)
        used = safe_url(used)
        if used in active and used != url:
            raise BuildError('多仓重定向成环')
        doc, encoding = decode_json(data)
        record = {'requested_url': url, 'used_url': used, 'fetched_at': now(),
                  'sha256': hashlib.sha256(data).hexdigest(), 'encoding': encoding,
                  'upstream_sites': len(doc.get('sites', [])) if isinstance(doc.get('sites'), list) else 0}
        raw[f'{spec["id"]}-{len(raw):02}.json'] = data
        if 'sites' in doc:
            if not isinstance(doc['sites'], list) or not doc['sites']:
                raise BuildError('sites 必须是非空数组')
            for site in doc['sites']:
                if not isinstance(site, dict) or not isinstance(site.get('key'), str) or not site['key'] or not isinstance(site.get('name'), str) or not site['name'] or not isinstance(site.get('api'), str) or not site['api'] or str(site.get('type')) not in ('0', '1', '2', '3', '4'):
                    raise BuildError('站点结构不完整')
            documents.append((doc, record))
        elif 'urls' in doc:
            if not isinstance(doc['urls'], list) or not doc['urls']:
                raise BuildError('urls 必须是非空数组')
            for item in doc['urls']:
                if not isinstance(item, dict) or not isinstance(item.get('url'), str):
                    raise BuildError('多仓条目缺少 URL')
                visit(urllib.parse.urljoin(used, item['url']), depth + 1)
            documents.append(({'sites': []}, record))
        else:
            raise BuildError('配置缺少 sites 或 urls')
        active.remove(url)
        visited.update((url, used))

    urls = [spec['url']] + spec.get('fallbacks', [])
    for index, url in enumerate(urls):
        try:
            visit(url, 0)
            total = sum(len(doc['sites']) for doc, _ in documents)
            if not total:
                raise BuildError('源展开后无站点')
            return {'source_id': spec['id'], 'requested_url': spec['url'], 'used_url': documents[-1][1]['used_url'],
                    'fallback': index > 0, 'fetched_at': now(), 'upstream_sites': total, 'status': 'ok',
                    'failure_reason': None, 'attempts': attempts, 'documents': [r for _, r in documents],
                    'sha256': documents[-1][1]['sha256']}, documents, raw
        except (ValueError, OSError, urllib.error.URLError) as exc:
            attempts.append({'url': url, 'reason': error_reason(exc)})
            documents.clear(); raw.clear(); visited.clear(); active.clear(); count = 0
    raise BuildError('来源及备用地址全部失败', {'source_id': spec['id'], 'status': 'failed', 'attempts': attempts})


def file_reference(value, base, field='ext'):
    """Only rewrite whole file-reference tokens, never substring replace arbitrary text."""
    if not isinstance(value, str) or not value:
        return value
    if '$$$' in value and field.lower() in FILE_KEYS:
        return '$$$'.join(file_reference(part, base, field) for part in value.split('$$$'))
    if value.lstrip().startswith(('{', '[')):
        # Inline JSON is kept byte-for-byte unless a recognized nested file reference changes.
        try:
            obj = json.loads(value)
            converted = resolve_nested(obj, base)
            return canonical(converted) if obj != converted else value
        except (ValueError, RecursionError):
            return value
    path, sep, suffix = value.partition(';md5;')
    if sep and not re.fullmatch('[0-9a-fA-F]{32}', suffix):
        raise BuildError('文件 md5 校验后缀格式非法')
    if path.startswith(('http://', 'https://')):
        # URL exact spelling and query ordering are behavior-sensitive.
        return value
    known = field.lower() in FILE_KEYS
    candidate = bool(RELATIVE.match(path)) or (known and bool(FILE_EXT.search(path)))
    if not candidate or not known or any(c in path for c in ('\n', '\r', '\\', ' ', '<', '>')):
        return value
    if '${' in path or '{{' in path or '://' in path:
        return value
    target = urllib.parse.urljoin(base, path)
    safe_url(target)
    return target + (sep + suffix if sep else '')


def resolve_nested(value, base, field='ext', depth=0):
    if depth > 32:
        raise BuildError('站点嵌套深度超限')
    if isinstance(value, dict):
        return {k: resolve_nested(v, base, k, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_nested(v, base, field, depth + 1) for v in value]
    if isinstance(value, str):
        return file_reference(value, base, field)
    return value


def engine(site):
    api = site.get('api', '')
    if api.startswith('csp_'):
        return 'CSP'
    path = urllib.parse.urlsplit(api).path.lower()
    if path.endswith('.py'):
        return 'Python'
    if path.endswith(('.js', '.mjs')):
        return 'JS'
    return 'API' if str(site.get('type')) in ('0', '1', '4') else '其他'


def behavior(site):
    # Compare EVERY remaining behavior/UI flag, not just API. Omitted != explicit default.
    return {k: v for k, v in site.items() if k not in ('name', 'key')}


def walk_strings(value, path=''):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from walk_strings(item, f'{path}.{key}' if path else key)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from walk_strings(item, f'{path}[{i}]')
    elif isinstance(value, str):
        yield path, value


def merge(results):
    entries, seen, removed, renames, excluded, globals_report = [], {}, [], [], [], []
    occupied = set()
    for source, documents, _ in results:
        for doc, provenance in documents:
            base = provenance['used_url']
            globals_report.append({'source_id': source['source_id'], 'used_url': base,
                                   'omitted_fields': sorted(k for k in doc if k != 'sites'),
                                   'reason': '主 JAR 按站点继承；其他全局字段不叠加，依赖原全局规则的播放待实机核验'})
            for original in doc['sites']:
                site = resolve_nested(copy.deepcopy(original), base)
                needs_jar = str(site['type']) == '3'
                if needs_jar and not site.get('jar'):
                    if not isinstance(doc.get('spider'), str) or not doc['spider']:
                        raise BuildError('type=3 站点缺少自己的 jar 和来源 spider')
                    site['jar'] = file_reference(doc['spider'], base, 'jar')
                # All sources prefixed even without collision: keys stay stable when sources are added.
                # Source parser-global context is considered even though it is deliberately not merged.
                parser_context = {k: doc[k] for k in ('parses', 'flags', 'rules', 'ijk', 'hosts', 'proxy', 'doh') if k in doc}
                fingerprint = digest({'site': behavior(site), 'parser_context': parser_context})
                context = {'source_id': source['source_id'], 'requested_url': source['requested_url'],
                           'used_url': base, 'original_key': original['key'], 'original_name': original['name'],
                           'type': original['type'], 'engine': engine(site), 'jar': site.get('jar'),
                           'behavior_sha256': fingerprint}
                # Hashing behavior handles duplicate upstream keys deterministically, independently of order.
                stable_key = f'{source["source_id"]}__{original["key"]}__{fingerprint[:12]}'
                if fingerprint in seen:
                    removed.append({'removed': context, 'kept_key': seen[fingerprint]['key'],
                                    'reason': '除 name/key 外全部字段一致，解析器和请求参数一致'})
                    continue
                if stable_key in occupied:
                    raise BuildError('稳定 key 哈希冲突')
                occupied.add(stable_key)
                site['key'] = stable_key
                site['name'] = f'{original["name"]} [{source["source_id"]}/{engine(site)}]'
                renames.append({'source_id': source['source_id'], 'old_key': original['key'], 'new_key': stable_key,
                                'old_name': original['name'], 'new_name': site['name']})
                tool_reason = TOOLS.get(site['api']) or TOOL_KEYS.get(original['key'])
                context.update({'key': stable_key, 'name': site['name'], 'api': site['api'],
                                'status': 'tool_hidden' if tool_reason else 'retained', 'tool_reason': tool_reason})
                if tool_reason:
                    excluded.append({'key': stable_key, 'name': site['name'], 'reason': tool_reason})
                entries.append((site, context))
                seen[fingerprint] = site
    names, variants = {}, {}
    for site, ctx in entries:
        names.setdefault(ctx['original_name'].casefold(), []).append(ctx)
        # Group by equal original key/name/API or host to surface alternate implementations.
        for token in {('key', ctx['original_key'].casefold()), ('name', ctx['original_name'].casefold()),
                      ('api', site['api'])}:
            variants.setdefault(token, []).append(ctx)
    collisions = [{'name': name, 'sites': group} for name, group in names.items() if len(group) > 1]
    kept_variants, variant_seen = [], set()
    for (kind, token), group in variants.items():
        ids = tuple(sorted(c['key'] for c in group))
        if len(ids) > 1 and ids not in variant_seen:
            variant_seen.add(ids)
            jars = sorted({c['jar'] or '' for c in group})
            kept_variants.append({'group_by': kind, 'value': token, 'different_jars': len(jars) > 1, 'sites': group})
    deps, http_refs, local_refs, unresolved = {}, [], [], []
    for site, ctx in entries:
        for path, value in walk_strings(site):
            # Includes inline configuration JSON and parser strings, not just file fields.
            for url in URL_RE.findall(value):
                url = url.rstrip(',;}')
                record = {'key': site['key'], 'field': path, 'url': url}
                if url.startswith('http://'):
                    http_refs.append(record)
                host = urllib.parse.urlsplit(url).hostname or ''
                try:
                    private = not ipaddress.ip_address(host).is_global
                except ValueError:
                    private = host == 'localhost'
                if private:
                    local_refs.append(record)
                is_jar = path == 'jar'
                if is_jar or re.search(r'\.(jar|js|mjs|py|so)(?:$|[?;#])', url, re.I):
                    dep = deps.setdefault(url, {'url': url, 'kind': 'jar' if is_jar else 'script',
                                                'status': '仅记录 URL，未下载、未执行、未审计代码',
                                                'checksum_status': 'md5 格式有效但未下载校验' if ';md5;' in url else '无校验声明', 'used_by': []})
                    dep['used_by'].append({'key': site['key'], 'field': path})
            if RELATIVE.match(value) and path.split('.')[-1].lower() in FILE_KEYS:
                unresolved.append({'key': site['key'], 'field': path, 'reason': '未能可靠识别的路径参数，保留原值'})
    if unresolved:
        raise BuildError('存在未解析的文件路径，拒绝发布')
    tvbox = {'sites': [s for s, _ in entries]}
    content = {'sites': [s for s, c in entries if c['status'] == 'retained']}
    if not content['sites']:
        raise BuildError('内容订阅无站点')
    return tvbox, content, [c for _, c in entries], {
        'removed_duplicates': removed, 'kept_variants': kept_variants, 'name_collisions': collisions,
        'key_renames': renames, 'hidden_tools': excluded, 'global_fields': globals_report,
        'dependencies': list(deps.values()), 'http_references': http_refs, 'local_references': local_refs,
        'unresolved_references': unresolved}


def validate_output(directory):
    directory = Path(directory)
    tvbox = json.loads((directory / 'tvbox.json').read_text(encoding='utf-8'))
    content = json.loads((directory / 'content.json').read_text(encoding='utf-8'))
    report = json.loads((directory / 'report.json').read_text(encoding='utf-8'))
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    for doc in (tvbox, content):
        sites = doc.get('sites')
        if not isinstance(sites, list) or not sites or len({s['key'] for s in sites}) != len(sites):
            raise BuildError('产物站点为空或 key 不唯一')
    counts = report['counts']
    if counts['raw'] != counts['retained'] + counts['removed'] or counts['retained'] != len(tvbox['sites']) or counts['content'] != len(content['sites']) or counts['removed'] != len(report['removed_duplicates']):
        raise BuildError('报告与产物数量不一致')
    if {s['key'] for s in content['sites']} != {s['key'] for s in manifest['sites'] if s['status'] == 'retained'}:
        raise BuildError('内容订阅与清单不一致')
    if {s['key'] for s in tvbox['sites']} != {s['key'] for s in manifest['sites']}:
        raise BuildError('全量订阅与清单不一致')
    if any(s['status'] != 'ok' for s in report['sources']):
        raise BuildError('包含失败来源')
    if counts['raw'] != sum(s['upstream_sites'] for s in report['sources']):
        raise BuildError('来源原始计数不一致')
    if set(report['file_sha256']) != {'tvbox.json', 'content.json', 'manifest.json'}:
        raise BuildError('产物校验清单不完整')
    for name, sha in report['file_sha256'].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != sha:
            raise BuildError('产物哈希不匹配')
    return report


def render_report(report):
    counts = report['counts']
    rows = ''.join(f'<tr><td>{html.escape(s["source_id"])}</td><td>{s["upstream_sites"]}</td><td>{html.escape(s["used_url"])}</td><td>{s["fallback"]}</td></tr>' for s in report['sources'])
    summary = {k: report[k] for k in ('counts', 'hidden_tools', 'removed_duplicates', 'name_collisions', 'kept_variants', 'global_fields')}
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>FongMi 四源聚合检查报告</title>
<style>body{{font:16px system-ui;max-width:1100px;margin:2em auto;padding:1em;line-height:1.7}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #bbb;padding:.5em;overflow-wrap:anywhere}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}a{{color:#1465b0}}</style>
<h1>FongMi 四源聚合检查报告</h1><p>版本 {html.escape(report['version'])} · 最近成功生成 {report['last_success_at']}</p>
<p>原始 {counts['raw']}，精确去重 {counts['removed']}，全量 {counts['retained']}，内容入口 {counts['content']}。</p>
<p><a href="content.json">默认订阅 content.json</a> · <a href="tvbox.json">全量 tvbox.json</a> · <a href="report.json">详细报告</a> · <a href="manifest.json">来源清单</a></p>
<p>仅配置聚合，未执行第三方 JAR/JS/Python，未完成电视首页、搜索或播放测试。内容优先不代表安全审计。全局 hosts/proxy/doh/rules/ijk 等未合并，依赖这些全局设置的站点可能需要进一步适配。本站若发布到 Pages 将公开可读。</p>
<table><tr><th>来源</th><th>站点</th><th>本次使用地址</th><th>备用源</th></tr>{rows}</table>
<p>未审计远程可执行依赖 {len(report['dependencies'])} 个，HTTP 明文引用 {len(report['http_references'])} 处。所有依赖仅记录 URL；不是存活检测或安全认可。失败/最近检查时间请查看 Actions，本站保留最近成功版本。</p>
<details><summary>查看去重、备用实现与全局字段详情</summary><pre>{html.escape(json.dumps(summary,ensure_ascii=False,indent=2))}</pre></details></html>'''


def build(specs, output, last_good=None, loader=fetch):
    ids = [s['id'] for s in specs]
    if not specs or len(set(ids)) != len(ids) or any(not re.fullmatch('[a-z0-9_-]+', sid) for sid in ids):
        raise BuildError('来源 ID 为空、重复或非法')
    results, failures = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(expand_source, spec, loader) for spec in specs]
        for spec, job in zip(specs, jobs):
            try:
                results.append(job.result())
            except Exception as exc:
                failures.append({'source_id': spec['id'], 'failure_reason': error_reason(exc),
                                 'details': getattr(exc, 'details', None)})
    if failures:
        save_json('failure.json', {'checked_at': now(), 'status': 'failed', 'failures': failures,
                                  'last_good_untouched': True})
        raise BuildError('至少一个源失败；保留最近成功产物，不生成残缺订阅', failures)
    tvbox, content, manifest, details = merge(results)
    previous = validate_output(last_good) if last_good and Path(last_good).is_dir() else None
    if previous:
        counts = {s['source_id']: s['upstream_sites'] for s in previous['sources']}
        for source, _, _ in results:
            old_count = counts.get(source['source_id'], 0)
            if old_count and source['upstream_sites'] < old_count * 0.7:
                raise BuildError('单源站点较最近成功版本减少超过 30%，拒绝自动发布，请审查上游变化')
    code_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    version = digest({'generator': code_hash, 'tvbox': tvbox, 'content': content, 'manifest': manifest})
    changed = previous is None or previous['version'] != version
    state = {'changed': changed, 'version': version, 'checked_at': now()}
    save_json('build-state.json', state)
    if not changed:
        return previous, False
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.stage-', dir=output.parent) as temp:
        stage = Path(temp) / 'public'
        stage.mkdir()
        for name, value in [('tvbox.json', tvbox), ('content.json', content), ('manifest.json', {'version': version, 'sites': manifest})]:
            save_json(stage / name, value)
        report = {'schema_version': 1, 'generator_version': VERSION, 'version': version,
                  'commit': os.environ.get('GITHUB_SHA', 'local'), 'generated_at': now(), 'last_success_at': now(),
                  'status': 'success', 'sources': [s for s, _, _ in results],
                  'counts': {'raw': sum(s['upstream_sites'] for s, _, _ in results),
                             'removed': len(details['removed_duplicates']), 'retained': len(tvbox['sites']),
                             'content': len(content['sites']), 'hidden_tools': len(details['hidden_tools']),
                             'different_jar_variant_groups': sum(g['different_jars'] for g in details['kept_variants']),
                             'name_collision_groups': len(details['name_collisions'])},
                  **details,
                  'verification': {'configuration': 'validated', 'dependencies': 'URL inventory only; not fetched or executed',
                                   'device_home_category_search': 'not tested', 'playback': 'not tested'},
                  'file_sha256': {n: hashlib.sha256((stage / n).read_bytes()).hexdigest() for n in ('tvbox.json', 'content.json', 'manifest.json')}}
        save_json(stage / 'report.json', report)
        (stage / 'report.html').write_text(render_report(report), encoding='utf-8')
        (stage / 'index.html').write_text(render_report(report), encoding='utf-8')
        (stage / '.nojekyll').touch()
        validate_output(stage)
        raw_stage = Path(temp) / 'raw'
        raw_stage.mkdir()
        raw_hashes = {}
        for _, _, raws in results:
            for name, data in raws.items():
                (raw_stage / name).write_bytes(data)
                raw_hashes[name] = hashlib.sha256(data).hexdigest()
        save_json(raw_stage / 'checksums.json', raw_hashes)
        # This directory is local / private Actions artifact; never included in Pages artifact.
        cache = Path('.snapshots')
        cache.mkdir(exist_ok=True)
        target_raw = cache / version
        if not target_raw.exists():
            shutil.copytree(raw_stage, target_raw)
        # Validation completed before touching current public; restore on a rename failure.
        backup = output.with_name(output.name + '.previous')
        if backup.exists():
            raise BuildError('存在未处理的 public.previous，需人工核查')
        if output.exists():
            output.rename(backup)
        try:
            stage.rename(output)
        except OSError:
            if backup.exists():
                backup.rename(output)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return report, True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sources', default='sources.json')
    parser.add_argument('--output', default='public')
    parser.add_argument('--last-good')
    parser.add_argument('--validate')
    args = parser.parse_args()
    try:
        if args.validate:
            report = validate_output(args.validate)
            print(canonical(report['counts']))
            return 0
        config = json.loads(Path(args.sources).read_text(encoding='utf-8'))
        report, changed = build(config['sources'], args.output, args.last_good)
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
                f.write(f'changed={str(changed).lower()}\nversion={report["version"]}\n')
        print(canonical({'changed': changed, 'version': report['version'], 'counts': report['counts']}))
        return 0
    except Exception as exc:
        save_json('failure.json', {'checked_at': now(), 'status': 'failed', 'reason': error_reason(exc),
                                  'failures': getattr(exc, 'details', None), 'last_good_untouched': True})
        print('BUILD FAILED:', error_reason(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
