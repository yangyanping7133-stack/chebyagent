#!/usr/bin/env python3
"""Save pinned upstream Codex sources and license evidence without executing them."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
from urllib.request import Request, urlopen


def fetch(url, destination, limit):
    request = Request(url, headers={'User-Agent': 'ChebyAgent-source-inventory'})
    digest = hashlib.sha256()
    total = 0
    with urlopen(request, timeout=60) as response, destination.open('xb') as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError('Source download exceeded its size limit')
            output.write(chunk)
            digest.update(chunk)
    return {'url': url, 'path': destination.name, 'bytes': total,
            'sha256': digest.hexdigest()}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def safe_relative(value):
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or '..' in path.parts or '.' in path.parts:
        raise ValueError('Unsafe relative path')
    return path


def is_evidence_path(relative):
    name = relative.name.lower()
    return (name.startswith(('license', 'licence', 'copying', 'notice'))
            or name in ('cargo.lock', 'cargo.toml', 'package.json',
                        'pnpm-lock.yaml', 'package-lock.json')
            or 'third_party' in relative.parts and name.endswith('.md'))


def verify_file(output, record):
    path = safe_relative(record.get('path', ''))
    target = output / Path(*path.parts)
    if not target.is_file() or target.is_symlink():
        raise ValueError('Missing or unsafe collected file: ' + path.as_posix())
    data = target.read_bytes()
    if len(data) != record.get('bytes') or sha256(data) != record.get('sha256'):
        raise ValueError('Collected file digest mismatch: ' + path.as_posix())
    return data


def verify_reference_chain(version, commit, references, output):
    if not isinstance(references, list) or not references:
        raise ValueError('Codex reference chain is missing')
    expected_object = None
    for index, record in enumerate(references):
        if record.get('path') != f'reference-{index}.json':
            raise ValueError('Codex reference path sequence mismatch')
        document = json.loads(verify_file(output, record))
        if index == 0:
            if document.get('ref') != 'refs/tags/rust-v' + version:
                raise ValueError('Codex tag reference mismatch')
        elif document.get('sha') != expected_object:
            raise ValueError('Codex annotated tag chain mismatch')
        obj = document.get('object', {})
        object_id = obj.get('sha', '')
        object_type = obj.get('type')
        if not re.fullmatch(r'[0-9a-f]{40}', object_id):
            raise ValueError('Invalid Codex upstream object id')
        if object_type == 'commit':
            if index != len(references) - 1 or object_id != commit:
                raise ValueError('Codex commit reference mismatch')
            return
        if object_type != 'tag':
            raise ValueError('Unexpected Codex tag target')
        expected_object = object_id
    raise ValueError('Codex reference chain does not end in a commit')


def archive_evidence(archive_data, commit):
    selected = []
    expected_root = 'codex-' + commit
    with tarfile.open(fileobj=io.BytesIO(archive_data), mode='r:gz') as source:
        for member in source:
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or '..' in member_path.parts:
                raise ValueError('Unsafe archive member')
            if not member_path.parts or member_path.parts[0] != expected_root:
                raise ValueError('Codex archive commit root mismatch')
            if len(member_path.parts) < 2 or not member.isfile():
                continue
            relative = PurePosixPath(*member_path.parts[1:])
            if not is_evidence_path(relative):
                continue
            if member.size > 8 * 1024 * 1024:
                raise ValueError('Evidence member exceeded its size limit')
            data = source.extractfile(member).read()
            selected.append({'path': ('evidence/' + relative.as_posix()),
                             'bytes': len(data), 'sha256': sha256(data)})
    return selected


def verify_existing(output):
    manifest_path = output / 'manifest.json'
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError('Codex source manifest is missing or unsafe')
    manifest = json.loads(manifest_path.read_text())
    version = manifest.get('version', '')
    commit = manifest.get('commit', '')
    if (manifest.get('component') != 'Codex'
            or not re.fullmatch(r'0\.\d+\.\d+', version)
            or manifest.get('tag') != 'rust-v' + version
            or not re.fullmatch(r'[0-9a-f]{40}', commit)):
        raise ValueError('Invalid Codex source identity')

    references = manifest.get('references')
    verify_reference_chain(version, commit, references, output)
    archive = manifest.get('source_archive')
    if not isinstance(archive, dict):
        raise ValueError('Codex source archive record is missing')
    expected_archive_path = f'codex-{version}-source.tar.gz'
    if archive.get('path') != expected_archive_path:
        raise ValueError('Codex source archive path mismatch')
    archive_data = verify_file(output, archive)
    if archive.get('url') != 'https://codeload.github.com/openai/codex/tar.gz/' + commit:
        raise ValueError('Codex source archive URL does not match commit')

    expected_evidence = archive_evidence(archive_data, commit)
    evidence = manifest.get('evidence')
    if not isinstance(evidence, list) or evidence != expected_evidence:
        raise ValueError('Codex evidence inventory differs from source archive')
    for record in evidence:
        verify_file(output, record)

    listed = {'manifest.json', expected_archive_path}
    listed.update(record['path'] for record in references)
    listed.update(record['path'] for record in evidence)
    actual = {
        path.relative_to(output).as_posix()
        for path in output.rglob('*') if path.is_file()
    }
    if actual != listed:
        raise ValueError('Codex source tree contains missing or unlisted files')
    return {'outcome': 'PASS', 'version': version, 'commit': commit,
            'source_sha256': archive['sha256'], 'evidence_files': len(evidence),
            'manifest_sha256': sha256(manifest_path.read_bytes())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.verify:
        if args.version is not None:
            raise ValueError('--version is not used with --verify')
        if not args.output.is_dir():
            raise ValueError('Existing output directory is required for verification')
        print(json.dumps(verify_existing(args.output)))
        return
    if args.version is None:
        raise ValueError('--version is required when collecting')
    if not re.fullmatch(r'0\.\d+\.\d+', args.version):
        raise ValueError('Expected a stable Codex version')
    args.output.mkdir(parents=True, exist_ok=False)
    tag = 'rust-v' + args.version
    api = 'https://api.github.com/repos/openai/codex/git/'
    references = []
    source_ref = api + 'ref/tags/' + tag
    for index in range(5):
        path = args.output / ('reference-%d.json' % index)
        references.append(fetch(source_ref, path, 1024 * 1024))
        obj = json.loads(path.read_text())['object']
        commit = obj['sha']
        if not re.fullmatch('[0-9a-f]{40}', commit):
            raise ValueError('Invalid upstream object id')
        if obj['type'] == 'commit':
            break
        if obj['type'] != 'tag':
            raise ValueError('Unexpected upstream tag target')
        source_ref = api + 'tags/' + commit
    else:
        raise ValueError('Upstream tag nesting limit exceeded')
    archive = args.output / ('codex-' + args.version + '-source.tar.gz')
    archive_record = fetch('https://codeload.github.com/openai/codex/tar.gz/' + commit,
                           archive, 256 * 1024 * 1024)
    evidence = []
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            parts = PurePosixPath(member.name).parts
            if len(parts) < 2:
                continue
            relative = PurePosixPath(*parts[1:])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Unsafe archive member')
            if not is_evidence_path(relative) or not member.isfile():
                continue
            if member.size > 8 * 1024 * 1024:
                raise ValueError('Evidence member exceeded its size limit')
            data = source.extractfile(member).read()
            target = args.output / 'evidence' / str(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as output:
                output.write(data)
            evidence.append({'path': target.relative_to(args.output).as_posix(),
                             'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    manifest = {'component': 'Codex', 'version': args.version, 'tag': tag,
                'commit': commit, 'references': references, 'source_archive': archive_record,
                'evidence': evidence,
                'boundary': 'Upstream source snapshot and included notices only; '
                            'binary reproducibility and complete dependency obligations unverified.'}
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'version': args.version, 'commit': commit,
                      'source_sha256': archive_record['sha256'],
                      'evidence_files': len(evidence), 'output': str(args.output)}))


if __name__ == '__main__':
    main()
