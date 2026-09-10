# Release and corresponding-source policy

Git stores reviewable source, build scripts, pinned coordinates and Skills.
GitHub Releases store the large APK and immutable build/source assets. Signing
keys, provider credentials, phone state and private operational evidence are
never Release assets.

GitHub Actions is intentionally disabled. GitHub-hosted runners must not build,
sign, package, test or publish this project. Release inputs are produced and
verified locally or on explicitly controlled project hardware, then uploaded
from a locally checked staging directory. The repository and Release pages are
distribution surfaces only.

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
9. direct copies of GPL-3.0-only and Apache-2.0, the project NOTICE,
   third-party notices, Codex notice, delivery license ledger and the current
   release compliance audit.
10. installation information, upstream modification notice, evidence
    reconciliation and the distributor's release decision.

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

For `v0.7.0`, the asset-integrity and mechanical reconciliation gates pass, but
public distribution remains blocked until the distributor completes
`docs/licensing/DISTRIBUTOR_ATTESTATION_0.7.0.md`. Archive completeness and a
project-level source license are necessary evidence, not a substitute for that
release-level decision.
