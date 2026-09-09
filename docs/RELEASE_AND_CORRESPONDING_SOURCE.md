# Release and corresponding-source policy

Git stores reviewable source, build scripts, pinned coordinates and Skills.
GitHub Releases store the large APK and immutable build/source assets. Signing
keys, provider credentials, phone state and private operational evidence are
never Release assets.

For each version, publish:

1. the signed universal ARM64 APK;
2. `SHA256SUMS` and signer-certificate SHA-256;
3. the exact binary inputs named by `Android/appliance/runtime/runtime.lock`;
4. Debian source packages for every binary package in the embedded rootfs;
5. Termux bootstrap/overlay recipes, patches and upstream source archives;
6. source archives plus LICENSE/NOTICE for every bundled Codex CLI version;
7. Android dependency coordinates, POM license declarations and extracted
   archive notices;
8. this repository's tag-generated source archive.

The repository plus these versioned Release assets are the preferred form for
modifying and rebuilding the distributed APK. Users may build and sign with
their own key. The project's private update key is intentionally not provided;
an APK signed by another key cannot update an existing installation in place.

The application ID is `com.termux` for compatibility with the embedded Termux
runtime. It conflicts with a separately installed Termux app. Back up Termux
data before changing installations. Uninstalling ChebyAgent removes its Android
private data.

No source offer should be inferred from a moving branch. A Release is complete
only when its tag, APK hash, binary-input hashes and corresponding-source asset
hashes are mutually bound in `SHA256SUMS`.
