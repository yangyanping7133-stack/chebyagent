# ChebyAgent 0.7.0 release compliance audit

Observed on 2026-09-09. This is a version-bound engineering audit, not legal
advice or a blanket legal-compliance declaration.

## Outcome

- Release delivery integrity: **PASS**.
- APK-to-large-input binding: **PASS**.
- Corresponding-source archive integrity: **PASS**.
- Direct distribution of core license and notice files: **PASS**.
- Mechanical component-evidence reconciliation: **PASS**.
- Distributor acceptance: **PENDING**.
- Public-release gate: **BLOCKED; keep the repository and Release private**
  until the distributor attestation is completed.

The original immutable ledger conservatively marked all 367 rows unassessed.
The later schema-2 reconciliation closes the duplicated identity and archive
binding placeholders without rewriting that ledger or making a legal
conclusion. The private Release must not yet be described as a fully legally
reviewed public open-source distribution.

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
8. The schema-2 reconciliation verifies 367/367 component license identities
   and binds all 226 runtime/extra rows to hash-named corresponding-source
   archives. It deliberately records zero row-level legal conclusions.
9. `INSTALLATION_INFORMATION.md` now records the own-key rebuild, replacement
   installation and runtime-component replacement path for the software-only
   APK distribution.

## Reclassified findings

The following remain disclosed but are not repeated as 367 separate legal
blockers:

1. Gradle artifacts, POMs and notices are hash-recorded build inputs. A
   one-to-one final DEX byte map is an engineering diagnostic, not a claimed
   property of the transformed APK.
2. Retained but unverified Debian `.dsc` signatures are a supply-chain
   assurance caveat, not an identity or source-archive gap.
3. The Termux `command-not-found` historical unpinned URL and `foot` equivalent
   source-tree proof remain explicit acquisition exceptions.
4. Codex binary reproducibility is not claimed. Fixed source, LICENSE and NOTICE
   are delivered for both bundled versions.

## Remaining release decision

One release-level decision remains. The actual distributor must complete
`DISTRIBUTOR_ATTESTATION_0.7.0.md`, accepting the recorded software-only scope,
recipient access to the source/notice/install assets, signing-key position and
disclosed source-acquisition exceptions. A script cannot sign or accept that
decision for the distributor.

## Public-release acceptance condition

Do not change repository visibility to public until the distributor attestation
is complete and the direct Release assets include the reconciliation,
modification and installation-information addendum. A later public release
should use a new immutable version rather than moving or replacing tag
`v0.7.0`.
