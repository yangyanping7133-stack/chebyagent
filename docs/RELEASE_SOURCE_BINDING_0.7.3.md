# ChebyAgent 0.7.3 release and source binding

ChebyAgent 0.7.3 is a software-only Android release. The signed APK is published
as `ChebyAgent-0.7.3.apk` together with its SHA-256 and signer-certificate
SHA-256. Signing private keys, provider credentials, Codex login state and phone
data are not distribution assets.

The embedded Termux bootstrap, PRoot overlay, Debian 13.6 rootfs and Codex CLI
0.147.0/0.153.4 binary inputs are byte-identical to the already archived 0.7.0
inputs. Their hashes remain pinned in `Android/appliance/runtime/runtime.lock`.
The version-bound corresponding source, recipes, patches, notices and license
evidence remain available as immutable assets of the
[ChebyAgent 0.7.0 Release](https://github.com/yangyanping7133-stack/chebyagent/releases/tag/v0.7.0):

- `ChebyAgent-0.7.0-debian-corresponding-source.tar`
- `ChebyAgent-0.7.0-termux-corresponding-source.tar`
- `ChebyAgent-0.7.0-codex-corresponding-source.tar`
- `ChebyAgent-0.7.0-license-evidence.tar`
- `ChebyAgent-0.7.0-delivery-license-ledger.json`
- the exact binary inputs, license texts, notices and installation information

The 0.7.3 tag supplies the corresponding ChebyAgent application source for the
changes made after 0.7.0: runtime recovery, Android-network DNS refresh, removal
of the legacy Mac-assisted model proxy, removal of MiniMax, native reasoning
labels, and local Codex login-file import. GitHub Actions is disabled; the APK
was built, signed and verified locally before upload.

Release identity:

- versionName: `0.7.3`
- versionCode: `19`
- APK SHA-256: `e0945173cf3d72947297dc040d26276e33fd7e03a6c24e4bb218a116f4330e44`
- signer certificate SHA-256: `dc9a4b8cbefe4da91e1a56233b3d2f164d651df1dcc27996ff8ce381c8bb2730`

These records are reproducible engineering evidence, not legal advice.
