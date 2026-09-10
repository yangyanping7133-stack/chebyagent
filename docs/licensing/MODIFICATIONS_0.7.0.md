# Upstream modifications and retained notices for 0.7.0

This file is the release-level change notice for imported third-party code. It
does not replace file-level copyright headers, upstream licenses or notices.

## Termux app source

Imported base: Termux app `v0.118.3`, commit
`5b657c6adf4304e5198951ce815fe0205dcac29c`.

The modified upstream files and the purpose of each change are recorded in
`third_party/termux-app/PATCHES.md`. They cover SDK 35 source compatibility,
ChebyAgent branding and foreground-notification behavior. The upstream
`LICENSE.md`, module exception list and original file headers are retained.

ChebyAgent-specific build and composition changes live outside the imported
snapshot, primarily under `Android/appliance/`, `Android/embedded-*`,
`connector/` and `tools/standalone/`.

## Termux and Debian package layers

The final APK embeds hash-pinned package layers. Version-bound Release archives
retain the package recipes, patches, upstream source inputs and package
copyright evidence. Two acquisition exceptions are disclosed rather than
hidden: one historical `command-not-found` input used an unpinned `master` URL,
and `foot` 1.21.0 is retained as a verified equivalent source tree because the
historical compressed bytes were unavailable.

## Codex CLI

The APK contains upstream Codex CLI payloads 0.147.0 and 0.153.4. ChebyAgent
does not patch those executable payloads. Their fixed upstream source trees,
Apache-2.0 LICENSE and NOTICE are delivered as a Release source archive.
ChebyAgent supplies separate launch, app-server and mobile-experience adapters.

The `codex-base-instructions.md` asset is hash-bound to the delivered Codex
source evidence and is treated as an upstream-derived runtime asset rather than
project-owned code.

## ACE

The dependency-free ACE core snapshot comes from commit
`321d430e520f369315bad512cd2d90f1fa14a596`. Its retained `UPSTREAM.md`,
Apache-2.0 LICENSE and source files identify the snapshot. ChebyAgent's adapter
and persistence integration are separate project files.

For exact binary/source hashes, use the Release `SHA256SUMS` together with
`RECONCILIATION_POLICY_0.7.0.json`.
