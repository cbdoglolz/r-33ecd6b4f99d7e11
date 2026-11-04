"""Read-only live verification. Never request upstream video bodies or run scripts."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build import MAX_BYTES, PUBLIC_FILES, BuildError, save_json, validate_output
import tempfile


def verify(base, expected_version=None):
    results = []
    with tempfile.TemporaryDirectory() as temp:
        for name in ('content.json', 'tvbox.json', 'report.json', 'manifest.json'):
            url = base.rstrip('/') + '/' + name
            request = urllib.request.Request(url, headers={'User-Agent': 'FongMiConfigAggregator/1.0', 'Cache-Control': 'no-cache'})
            with urllib.request.urlopen(request, timeout=20) as response:
                content_type = response.headers.get('Content-Type', '').split(';')[0]
                data = response.read(MAX_BYTES * 8 + 1)
                if response.status != 200 or content_type not in ('application/json', 'text/plain') or len(data) > MAX_BYTES * 8:
                    raise BuildError('Pages HTTP 状态、类型或体积异常')
                json.loads(data.decode('utf-8'))
                Path(temp, name).write_bytes(data)
                results.append({'url': url, 'status': response.status, 'content_type': content_type, 'bytes': len(data),
                                'sha256': hashlib.sha256(data).hexdigest()})
        report = validate_output(temp)
        if expected_version and report['version'] != expected_version:
            raise BuildError('Pages 尚未返回期望内容版本')
        return {'version': report['version'], 'counts': report['counts'], 'http_checks': results,
                'device_playback': '未测试'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('base')
    parser.add_argument('--version')
    parser.add_argument('--output', default='pages-verification.json')
    args = parser.parse_args()
    result = verify(args.base, args.version)
    save_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
