# ChebyAgent 0.7.0

This is the first consolidated ChebyAgent source and binary release.

## Start here

Ordinary users install `ChebyAgent-0.7.0.apk`; no USB connection, Termux setup
or desktop command is required. The complete Chinese installation, login,
accessibility, safety-confirmation and troubleshooting guide is in the
[repository README](https://github.com/yangyanping7133-stack/chebyagent/blob/main/README.md).

## Included

- Android conversation UI, resumable Codex app-server sessions and recovery;
- accessibility-based screen inspection and confirmed phone actions;
- embedded Termux, PRoot/Debian and ARM64 Codex CLI 0.153.4;
- PhoneBridge, local MCP and ACE memory adapter;
- reusable skills for maps, restaurants, coffee, supermarkets, personal
  services, renting, delivery and ride-hailing;
- gateway, relay, connector, deployment and audit tooling.

## APK identity

- package: `com.termux`
- versionCode: `16`
- versionName: `0.7.0`
- APK SHA-256: `8f3e36e0767d842242521f7d3b9c4d935a586b4f93cabdb6d700f7f53287f234`
- signer certificate SHA-256:
  `dc9a4b8cbefe4da91e1a56233b3d2f164d651df1dcc27996ff8ce381c8bb2730`

The exact APK was installed as a data-preserving update on an Android 12 ARM64
test phone. Its device-side `base.apk` hash matched the Release APK, the
accessibility service remained enabled, and a cold launch resumed the main
activity and foreground agent service.

## Important boundaries

- ChebyAgent uses application ID `com.termux` and conflicts with a separately
  installed Termux app.
- The private update signing key is not distributed. Self-built APKs must be
  signed with the builder's own key and cannot update this signed build in
  place.
- Phone actions that purchase, order, book or submit remain user-confirmed.
- The final accepted coffee scenario took about six minutes; the three-minute
  response target remains open.

## Source and licenses

The repository is GPL-3.0-only. Release assets include the exact large binary
inputs plus version-bound Debian, Termux, Codex and Android dependency source
evidence. See `SHA256SUMS`, `THIRD_PARTY_NOTICES.md`, and
`docs/RELEASE_AND_CORRESPONDING_SOURCE.md`.

The component inventory and schema-2 reconciliation are engineering evidence,
not legal advice. The reconciliation closes 367/367 license identities and
binds 226/226 runtime/extra rows to source archives without recording a legal
conclusion. The actual distributor must complete the release-level attestation
before making the release public.

## Compliance addendum

- GitHub Actions is disabled for this repository. GitHub is not used to
  compile, test, sign, package or automatically publish ChebyAgent.
- All `v0.7.0` binaries were built outside GitHub and were uploaded only after
  local, version-bound verification.
- The Release includes direct license, notice, delivery-ledger and audit-report
  assets in addition to the corresponding-source archives.
- Delivery integrity, source-archive integrity and mechanical evidence
  reconciliation pass. The repository remains private because the authorized
  distributor attestation is still pending.
