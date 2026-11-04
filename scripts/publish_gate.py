"""Retry pending content after a failed deployment without publishing timestamp-only checks."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build import BuildError, MAX_BYTES, save_json, validate_output


def decide(changed, enabled, output='public', last_good='.last-good/public', page_url=None):
    if not enabled:
        return {'publish_needed': False, 'reason': 'Pages disabled'}
    if changed:
        report = validate_output(output)
        return {'publish_needed': True, 'reason': 'new content or explicit restore', 'version': report['version']}
    report = validate_output(last_good)
    expected = report['version']
    if not page_url or not page_url.startswith('https://'):
        raise BuildError('缺少 HTTPS Pages 地址，无法确认已发布版本')
    request = urllib.request.Request(page_url.rstrip('/') + '/report.json',
                                     headers={'User-Agent':'ConfigCheck/1.1', 'Cache-Control':'no-cache'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise BuildError('无法确认已发布版本的 HTTP 状态')
            data = response.read(MAX_BYTES * 8 + 1)
            if len(data) > MAX_BYTES * 8:
                raise BuildError('已发布版本报告体积超限')
            remote = json.loads(data.decode('utf-8'))
            current = remote.get('version')
            if not isinstance(current, str) or not re.fullmatch('[0-9a-f]{64}', current):
                raise BuildError('已发布版本报告格式异常')
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise BuildError(f'已发布版本检查 HTTP {exc.code}；保留当前 Pages，稍后重试') from exc
        current = None
    except (OSError, ValueError) as exc:
        raise BuildError('无法可靠读取已发布版本；保留当前 Pages，稍后重试') from exc
    if current == expected:
        return {'publish_needed': False, 'reason': 'same content already published', 'version': expected}
    if Path(output).exists():
        raise BuildError('发布恢复目录已存在，拒绝覆盖不明输出')
    shutil.copytree(last_good, output)
    validate_output(output)
    return {'publish_needed': True, 'reason': 'complete snapshot not yet published',
            'version': expected, 'previous_published_version': current}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='public')
    parser.add_argument('--last-good', default='.last-good/public')
    args = parser.parse_args()
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        parser.error('缺少有效 GITHUB_REPOSITORY')
    owner, name = repo.split('/')
    result = decide(os.environ.get('CONFIG_CHANGED') == 'true', os.environ.get('PAGES_ENABLED') == 'true',
                    args.output, args.last_good, f'https://{owner}.github.io/{name}/')
    save_json('publication-decision.json', result)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
            f.write('publish_needed=' + str(result['publish_needed']).lower() + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
