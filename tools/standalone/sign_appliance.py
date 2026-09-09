#!/usr/bin/env python3
"""Sign appliance artifacts with one private, upgrade-stable project certificate."""
import argparse
import os
from pathlib import Path
import secrets
import subprocess

REPO = Path(__file__).resolve().parents[2]
PRIVATE = REPO / 'artifacts/private/android-signing'
STORE = PRIVATE / 'chebyagent-appliance.p12'
PASSWORD = PRIVATE / 'chebyagent-appliance.pass'
ALIAS = 'chebyagent-appliance'


def run(args):
    return subprocess.run([str(value) for value in args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def private_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('Signing material must be an owner-only regular file')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--initialize', action='store_true',
                        help='Create signing material once; never replace an existing key')
    parser.add_argument('--keytool', type=Path)
    parser.add_argument('--apksigner', type=Path)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if PRIVATE.is_symlink():
        raise ValueError('Private signing directory must not be a symlink')
    if args.initialize:
        if STORE.exists() or PASSWORD.exists():
            raise ValueError('Signing material already exists; refusing to replace it')
        if not args.keytool:
            raise ValueError('--keytool is required for initialization')
        PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(PRIVATE, 0o700)
        descriptor = os.open(PASSWORD, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(secrets.token_urlsafe(48) + '\n')
        # Password is read by keytool from a private file, never an argument value.
        run([args.keytool, '-genkeypair', '-keystore', STORE, '-storetype', 'PKCS12',
             '-storepass:file', PASSWORD, '-keypass:file', PASSWORD, '-alias', ALIAS,
             '-keyalg', 'RSA', '-keysize', '3072', '-validity', '10000',
             '-dname', 'CN=ChebyAgent Private Test,O=ChebyAgent', '-noprompt'])
        os.chmod(STORE, 0o600)
        print('Created project signing material; keep a secure backup outside releases.')
    if args.input or args.output:
        if not all((args.input, args.output, args.apksigner)):
            raise ValueError('Signing requires --input, --output and --apksigner')
        if not args.input.is_file() or args.input.resolve() == args.output.resolve():
            raise ValueError('Input must exist and output must be a separate file')
        if args.output.exists():
            raise ValueError('Output exists; use a new checkpoint artifact path')
        private_file(STORE)
        private_file(PASSWORD)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        run([args.apksigner, 'sign', '--ks', STORE, '--ks-key-alias', ALIAS,
             # PKCS12 uses the store password for its key. Reading the same one-line
             # file twice makes apksigner consume EOF for the second password.
             '--ks-pass', 'file:' + str(PASSWORD),
             '--out', args.output, args.input])
        run([args.apksigner, 'verify', '--verbose', args.output])
        print('Signed and verified APK. This does not establish device or product acceptance.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        # Tool output is intentionally not echoed; it may contain signing configuration.
        raise SystemExit('Signing failed: ' + (str(error) if isinstance(error, ValueError)
                                                else type(error).__name__))
