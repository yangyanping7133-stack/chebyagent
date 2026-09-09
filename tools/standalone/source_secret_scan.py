#!/usr/bin/env python3
"""Redacted secret-candidate scan of this checkout and its HEAD-reachable blobs."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


RULES = {
    'private_key': re.compile(rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----'),
    'github_token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'provider_token': re.compile(rb'\bsk-[A-Za-z0-9_-]{24,}\b'),
    # Z.AI user keys are commonly an identifier and secret separated by one dot.
    # Keep this shape-specific so ordinary 32-character hashes do not match.
    'provider_token_dotted': re.compile(
        rb'(?<![A-Za-z0-9_-])[0-9a-fA-F]{32}\.[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])'
    ),
    'literal_credential': re.compile(
        rb'["\x27](?:apiKey|api_key|access_token|refresh_token|password)["\x27]\s*:\s*'
        rb'["\x27][A-Za-z0-9_./+=:-]{16,}["\x27]', re.I),
}
SENSITIVE_NAME = re.compile(r'(?:^|/)(?:auth\.json|id_rsa|id_ed25519|[^/]*\.(?:p12|jks|keystore|pass))$', re.I)


def git(*args):
    return subprocess.check_output(['git', *args])


def snapshot_files(root):
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Snapshot root must be a real directory')
    root = root.resolve()
    paths = []
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('Snapshot contains a symlink: ' + str(path.relative_to(root)))
        if path.is_file():
            paths.append(path)
    return root, sorted(paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--snapshot-root', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Refusing to overwrite a scan report')
    findings = []
    scanned = {'working_files': 0, 'history_blobs': 0, 'bytes': 0}

    def check(data, origin, path, oid=None):
        if SENSITIVE_NAME.search(path):
            findings.append({'origin': origin, 'path': path, 'rule': 'sensitive_filename', 'object': oid})
        for name, pattern in RULES.items():
            for match in pattern.finditer(data):
                findings.append({'origin': origin, 'path': path, 'object': oid, 'rule': name,
                                 'line': data.count(b'\n', 0, match.start()) + 1})
        scanned['bytes'] += len(data)

    if args.snapshot_root:
        snapshot_root, paths = snapshot_files(args.snapshot_root)
        output = args.output.absolute().resolve()
        if output == snapshot_root or snapshot_root in output.parents:
            raise ValueError('Scan report must be outside the snapshot')
        for path in paths:
            check(path.read_bytes(), 'source_snapshot', str(path.relative_to(snapshot_root)))
            scanned['working_files'] += 1
        snapshot_manifest = json.loads((snapshot_root / 'SOURCE_SNAPSHOT.json').read_text())
        head = snapshot_manifest.get('source_head')
        boundary = ('Candidate patterns only; no matched values are emitted. This scan covers the '
                    'verified source snapshot file tree, not ignored release assets, APK contents, '
                    'nested archives outside the snapshot, or later commits.')
    else:
        names = git('ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')
        for name in sorted(set(names)):
            if not name:
                continue
            path = Path(name.decode())
            if path.is_symlink() or not path.is_file():
                continue
            check(path.read_bytes(), 'working_tree', str(path))
            scanned['working_files'] += 1

        # Restrict to this HEAD. Other branches/worktrees may belong to the distributed project.
        objects = git('rev-list', '--objects', 'HEAD').splitlines()
        process = subprocess.Popen(['git', 'cat-file', '--batch'], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE)
        for row in objects:
            fields = row.split(b' ', 1)
            oid = fields[0]
            process.stdin.write(oid + b'\n')
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3:
                raise ValueError('Cannot read Git object')
            size = int(header[2])
            data = process.stdout.read(size)
            if len(data) != size or process.stdout.read(1) != b'\n':
                raise ValueError('Incomplete Git object stream')
            if header[1] == b'blob':
                path = fields[1].decode(errors='replace') if len(fields) > 1 else '(unnamed)'
                check(data, 'HEAD_history', path, oid.decode())
                scanned['history_blobs'] += 1
        process.stdin.close()
        if process.wait() != 0:
            raise ValueError('Git object reader failed')
        head = git('rev-parse', 'HEAD').decode().strip()
        boundary = ('Candidate patterns only; no matched values are emitted. Ignored release assets, '
                    'nested archives, and final delivery contents need separate scans.')
    report = {'head': head, 'counts': scanned,
              'findings': findings,
              'boundary': boundary,
              'outcome': 'REVIEW' if findings else 'NO_CANDIDATES_FOUND'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as output:
        json.dump(report, output, indent=2)
        output.write('\n')
    print(json.dumps({'counts': scanned, 'candidate_count': len(findings),
                      'outcome': report['outcome'], 'report': str(args.output)}))


if __name__ == '__main__':
    main()
