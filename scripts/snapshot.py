"""Recover/persist complete last-good snapshots in the dedicated snapshots Git branch."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build import PUBLIC_FILES, BuildError, save_json, validate_output


def api(path, method='GET', payload=None, raw=False):
    cmd = ['gh', 'api', path, '--method', method]
    if raw:
        cmd += ['-H', 'Accept: application/vnd.github.raw+json']
    if payload is not None:
        cmd += ['--input', '-']
    result = subprocess.run(cmd, input=json.dumps(payload).encode() if payload is not None else None,
                            capture_output=True, check=False)
    if result.returncode:
        # Preserve HTTP code only. Never copy credentials / arbitrary API response bodies into reports.
        if b'(HTTP 404)' in result.stderr:
            raise FileNotFoundError(path)
        raise BuildError(f'GitHub API 请求失败: {method} {path.split("?")[0]}')
    return result.stdout if raw else json.loads(result.stdout or b'{}')


def get_ref(repo, ref):
    if ref != 'snapshots' and (len(ref) != 40 or any(c not in '0123456789abcdef' for c in ref)):
        raise BuildError('恢复来源必须为 snapshots 或明确的 40 位快照提交 SHA')
    if ref != 'snapshots':
        return ref
    try:
        return api(f'repos/{repo}/git/ref/heads/snapshots')['object']['sha']
    except FileNotFoundError:
        return None


def recover(repo, directory, ref='snapshots'):
    sha = get_ref(repo, ref)
    if sha is None:
        print('首次构建：不存在 last-good；必须完整读取所有源后才能发布。')
        return False
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # Pin every download to the same immutable commit, never a moving branch reference.
    for name in PUBLIC_FILES:
        data = api(f'repos/{repo}/contents/public/{name}?ref={sha}', raw=True)
        (directory / name).write_bytes(data)
    report = validate_output(directory)
    meta = json.loads(api(f'repos/{repo}/contents/snapshot.json?ref={sha}', raw=True))
    if report['version'] != meta['version']:
        raise BuildError('快照元数据版本不符')
    if set(meta['public_checksums']) != set(PUBLIC_FILES):
        raise BuildError('快照文件校验清单不完整')
    for name, expected in meta['public_checksums'].items():
        if name not in PUBLIC_FILES or hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise BuildError('快照文件校验失败')
    save_json(directory.parent / 'recovered-snapshot.json', {'commit': sha, 'version': report['version']})
    print(f'已恢复 last-good 快照 {sha} / {report["version"]}')
    return True


def persist(repo, directory='public'):
    directory = Path(directory)
    report = validate_output(directory)
    parent = get_ref(repo, 'snapshots')
    if parent:
        old = api(f'repos/{repo}/contents/snapshot.json?ref={parent}')
        previous = json.loads(base64.b64decode(old['content']))
        if previous['version'] == report['version']:
            print('内容版本未变，不创建快照提交。')
            return parent
    raw_dir = Path('.snapshots') / report['version']
    raw_checksums = json.loads((raw_dir / 'checksums.json').read_text(encoding='utf-8'))
    if not raw_checksums:
        raise BuildError('缺少成功原始配置快照')
    files = {}
    for name, expected in raw_checksums.items():
        if Path(name).name != name:
            raise BuildError('非法快照文件名')
        data = (raw_dir / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise BuildError('原始配置快照哈希不符')
        files[f'raw/{name}'] = data
    files['raw/checksums.json'] = (raw_dir / 'checksums.json').read_bytes()
    public_checksums = {}
    for name in PUBLIC_FILES:
        files[f'public/{name}'] = (directory / name).read_bytes()
        public_checksums[name] = hashlib.sha256(files[f'public/{name}']).hexdigest()
    files['snapshot.json'] = json.dumps({'version': report['version'], 'source_commit': report['commit'],
                                        'generated_at': report['generated_at'], 'public_checksums': public_checksums}, ensure_ascii=False, indent=2).encode()
    tree = []
    for path, data in files.items():
        blob = api(f'repos/{repo}/git/blobs', 'POST', {'content': base64.b64encode(data).decode(), 'encoding': 'base64'})
        tree.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
    tree_sha = api(f'repos/{repo}/git/trees', 'POST', {'tree': tree})['sha']
    commit = api(f'repos/{repo}/git/commits', 'POST', {'message': f'Complete configuration snapshot {report["version"][:16]}',
                                                   'tree': tree_sha, 'parents': [parent] if parent else []})['sha']
    if parent:
        api(f'repos/{repo}/git/refs/heads/snapshots', 'PATCH', {'sha': commit, 'force': False})
    else:
        api(f'repos/{repo}/git/refs', 'POST', {'ref': 'refs/heads/snapshots', 'sha': commit})
    print(f'完整快照已保存：{commit}')
    return commit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['recover', 'persist'])
    parser.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY'))
    parser.add_argument('--directory')
    parser.add_argument('--ref', default='snapshots')
    args = parser.parse_args()
    if not args.repo:
        parser.error('缺少 --repo 或 GITHUB_REPOSITORY')
    if args.action == 'recover':
        recover(args.repo, args.directory or '.last-good/public', args.ref)
    else:
        persist(args.repo, args.directory or 'public')


if __name__ == '__main__':
    main()
