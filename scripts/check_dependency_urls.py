"""HEAD-only spot checks. No executable bodies are downloaded or evaluated."""
import concurrent.futures
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build import Redirects, error_reason, now, safe_url, save_json


def check(dep):
    url = dep['url'].split(';md5;')[0]
    try:
        url = safe_url(url, resolve=True)
        request = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'FongMiConfigAggregator/1.0'})
        with urllib.request.build_opener(Redirects()).open(request, timeout=12) as response:
            final = urllib.parse.urlsplit(response.url)
            # Redirect targets may contain temporary bearer signatures. Never log their query strings.
            public_final = urllib.parse.urlunsplit((final.scheme, final.netloc, final.path, '', ''))
            return {'url': dep['url'], 'status': response.status, 'used_url_without_query': public_final,
                    'content_type': response.headers.get('Content-Type'), 'method': 'HEAD', 'code_audited': False}
    except Exception as exc:
        return {'url': dep['url'], 'error': error_reason(exc), 'method': 'HEAD', 'code_audited': False}


def main():
    report = json.loads(Path('public/report.json').read_text(encoding='utf-8'))
    deps = report['dependencies']
    selected = [d for d in deps if d['kind'] == 'jar']
    for sid in ('clun', 'wang', 'qist', 'pray'):
        for ext in ('.py', '.js'):
            item = next((d for d in deps if d['url'].split(';')[0].endswith(ext) and any(u['key'].startswith(sid + '__') for u in d['used_by'])), None)
            if item and item not in selected:
                selected.append(item)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        checks = list(pool.map(check, selected))
    result = {'checked_at': now(), 'selection': '所有直接 JAR URL + 每源各一个 Python/JS URL（如有）', 'checks': checks,
              'note': '仅 HEAD 响应，不代表脚本可运行、校验哈希匹配或电视可播放'}
    Path('diagnostics').mkdir(exist_ok=True)
    save_json('diagnostics/dependency-url-checks.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
