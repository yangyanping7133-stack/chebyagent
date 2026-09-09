#!/usr/bin/env python3
"""Copy this checkout's non-ignored source files into a new reviewable snapshot."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


MANIFEST_NAME = 'SOURCE_SNAPSHOT.json'


def git(*args):
    return subprocess.check_output(['git', *args])


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_snapshot(output):
    output = output.absolute()
    if output.is_symlink() or not output.is_dir():
        raise ValueError('Snapshot root must be a real directory')
    output = output.resolve()
    manifest_path = output / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError('Snapshot manifest is missing or unsafe')
    document = json.loads(manifest_path.read_text())
    records = document.get('files')
    if not isinstance(records, list):
        raise ValueError('Snapshot manifest has no file list')
    expected = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get('path'), str):
            raise ValueError('Snapshot contains an invalid file record')
        relative = Path(record['path'])
        if relative.is_absolute() or '..' in relative.parts or str(relative) == MANIFEST_NAME:
            raise ValueError('Snapshot manifest contains an unsafe path')
        if str(relative) in expected:
            raise ValueError('Snapshot manifest contains a duplicate path')
        expected.add(str(relative))
        path = output / relative
        if path.is_symlink() or not path.is_file() or output not in path.resolve().parents:
            raise ValueError('Snapshot file is missing or unsafe: ' + str(relative))
        if path.stat().st_size != record.get('bytes'):
            raise ValueError('Snapshot file size mismatch: ' + str(relative))
        if file_sha256(path) != record.get('sha256'):
            raise ValueError('Snapshot file hash mismatch: ' + str(relative))
        if oct(path.stat().st_mode & 0o777) != record.get('mode'):
            raise ValueError('Snapshot file mode mismatch: ' + str(relative))
    actual = set()
    for path in output.rglob('*'):
        if path.is_symlink():
            raise ValueError('Snapshot contains a symlink: ' + str(path.relative_to(output)))
        if path.is_file():
            relative = str(path.relative_to(output))
            if relative != MANIFEST_NAME:
                actual.add(relative)
    if actual != expected:
        raise ValueError('Snapshot file set differs from its manifest')
    return {
        'files_verified': len(expected),
        'source_head': document.get('source_head'),
        'source_branch': document.get('source_branch'),
        'output': str(output),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify_snapshot(args.output)))
        return
    root = Path(git('rev-parse', '--show-toplevel').decode().strip()).resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    names = git('ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')
    records = []
    for name in sorted(set(names)):
        if not name:
            continue
        relative = Path(name.decode())
        source = root / relative
        if not source.exists() and not source.is_symlink():
            continue  # Preserve working-tree deletions.
        if source.is_symlink() or not source.is_file():
            raise ValueError('Source snapshot requires reviewed regular files: ' + str(relative))
        if root not in source.resolve().parents or output in source.resolve().parents:
            raise ValueError('Source path escaped checkout or recursed into snapshot')
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        digest = file_sha256(source)
        if file_sha256(target) != digest:
            raise ValueError('Snapshot copy mismatch')
        records.append({'path': str(relative), 'sha256': digest, 'bytes': source.stat().st_size,
                        'mode': oct(source.stat().st_mode & 0o777)})
    record = {'source_head': git('rev-parse', 'HEAD').decode().strip(),
              'source_branch': git('branch', '--show-current').decode().strip(),
              'boundary': 'Current source snapshot including uncommitted edits; '
                          'ignored private assets and credentials excluded. Not final product acceptance.',
              'files': records}
    (output / MANIFEST_NAME).write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({'files_copied_and_verified': len(records), 'output': str(output)}))


if __name__ == '__main__':
    main()
