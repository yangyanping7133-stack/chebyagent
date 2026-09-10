# Installation information for modified builds

This document applies to the ChebyAgent 0.7.x software-only APK distribution.
ChebyAgent does not distribute, sell or lease an Android phone or other hardware
product with the APK. It does not impose an APK-signing-key check, verified-boot
change, bootloader change or product-specific authorization code on modified
builds.

## Build and sign your version

1. Check out the source tag that matches the installed release and obtain the
   exact runtime/source assets named in `docs/RELEASE_AND_CORRESPONDING_SOURCE.md`.
2. Apply your changes and build locally with the Android toolchain described by
   the repository. GitHub Actions is neither required nor enabled.
3. Sign the resulting APK with an Android signing key that you control. The
   ChebyAgent distributor's update key is private and is not required to build,
   inspect, modify, sign or install your own version.

## Install a differently signed build

Android does not allow an APK signed by a different key to update the installed
package in place. ChebyAgent also uses application ID `com.termux`, so it cannot
coexist with a separately signed Termux installation.

1. Export or back up data that must be retained. Uninstalling removes the
   application's Android-private data.
2. Uninstall the existing `com.termux` package.
3. Install your signed APK through Android's ordinary package installer or ADB.
4. Open the app and complete first-run provisioning again.

No ChebyAgent service must approve the replacement build. The unavoidable data
loss risk comes from Android's package-signature and application-sandbox rules,
so verify the backup before uninstalling.

## Replace or relink bundled runtime components

The preferred modification form is the repository source plus the version-bound
Release source archives. The APK composition root is `Android/appliance/`.

- Termux application/runtime source is under `third_party/termux-app/`; local
  changes are listed in `third_party/termux-app/PATCHES.md`.
- Debian and Termux package recipes, patches and upstream source inputs are in
  the corresponding-source Release archives.
- Codex source, LICENSE and NOTICE for each bundled version are in the Codex
  corresponding-source archive.
- Runtime filenames and exact binary hashes are pinned in
  `Android/appliance/runtime/runtime.lock` and checked by the local build.

Modify or rebuild the relevant component, replace its pinned runtime input,
update the lock/hash in the same source change, then rebuild and sign the APK.
Shared libraries are ordinary files inside the embedded Termux/Debian layers;
there is no ChebyAgent cryptographic restriction on replacing them in a
self-built APK.

This is factual installation and rebuilding information, not legal advice or a
promise that every third-party component is reproducibly buildable on every
host.
