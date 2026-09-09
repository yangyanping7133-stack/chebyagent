#!/usr/bin/env python3
"""Read pinned runtime archives, hashing members and reporting redacted state candidates.

No archive code is executed or extracted. Pattern matches require review; absence
of matches is not a proof that arbitrary binary data contains no credentials.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import zipfile

from component_inventory import DEFAULT_NODE, DECODE, ROOT, file_sha
from source_secret_scan import RULES

STATE = re.compile(r'(^|/)(?:\.ssh|\.cheby|\.codex|auth\.json|providers\.enc|provider-settings\.json|'
                   r'id_rsa|id_ed25519|ssh_host_.*_key|[^/]+\.(?:p12|jks|keystore|pass))(/|$)')
DEBIAN_STATE = re.compile(r'^(?:root|home|tmp|run|var/cache|var/lib/apt/lists|var/log|var/tmp|'
                          r'dev|proc|sys|data|storage|apex|odm|system|vendor)(/|$)|^etc/machine-id$')
CHUNK = 1024 * 1024
OVERLAP = 8192


def member_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('Unsafe archive member name')
    return str(path)


def scan_stream(stream):
    digest = hashlib.sha256()
    total, tail, seen, matches = 0, b'', set(), []
    while True:
        chunk = stream.read(CHUNK)
        if not chunk:
            break
        digest.update(chunk)
        window = tail + chunk
        offset = total - len(tail)
        for rule, pattern in RULES.items():
            for match in pattern.finditer(window):
                location = (rule, offset + match.start())
                if location not in seen:
                    seen.add(location)
                    matches.append({'rule': rule, 'byte_offset': location[1]})
                    if len(matches) > 10000:
                        raise ValueError('Too many candidates; review archive separately')
        total += len(chunk)
        tail = window[-OVERLAP:]
    return total, digest.hexdigest(), matches


def inspect_archive(path, origin, node):
    records, findings, names = [], [], set()

    def accept(name, size, kind, stream=None):
        name = member_name(name)
        if name in names:
            raise ValueError('Duplicate archive member')
        names.add(name)
        record = {'path': name, 'type': kind, 'bytes': size}
        if kind != 'directory' and (STATE.search(name) or (origin == 'debian' and DEBIAN_STATE.search(name))):
            findings.append({'path': name, 'rule': 'state_path'})
        if stream is not None:
            count, digest, matches = scan_stream(stream)
            if count != size:
                raise ValueError('Member size mismatch')
            record['sha256'] = digest
            findings.extend({'path': name, **item} for item in matches)
        records.append(record)

    process = None
    try:
        if path.suffix == '.zip':
            with zipfile.ZipFile(path) as archive:
                for entry in archive.infolist():
                    if entry.is_dir():
                        accept(entry.filename, 0, 'directory')
                    else:
                        with archive.open(entry) as stream:
                            accept(entry.filename, entry.file_size, 'file', stream)
        else:
            if path.name.endswith('.zst'):
                process = subprocess.Popen([str(node), '-e', DECODE, str(path)], stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL)
                archive = tarfile.open(fileobj=process.stdout, mode='r|')
            else:
                archive = tarfile.open(path, mode='r|gz')
            with archive:
                for entry in archive:
                    if entry.isfile():
                        with archive.extractfile(entry) as stream:
                            accept(entry.name, entry.size, 'file', stream)
                    else:
                        accept(entry.name, 0, 'directory' if entry.isdir() else 'link_or_special')
            if process:
                for _ in iter(lambda: process.stdout.read(CHUNK), b''):
                    pass
                if process.wait() != 0:
                    raise ValueError('Archive decompression failed')
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait()
    return {'members': records, 'findings': findings,
            'file_bytes_scanned': sum(r['bytes'] for r in records if r['type'] == 'file')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--node', type=Path, default=DEFAULT_NODE)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    lock = dict(re.findall(r"^([A-Z][A-Z0-9_]*)='([^']*)'$",
                           (ROOT / 'Android/appliance/runtime/runtime.lock').read_text(), re.M))
    inputs = {
        'debian': (ROOT/'artifacts/private/runtime/standalone-4.1'/lock['DEBIAN_ROOTFS_ASSET'], lock['DEBIAN_ROOTFS_SHA256']),
        'bootstrap': (ROOT/'third_party/termux-app/app/src/main/cpp/bootstrap-aarch64.zip', 'c8d702b6f742935001c37cda81b8ac69504a95d5cf28f2899532dd8cd4b057eb'),
        'overlay': (ROOT/'artifacts/private/runtime/standalone-4.1'/lock['TERMUX_PROOT_OVERLAY_ASSET'], lock['TERMUX_PROOT_OVERLAY_SHA256']),
        'codex': (ROOT/f"artifacts/private/runtime/codex-{lock['CODEX_VERSION']}"/lock['CODEX_ARCHIVE_ASSET'], lock['CODEX_ARCHIVE_SHA256']),
    }
    summary = {'archives': {}, 'boundary': 'All regular-file bytes scanned with bounded overlap; '
               'no nested archive decoding or binary decompilation. No credential values emitted. '
               'Candidates require review; no-candidate results are not a complete secrecy proof.'}
    for origin, (path, expected) in inputs.items():
        digest = file_sha(path)
        if digest != expected:
            raise ValueError('Pinned archive mismatch: ' + origin)
        result = inspect_archive(path, origin, args.node)
        result['archive_sha256'] = digest
        (args.output/(origin + '.json')).write_text(json.dumps(result, indent=2) + '\n')
        summary['archives'][origin] = {'sha256': digest, 'members': len(result['members']),
            'file_bytes_scanned': result['file_bytes_scanned'], 'candidates': len(result['findings'])}
        print(json.dumps({origin: summary['archives'][origin]}), flush=True)
    (args.output/'summary.json').write_text(json.dumps(summary, indent=2) + '\n')


if __name__ == '__main__':
    main()
