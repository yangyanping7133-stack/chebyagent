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
| Component license identity | Pass | 367/367 direct or explicitly mapped |
| Runtime/source release binding | Pass | 226/226 runtime and extra rows |
| Row-level legal conclusions | None claimed | The reconciler records evidence only |
| Distributor acceptance | **Pending** | One release-level attestation remains |
| Public distribution approval | **Not approved** | Repository and Release stay private until acceptance |

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
  original machine-readable fail-closed review ledger.
- [`RECONCILIATION_POLICY_0.7.0.json`](RECONCILIATION_POLICY_0.7.0.json) and
  [`RELEASE_COMPLIANCE_RECONCILIATION_0.7.0.json`](RELEASE_COMPLIANCE_RECONCILIATION_0.7.0.json)
  reconcile the later source delivery without mutating the original ledger.
- [`../../INSTALLATION_INFORMATION.md`](../../INSTALLATION_INFORMATION.md)
  explains how to rebuild, self-sign, replace an installation and replace
  bundled runtime components.
- [`MODIFICATIONS_0.7.0.md`](MODIFICATIONS_0.7.0.md) records changes to imported
  upstream code and disclosed acquisition exceptions.
- [`DISTRIBUTOR_ATTESTATION_0.7.0.md`](DISTRIBUTOR_ATTESTATION_0.7.0.md) is the
  remaining human-owned release decision.

## Public-release acceptance gate

The original 367 row-level placeholders are now reconciled as evidence states,
not auto-approved as legal conclusions. Before public release, an authorized
distributor must complete the single release-level attestation and ensure the
addendum files are direct Release assets. If the distribution expands to
hardware, a commercial bundle or a different signing/installation model, the
attestation must be redone for that scope. Do not rewrite the existing `v0.7.0`
tag to make a later decision retroactive.

Reproduce the reconciliation locally (no GitHub Actions):

```bash
cd tools/standalone
python3 reconcile_delivery_license_ledger.py \
  --ledger /path/to/ChebyAgent-0.7.0-delivery-license-ledger.json \
  --policy ../../docs/licensing/RECONCILIATION_POLICY_0.7.0.json \
  --sha256s /path/to/SHA256SUMS \
  --output /tmp/ChebyAgent-0.7.0-compliance-reconciliation.json
```
