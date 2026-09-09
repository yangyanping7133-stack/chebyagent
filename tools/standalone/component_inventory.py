#!/usr/bin/env python3
"""Read pinned delivery archives without executing their code; collect license evidence.

Only package metadata and license documents are copied. Links are resolved in a
virtual archive namespace, never materialized on the host. Source availability
and redistribution compliance are deliberately separate from license presence.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import posixpath
import re
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_NODE = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
DECODE = 'require("node:fs").createReadStream(process.argv[1]).pipe(require("node:zlib").createZstdDecompress()).pipe(process.stdout)'
LICENSE = re.compile(r'^(copyright|copying|license|licence|notice)(?:[._-].*)?$', re.I)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def normalize(name):
    normalized = posixpath.normpath(name).lstrip('/')
    if normalized == '..' or normalized.startswith('../'):
        raise ValueError('Archive path escapes root')
    return normalized


def selected(name):
    return (name in ('var/lib/dpkg/status', 'SYMLINKS.txt')
            or '/common-licenses/' in name or name.startswith('share/LICENSES/')
            or (('/share/doc/' in '/' + name or '/opt/' in '/' + name)
                and LICENSE.match(posixpath.basename(name)))
            or name.endswith(('codex-package.json', 'npm-package.json')))


def read_archive(path, node, all_files=False):
    files, links, members = {}, {}, []
    if path.suffix == '.zip':
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                name = normalize(item.filename)
                if not item.is_dir() and selected(name):
                    files[name] = archive.read(item)
            for line in files.get('SYMLINKS.txt', b'').decode().splitlines():
                target, name = line.split('←', 1)
                links[normalize(name)] = normalize(posixpath.join(posixpath.dirname(name), target))
        return files, links, members
    process = None
    try:
        if path.name.endswith('.zst'):
            process = subprocess.Popen([str(node), '-e', DECODE, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            archive = tarfile.open(fileobj=process.stdout, mode='r|')
        else:
            archive = tarfile.open(path, mode='r|gz')
        with archive:
            for item in archive:
                name = normalize(item.name)
                if name == '.':
                    continue
                if item.issym() or item.islnk():
                    target = item.linkname if item.islnk() else posixpath.join(posixpath.dirname(name), item.linkname)
                    links[name] = normalize(target)
                    if all_files:
                        members.append({'path': name, 'type': 'link', 'target': item.linkname})
                elif item.isfile() and (selected(name) or all_files or name.startswith('package/')):
                    stream = archive.extractfile(item)
                    data = stream.read()
                    if selected(name) or name.startswith('package/'):
                        if not name.startswith('package/vendor/') or name.endswith('.json'):
                            files[name] = data
                    if all_files or name.startswith('package/'):
                        members.append({'path': name, 'type': 'file', 'size': item.size, 'sha256': sha(data)})
        if process:
            # Consume trailing tar padding so the decoder can finish normally.
            for _ in iter(lambda: process.stdout.read(1024 * 1024), b''):
                pass
            error = process.stderr.read()
            if process.wait() != 0:
                raise RuntimeError('Archive decompression failed: ' + error.decode())
        return files, links, members
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait()


def resolve(path, files, links):
    current, visited = normalize(path), set()
    while current not in visited:
        visited.add(current)
        if current in files:
            return current
        parts = current.split('/')
        for length in range(len(parts), 0, -1):
            ancestor = '/'.join(parts[:length])
            if ancestor in links:
                current = normalize(posixpath.join(links[ancestor], *parts[length:]))
                break
        else:
            return None
    return None


def paragraphs(data):
    output = []
    for block in data.decode('utf-8', 'replace').split('\n\n'):
        record, key = {}, None
        for line in block.splitlines():
            if line.startswith((' ', '\t')) and key:
                record[key] += ('\n' if record[key] else '') + line[1:]
            else:
                field = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9-]*):(?: (.*))?', line)
                if field:
                    key, value = field.groups()
                    record[key] = value or ''
                else:
                    key = None
        if record.get('Status') == 'install ok installed':
            output.append(record)
    return output


def package_rows(records, files, links, origin, prefix):
    rows = []
    license_names = {path for path in files.keys() | links.keys() if LICENSE.match(posixpath.basename(path))}
    for record in sorted(records, key=lambda row: row['Package']):
        package = record['Package']
        doc = f'{prefix}share/doc/{package}'
        evidence = []
        candidates = {name for name in license_names if name.startswith(doc + '/')}
        # Debian often aliases entire binary-package documentation directories.
        for name in ('copyright', 'LICENSE', 'COPYING', 'NOTICE'):
            candidates.add(doc + '/' + name)
        for candidate in sorted(candidates):
            target = resolve(candidate, files, links)
            if target:
                data = files[target].decode('utf-8', 'replace')
                evidence.append({'path': candidate, 'resolved_path': target, 'sha256': sha(files[target]),
                                 'declared_license_labels': sorted(set(re.findall(r'^License: (.+)$', data, re.M))),
                                 'declared_source_lines': re.findall(r'^Source: (.+)$', data, re.M)})
        source = record.get('Source', package)
        matched = re.fullmatch(r'([^ ]+)(?: \((.+)\))?', source)
        source_name = matched.group(1) if matched else source
        source_version = matched.group(2) if matched and matched.group(2) else record['Version']
        rows.append({'origin': origin, 'package': package, 'version': record['Version'],
                     'architecture': record.get('Architecture'), 'source_field': record.get('Source'),
                     'source_package': source_name, 'source_version': source_version,
                     'source_identity_basis': ('Debian Source field or Debian binary-source naming convention' if origin == 'debian'
                                               else 'Binary package name/version only; see separate pinned recipe inventory'),
                     'homepage_from_package': record.get('Homepage'), 'license_evidence': evidence,
                     'license_evidence_state': 'PRESENT' if evidence else 'MISSING',
                     'corresponding_source_state': 'NOT_COLLECTED',
                     'source_gap': ('Exact Debian source package/version recorded; source archives and packaging not collected.'
                                    if origin == 'debian' else 'Exact Termux recipe commit, patches and source archive not collected.'),
                     'compliance_state': 'NOT_ASSESSED'})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--node', type=Path, default=DEFAULT_NODE)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Output already exists; choose a new immutable inventory directory')
    lock = dict(re.findall(r"^([A-Z][A-Z0-9_]*)='([^']*)'$", (ROOT/'Android/appliance/runtime/runtime.lock').read_text(), re.M))
    inputs = {
        'debian': (ROOT/'artifacts/private/runtime/standalone-4.1'/lock['DEBIAN_ROOTFS_ASSET'], lock['DEBIAN_ROOTFS_SHA256']),
        'bootstrap': (ROOT/'third_party/termux-app/app/src/main/cpp/bootstrap-aarch64.zip', 'c8d702b6f742935001c37cda81b8ac69504a95d5cf28f2899532dd8cd4b057eb'),
        'overlay': (ROOT/'artifacts/private/runtime/standalone-4.1'/lock['TERMUX_PROOT_OVERLAY_ASSET'], lock['TERMUX_PROOT_OVERLAY_SHA256']),
        'codex': (ROOT/f"artifacts/private/runtime/codex-{lock['CODEX_VERSION']}"/lock['CODEX_ARCHIVE_ASSET'], lock['CODEX_ARCHIVE_SHA256']),
    }
    archives, sources = {}, {}
    for name, (path, expected) in inputs.items():
        digest = file_sha(path)
        if digest != expected:
            raise SystemExit('Pinned archive mismatch: ' + name)
        archives[name] = {'path': str(path.relative_to(ROOT)), 'sha256': digest, 'bytes': path.stat().st_size}
        print('Reading verified ' + name, flush=True)
        sources[name] = read_archive(path, args.node, name == 'overlay')
    args.output.mkdir(parents=True)
    evidence_manifest = []
    for origin, (files, links, _) in sources.items():
        for name, data in sorted(files.items()):
            target = args.output/'evidence'/origin/name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            evidence_manifest.append({'origin': origin, 'archive_path': name, 'sha256': sha(data), 'bytes': len(data),
                                      'saved_path': str(target.relative_to(args.output))})
        (args.output/f'{origin}-links.json').write_text(json.dumps(links, indent=2, sort_keys=True)+'\n')
    debian_files, debian_links, _ = sources['debian']
    bootstrap_files, bootstrap_links, _ = sources['bootstrap']
    overlay_files, overlay_links, overlay_members = sources['overlay']
    merged_files = {**bootstrap_files, **overlay_files}
    merged_links = {**bootstrap_links, **overlay_links}
    rows = package_rows(paragraphs(debian_files['var/lib/dpkg/status']), debian_files, debian_links, 'debian', 'usr/')
    rows += package_rows(paragraphs(bootstrap_files['var/lib/dpkg/status']), merged_files, merged_links, 'termux-bootstrap', '')
    for package, version_key, sha_key in [('proot', 'PROOT_VERSION', 'PROOT_PACKAGE_SHA256'), ('libandroid-shmem', 'LIBANDROID_SHMEM_VERSION', 'LIBANDROID_SHMEM_PACKAGE_SHA256'), ('libtalloc', 'LIBTALLOC_VERSION', 'LIBTALLOC_PACKAGE_SHA256')]:
        row = package_rows([{'Package': package, 'Version': lock[version_key], 'Architecture': 'aarch64'}], merged_files, merged_links, 'termux-overlay', '')[0]
        row['identity_evidence'] = 'runtime.lock and capture script; overlay has no dpkg control metadata'
        row['original_deb_sha256'] = lock[sha_key]
        rows.append(row)
    extra = []
    for origin in ('debian', 'codex'):
        files = sources[origin][0]
        for name, data in files.items():
            if name.endswith('codex-package.json'):
                metadata = json.loads(data)
                extra.append({'component': 'Codex upstream payload', 'origin': origin, 'metadata_path': name,
                              'metadata': metadata, 'metadata_sha256': sha(data),
                              'corresponding_source_state': 'NOT_COLLECTED', 'compliance_state': 'NOT_ASSESSED'})
    prompt_candidates = list((ROOT/'Android/appliance/runtime').glob('*prompt*'))
    prompt_candidates += [ROOT/'Android/appliance/runtime/codex-base-instructions.md']
    for path in prompt_candidates:
        if path.is_file():
            extra.append({'component': 'Codex prompt asset', 'path': str(path.relative_to(ROOT)), 'sha256': file_sha(path),
                          'source_claim': 'openai/codex 0.153.4 models-manager/prompt.md; corroborate vendored provenance',
                          'compliance_state': 'NOT_ASSESSED'})
    result = {'schema_version': 1, 'archives': archives, 'packages': rows, 'extra_components': extra,
              'overlay_members': overlay_members, 'evidence_manifest': evidence_manifest,
              'scope': 'Archive package metadata and license documents only; excludes Gradle/JNI dependency inventory and full source collection.',
              'compliance_state': 'NOT_ASSESSED'}
    (args.output/'inventory.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    with (args.output/'packages.tsv').open('w', newline='') as stream:
        writer = csv.writer(stream, delimiter='\t')
        writer.writerow(['origin','package','version','source_package','source_version','license_evidence','corresponding_source'])
        for row in rows:
            writer.writerow([row[key] for key in ('origin','package','version','source_package','source_version','license_evidence_state','corresponding_source_state')])
    counts = {}
    for origin in ('debian', 'termux-bootstrap', 'termux-overlay'):
        subset = [row for row in rows if row['origin'] == origin]
        counts[origin] = {'packages': len(subset), 'license_evidence_present': sum(bool(row['license_evidence']) for row in subset),
                          'missing_license_packages': [row['package'] for row in subset if not row['license_evidence']]}
    summary = {'counts': counts, 'evidence_files': len(evidence_manifest), 'inventory_sha256': file_sha(args.output/'inventory.json'),
               'packages_tsv_sha256': file_sha(args.output/'packages.tsv'), 'source_archives_collected': 0, 'compliance_state': 'NOT_ASSESSED'}
    (args.output/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
