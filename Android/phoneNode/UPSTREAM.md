# ChebyNode vendoring record

This Android application is the Codex-isolated PhoneBridge node bundled with ChebyCodex Mobile.

- Upstream repository: local `chebynode` checkout
- Upstream commit: `1a33089a7bb7baeede315a5ea33daf1fbed6513b`
- Package: `com.chebysight.chebyagent.phonenode`
- Version: `0.4.11-phonebridge` (`versionCode 37`)
- PhoneBridge endpoint: port `3438`
- Remote MCP listener: disabled in this build

Local changes retained here:

1. PhoneBridge tool text does not trigger broad call/SMS false positives.
2. Semantic editable controls outrank empty decoration nodes, with a focused-input traversal fallback.
3. Android 10 screenshots use an explicit MediaProjection consent flow; Android 11+ keeps the accessibility screenshot path.
4. The policy gate permits only the exact Android system `ChebyNode` MediaProjection consent button and rejects lookalikes.
5. Policy checks use the live accessibility root package before the transient event package, so a verified launcher icon remains explainable after Home.

Do not commit generated APKs, debug keystores, credentials, pairing material, or device state. A production release must use a private release key; the real-device gate APK may use the Android debug signer.
