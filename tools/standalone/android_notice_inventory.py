#!/usr/bin/env python3
"""Collect declared POM licenses and notices from resolved AAR/JAR inputs."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET
import zipfile


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = json.loads(args.inventory.read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    notices = []

    def scan(data, coordinate, chain, depth=0):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                path = PurePosixPath(member.filename)
                if path.is_absolute() or '..' in path.parts:
                    raise ValueError('Unsafe archive member')
                if member.is_dir():
                    continue
                name = path.name.lower()
                selected = name.startswith(('license', 'licence', 'notice', 'copying', 'dependencies'))
                nested = depth < 2 and name.endswith('.jar')
                if not selected and not nested:
                    continue
                if member.file_size > (100 * 1024 * 1024 if nested else 8 * 1024 * 1024):
                    raise ValueError('Archive member too large')
                content = archive.read(member)
                location = chain + '!/' + member.filename
                if selected:
                    target = args.output / 'evidence' / (sha(content) + '.txt')
                    target.parent.mkdir(exist_ok=True)
                    if not target.exists():
                        target.write_bytes(content)
                    notices.append({'coordinate': coordinate, 'location': location,
                                    'path': target.relative_to(args.output).as_posix(),
                                    'sha256': sha(content), 'bytes': len(content)})
                if nested:
                    scan(content, coordinate, location, depth + 1)

    for artifact in source['artifacts']:
        data = Path(artifact['file']).read_bytes()
        if sha(data) != artifact['sha256']:
            raise ValueError('Resolved artifact changed after inventory')
        if zipfile.is_zipfile(io.BytesIO(data)):
            scan(data, artifact['coordinate'], artifact['name'])

    declarations = []
    ns = {'m': 'http://maven.apache.org/POM/4.0.0'}
    for pom in source['poms']:
        if 'path' not in pom:
            declarations.append({'coordinate': pom['coordinate'], 'status': 'UNRESOLVED'})
            continue
        data = (args.inventory.parent / pom['path']).read_bytes()
        if sha(data) != pom['sha256']:
            raise ValueError('POM changed after inventory')
        root = ET.fromstring(data)
        licenses = [{'name': item.findtext('m:name', default='', namespaces=ns),
                     'url': item.findtext('m:url', default='', namespaces=ns)}
                    for item in root.findall('m:licenses/m:license', ns)]
        declarations.append({'coordinate': pom['coordinate'], 'licenses': licenses,
                             'scm_url': root.findtext('m:scm/m:url', default='', namespaces=ns)})
    result = {'input_manifest_sha256': sha(args.inventory.read_bytes()),
              'boundary': 'Direct POM declarations and included archive notices; '
                          'parent inheritance, final APK coverage, and obligations require review.',
              'declarations': declarations, 'notices': notices}
    (args.output / 'manifest.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'artifacts_verified': len(source['artifacts']),
                      'poms_with_direct_license': sum(bool(x.get('licenses')) for x in declarations),
                      'notice_records': len(notices),
                      'unique_notice_files': len(set(x['sha256'] for x in notices))}))


if __name__ == '__main__':
    main()
