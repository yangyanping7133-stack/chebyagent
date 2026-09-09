#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import sys
import unittest
sys.dont_write_bytecode = True


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inventory = load('component_inventory')
recipes = load('component_source_enrichment')


class EvidenceTests(unittest.TestCase):
    def test_empty_control_field_does_not_absorb_continuations_into_version(self):
        data = (b'Package: audit\nStatus: install ok installed\nVersion: 1:4.0.2-2\n'
                b'Conffiles:\n /etc/libaudit.conf cdc703f9\nArchitecture: arm64\n\n')
        record = inventory.paragraphs(data)[0]
        self.assertEqual('1:4.0.2-2', record['Version'])
        self.assertEqual('/etc/libaudit.conf cdc703f9', record['Conffiles'])

    def test_overlay_license_resolves_against_bootstrap(self):
        files = {'share/LICENSES/GPL-2.0.txt': b'license'}
        links = {'share/doc/proot/copyright': 'share/LICENSES/GPL-2.0.txt'}
        self.assertEqual(inventory.resolve('share/doc/proot/copyright', files, links), 'share/LICENSES/GPL-2.0.txt')

    def test_debian_directory_alias_resolves(self):
        self.assertEqual(inventory.resolve('usr/share/doc/binary/copyright',
                         {'usr/share/doc/source/copyright': b'license'},
                         {'usr/share/doc/binary': 'usr/share/doc/source'}), 'usr/share/doc/source/copyright')

    def test_loop_does_not_invent_license(self):
        self.assertIsNone(inventory.resolve('a', {}, {'a': 'b', 'b': 'a'}))

    def test_escape_is_rejected(self):
        with self.assertRaises(ValueError):
            inventory.normalize('../../outside')

    def test_version_literals_arrays_and_revision(self):
        self.assertEqual(recipes.package_version('TERMUX_PKG_VERSION=(29 "other")'), '29')
        self.assertEqual(recipes.package_version('TERMUX_PKG_VERSION="1.3-20240307"'), '1.3-20240307-0')
        self.assertEqual(recipes.package_version('TERMUX_PKG_VERSION=1:3.4.1'), '1:3.4.1')

    def test_version_variable_expansion_does_not_execute_shell(self):
        self.assertEqual(recipes.package_version('_MAIN_VERSION=5.2\n_PATCH_VERSION=37\nTERMUX_PKG_VERSION=${_MAIN_VERSION}.${_PATCH_VERSION}\nTERMUX_PKG_REVISION=2'), '5.2.37-2')
        self.assertIsNone(recipes.package_version('TERMUX_PKG_VERSION=$(touch /tmp/should-never-run)'))


if __name__ == '__main__':
    unittest.main()
