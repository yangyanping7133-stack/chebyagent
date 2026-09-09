import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name('codex_source_inventory.py')
SPEC = importlib.util.spec_from_file_location('codex_source_inventory', MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CodexSourceInventoryTest(unittest.TestCase):
    VERSION = '0.153.4'
    TAG_OBJECT = 'a' * 40
    COMMIT = 'b' * 40

    def make_collection(self, root):
        output = root / 'collection'
        output.mkdir()
        ref0 = {'ref': 'refs/tags/rust-v' + self.VERSION,
                'object': {'sha': self.TAG_OBJECT, 'type': 'tag'}}
        ref1 = {'sha': self.TAG_OBJECT,
                'object': {'sha': self.COMMIT, 'type': 'commit'}}
        references = []
        for index, document in enumerate((ref0, ref1)):
            data = json.dumps(document).encode()
            path = f'reference-{index}.json'
            (output / path).write_bytes(data)
            references.append({'url': 'https://example.invalid/' + path, 'path': path,
                               'bytes': len(data), 'sha256': MODULE.sha256(data)})

        archive_buffer = io.BytesIO()
        root_name = 'codex-' + self.COMMIT
        with tarfile.open(fileobj=archive_buffer, mode='w:gz') as archive:
            for relative, data in (('LICENSE', b'license'),
                                   ('codex-rs/Cargo.toml', b'[workspace]'),
                                   ('README.md', b'not selected')):
                info = tarfile.TarInfo(root_name + '/' + relative)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        archive_data = archive_buffer.getvalue()
        archive_name = f'codex-{self.VERSION}-source.tar.gz'
        (output / archive_name).write_bytes(archive_data)
        evidence = MODULE.archive_evidence(archive_data, self.COMMIT)
        for record in evidence:
            relative = record['path'].removeprefix('evidence/')
            data = b'license' if relative == 'LICENSE' else b'[workspace]'
            target = output / record['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        manifest = {
            'component': 'Codex', 'version': self.VERSION,
            'tag': 'rust-v' + self.VERSION, 'commit': self.COMMIT,
            'references': references,
            'source_archive': {
                'url': 'https://codeload.github.com/openai/codex/tar.gz/' + self.COMMIT,
                'path': archive_name, 'bytes': len(archive_data),
                'sha256': MODULE.sha256(archive_data),
            },
            'evidence': evidence,
        }
        (output / 'manifest.json').write_text(json.dumps(manifest))
        return output

    def test_verify_existing_rechecks_chain_archive_and_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = self.make_collection(Path(temporary))
            result = MODULE.verify_existing(output)
            self.assertEqual(('PASS', self.VERSION, 2),
                             (result['outcome'], result['version'], result['evidence_files']))

    def test_verify_existing_rejects_changed_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = self.make_collection(Path(temporary))
            (output / 'evidence' / 'LICENSE').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                MODULE.verify_existing(output)

    def test_verify_existing_rejects_reference_chain_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = self.make_collection(Path(temporary))
            manifest_path = output / 'manifest.json'
            manifest = json.loads(manifest_path.read_text())
            ref_path = output / 'reference-1.json'
            document = json.loads(ref_path.read_text())
            document['object']['sha'] = 'c' * 40
            data = json.dumps(document).encode()
            ref_path.write_bytes(data)
            manifest['references'][1].update(bytes=len(data), sha256=MODULE.sha256(data))
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'commit reference mismatch'):
                MODULE.verify_existing(output)

    def test_verify_existing_rejects_unlisted_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = self.make_collection(Path(temporary))
            (output / 'extra').write_text('unexpected')
            with self.assertRaisesRegex(ValueError, 'unlisted files'):
                MODULE.verify_existing(output)


if __name__ == '__main__':
    unittest.main()
