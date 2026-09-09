# License and corresponding-source status

This directory is the entry point for ChebyAgent redistribution evidence. It
records what has been collected and verified, and what still requires an
explicit component-by-component decision. It is not legal advice.

## Current 0.7.0 result

| Gate | Result | Evidence |
|---|---|---|
| Project license text | Pass | Root `LICENSE` contains GPL-3.0-only |
| APK, source and notice asset hashes | Pass | Release `SHA256SUMS` and delivery ledger |
| Debian corresponding-source archive | Pass for inventory and archive integrity | 101 source packages, 350 files |
| Termux corresponding-source archive | Pass for inventory and archive integrity | 75 binary packages, 71 recipes, 123 inputs |
| Codex fixed-version source and license evidence | Pass for archive integrity | Versions 0.147.0 and 0.153.4 |
| Android/JVM declared dependency inventory | Present | 141 Maven declarations plus extracted notices |
| Package-specific redistribution obligations | **Open** | 367 ledger rows require human review |
| Public distribution approval | **Not approved** | Repository and Release stay private |

“Pass” in this table means the named engineering gate passed. It does not mean
that every legal obligation of every component has been adjudicated.

## Where to look

- [`RELEASE_COMPLIANCE_AUDIT_0.7.0.md`](RELEASE_COMPLIANCE_AUDIT_0.7.0.md) is
  the current decision record and blocker list.
- [`COMPONENT_INVENTORY_20260905.md`](COMPONENT_INVENTORY_20260905.md) is the
  detailed historical inventory that the 0.7.0 audit builds on.
- [`../../THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md) maps bundled
  component families to license and corresponding-source material.
- [`../RELEASE_AND_CORRESPONDING_SOURCE.md`](../RELEASE_AND_CORRESPONDING_SOURCE.md)
  defines the per-version Release asset contract.
- The `ChebyAgent-0.7.0-delivery-license-ledger.json` Release asset is the
  machine-readable fail-closed review ledger.

## Public-release acceptance gate

Do not change the repository or Release to public merely because project code
and source archives are visible to the distributor. Before public release, an
authorized reviewer must resolve every ledger row and record, where applicable:

1. the exact license and copyright attribution;
2. which license/NOTICE text must accompany the binary;
3. which corresponding source, build scripts and modification notices apply;
4. whether source-offer, relinking or Installation Information obligations
   apply to this APK and distribution method;
5. the byte-to-component mapping from the signed APK to the reviewed record.

The review must also close the unresolved boundaries listed in the current
audit, including Debian signature verification, historical Termux inputs and
the Codex dependency review. Only then may the version receive a new, immutable
public-release decision. Do not rewrite the existing `v0.7.0` tag to make that
decision retroactive.
