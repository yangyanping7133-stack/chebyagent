#!/usr/bin/env python3
"""Compare stored Termux package metadata with a pinned recipe source archive."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile


COMMIT = '5a6d1c1eb868795dce83a6c269387b9f82d21805'
ARCHIVE_SHA = 'd36592f747fea017af60647e17e764eba254fefdf503b195e05abbbdcad9742d'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def literal(text, variable):
    match = re.search(r'^' + variable + r'=(.+)$', text, re.M)
    if not match:
        return None
    return match.group(1).strip().strip('"\'')


def package_version(text):
    """Resolve only literal version assignments, never execute recipe shell code."""
    variables = {}
    for line in text.splitlines():
        match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)=(.*)', line)
        if not match:
            continue
        name, value = match.groups()
        if value.startswith('('):
            value = value[1:].split()[0].rstrip(')')
        value = value.strip('"\'')
        value = re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)',
                       lambda m: variables.get(m.group(1) or m.group(2), m.group(0)), value)
        if re.fullmatch(r'[0-9][0-9A-Za-z.:+~_-]*', value):
            variables[name] = value
    version = variables.get('TERMUX_PKG_VERSION')
    if not version:
        return None
    revision = variables.get('TERMUX_PKG_REVISION', '0')
    epoch = variables.get('TERMUX_PKG_EPOCH', '0')
    return ((epoch + ':') if epoch != '0' else '') + version + ('-' + revision if revision != '0' or '-' in version else '')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', required=True, type=Path)
    parser.add_argument('--source-archive', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Choose a new immutable output directory')
    if digest(args.source_archive.read_bytes()) != ARCHIVE_SHA:
        raise SystemExit('Pinned Termux recipe archive SHA mismatch')
    inventory = json.loads(args.inventory.read_text())
    recipes, files, subpackages = {}, {}, {}
    prefix = 'termux-packages-' + COMMIT + '/'
    with tarfile.open(args.source_archive) as archive:
        for item in archive:
            if not item.isfile() or not item.name.startswith(prefix):
                continue
            name = item.name.removeprefix(prefix)
            if name in ('LICENSE.md', 'LICENSE', 'NOTICE') or name.startswith('packages/'):
                files[name] = archive.extractfile(item).read()
                match = re.fullmatch(r'packages/([^/]+)/build.sh', name)
                if match:
                    recipes[match.group(1)] = name
                match = re.fullmatch(r'packages/([^/]+)/([^/]+)\.subpackage\.sh', name)
                if match:
                    subpackages.setdefault(match.group(2), []).append((match.group(1), name))
    args.output.mkdir(parents=True)
    rows, wanted = [], set()
    for package in inventory['packages']:
        if not package['origin'].startswith('termux-'):
            continue
        name = package['package']
        candidates = [(name, None)] if name in recipes else subpackages.get(name, [])
        row = {'origin': package['origin'], 'package': name, 'installed_version': package['version'],
               'corresponding_upstream_source_state': 'NOT_COLLECTED', 'compliance_state': 'NOT_ASSESSED'}
        if len(candidates) != 1:
            row['recipe_state'] = 'MISSING_OR_AMBIGUOUS'
            row['candidates'] = candidates
        else:
            parent, subpackage = candidates[0]
            recipe = recipes[parent]
            text = files[recipe].decode()
            version, revision, epoch = (literal(text, 'TERMUX_PKG_' + part) for part in ('VERSION', 'REVISION', 'EPOCH'))
            coordinate = package_version(text)
            row.update({'source_recipe_package': parent, 'recipe': recipe, 'recipe_sha256': digest(files[recipe]),
                        'recipe_version_expression': coordinate, 'subpackage_recipe': subpackage,
                        'recipe_state': 'VERSION_MATCH' if coordinate == package['version'] else 'VERSION_MISMATCH_OR_EXPRESSION',
                        'declared_license': literal(text, 'TERMUX_PKG_LICENSE'),
                        'source_url_expression': literal(text, 'TERMUX_PKG_SRCURL'),
                        'source_sha256_expression': literal(text, 'TERMUX_PKG_SHA256'),
                        'source_recipe_url': f'https://github.com/termux/termux-packages/blob/{COMMIT}/{recipe}'})
            if subpackage:
                row['subpackage_recipe_sha256'] = digest(files[subpackage])
            if not package['license_evidence'] and row['recipe_state'] == 'VERSION_MATCH':
                row['license_gap_resolution'] = 'Exact-version parent/subpackage recipe identified. Use parent source copyright; do not infer complete notices from license label.'
                row['shared_parent_license_evidence'] = next((p['license_evidence'] for p in inventory['packages'] if p['origin'] == 'termux-bootstrap' and p['package'] == parent), [])
            wanted.add(parent)
        rows.append(row)
    manifest = []
    for name, data in sorted(files.items()):
        if name.startswith('packages/') and name.split('/')[1] not in wanted:
            continue
        path = args.output/'recipe-evidence'/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        manifest.append({'path': str(path.relative_to(args.output)), 'sha256': digest(data), 'bytes': len(data)})
    report = {'inventory_sha256': digest(args.inventory.read_bytes()), 'source_archive_sha256': ARCHIVE_SHA,
              'termux_recipe_commit': COMMIT, 'release_tag': 'bootstrap-2025.03.28-r1+apt-android-7',
              'source_archive_url': f'https://codeload.github.com/termux/termux-packages/tar.gz/{COMMIT}',
              'packages': rows, 'evidence_manifest': manifest,
              'boundary': 'Collects Termux build recipes and patches. Upstream ingredient source archives remain separate.'}
    (args.output/'recipe-inventory.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'packages': len(rows), 'exact_version_matches': sum(r['recipe_state'] == 'VERSION_MATCH' for r in rows),
                      'unmatched': [{k:r[k] for k in ('origin','package','installed_version','recipe_state')} for r in rows if r['recipe_state'] != 'VERSION_MATCH'],
                      'recipe_and_patch_files_saved': len(manifest), 'report_sha256': digest((args.output/'recipe-inventory.json').read_bytes())}, indent=2))


if __name__ == '__main__':
    main()
