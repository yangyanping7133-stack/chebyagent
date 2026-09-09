# ChebyAgent 0.7.0 release compliance audit

Observed on 2026-09-09. This is a version-bound engineering audit, not legal
advice or a blanket legal-compliance declaration.

## Outcome

- Release delivery integrity: **PASS**.
- APK-to-large-input binding: **PASS**.
- Corresponding-source archive integrity: **PASS**.
- Direct distribution of core license and notice files: **PASS**.
- Per-component legal-obligation review: **BLOCKED**.
- Public-release gate: **BLOCKED; keep the repository and Release private**.

The private Release may be used by recipients who also receive access to the
matching source and license assets. It must not yet be described as a fully
legally reviewed public open-source distribution.

## No GitHub-hosted build

GitHub Actions is disabled at repository level. The former verification
workflow was removed from the default branch. No GitHub-hosted compilation,
signing, packaging or publication was used for this audit. The two historical
workflow attempts both failed before running project tests and are retained as
audit history.

## Verified evidence

1. The exact Release APK has SHA-256
   `8f3e36e0767d842242521f7d3b9c4d935a586b4f93cabdb6d700f7f53287f234`.
   It is a non-debuggable, non-test-only `com.termux` release package.
2. The APK contains GPL-3.0 text, project `NOTICE`,
   `THIRD_PARTY_NOTICES.md`, and ACE's Apache-2.0 license. All three project
   notice hashes match the immutable `v0.7.0` source tag. The Release addendum
   extends the third-party notices without changing the APK.
3. The APK-embedded Debian rootfs, Termux overlay and Codex CLI archives match
   the hashes in `runtime.lock` and the GitHub Release assets.
4. The Release `SHA256SUMS` entries match every APK, binary-input and
   corresponding-source asset digest reported by GitHub.
5. Offline internal verification of the corresponding-source assets passed:

   - Debian: 101 exact source packages, 350 retained files, 590,938,522 bytes;
   - Termux bootstrap: 75 binary packages, 71 source-recipe packages,
     123 source ingredients, 464 retained files;
   - Termux overlay: 3 packages and 4 retained recipe/patch files;
   - Codex CLI 0.147.0 and 0.153.4: pinned commits, complete upstream source
     archives, LICENSE/NOTICE, and 157 plus 172 extracted evidence files.
6. The license-evidence archive records 223 runtime packages, 3 additional
   runtime components and 141 Maven declarations, for 367 component rows.
7. The five Maven direct-POM omissions and five Termux subpackage direct-path
   omissions have explicit license/source identity mappings in
   `THIRD_PARTY_NOTICES.md`.

## Remaining blockers

1. All 367 component rows remain fail-closed pending package-specific review of
   notice, attribution, source-offer, installation-information and relinking
   obligations. License identity alone does not close those obligations.
2. Android/JNI components are not yet mapped byte-for-byte from the signed APK
   back to every dependency record.
3. Debian `.dsc` signatures are retained but were not cryptographically
   verified.
4. The Termux `command-not-found` historical input used an unpinned `master`
   URL, and the `foot` 1.21.0 source is preserved through a verified equivalent
   source tree rather than the unavailable historical compressed bytes.
5. Codex source archives are version- and commit-bound, but a complete
   dependency-by-dependency legal adjudication and reproducible binary rebuild
   have not been established.
6. GPLv3 Installation Information applicability to every eventual device and
   distribution arrangement needs a distributor-side legal determination.

## Public-release acceptance condition

Do not change repository visibility to public until the version-bound delivery
ledger has no unresolved mandatory obligations, the APK/JNI mapping is closed,
all required notices are confirmed readable to binary recipients, and the
distributor accepts the remaining signing and installation-information
position. A later public release should use a new immutable version rather than
moving or replacing tag `v0.7.0`.
