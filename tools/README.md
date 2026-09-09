# Protocol gate

Run from the repository root with Python 3.9 or newer:

```sh
python3 tools/run_protocol_gate.py
```

The command validates all scenario and failure-stream fixtures, writes one
evidence file per case, and produces `fixtures/v1/gate/manifest.json`. A failed
case returns exit code 1. Invalid fixture or contract input returns exit code 2.

The reducer model intentionally stops at the protocol boundary. It verifies
cursor, replay, idempotency, revision, fallback, and approval invariants without
pretending to be the Android UI or the production Gateway.

## Real Android UI gate

Every Android release candidate must also pass the paired-phone UI gate; the
headless Relay test is not a substitute. With the release APK already installed
and the phone online, run:

```sh
python3 tools/real_device_ui_smoke.py \
  --adb "$ANDROID_HOME/platform-tools/adb" \
  --serial DEVICE_SERIAL \
  --expected-version 0.2.2
```

The host only starts a phone-side agent and retrieves its evidence; it never
sends screen coordinates or input events. The agent opens the installed app,
requests a new conversation (or safely reuses a genuinely empty one), proves
prior dynamic content disappeared, types a
unique prompt, taps Send, and requires the exact real Codex response in one fresh
UI snapshot. This prevents an old completion card from being counted as the
current run. Screenshots, the final UI hierarchy, timing, package version, and
the PASS/FAIL manifest are written below
`artifacts/private/device-evidence/`. The test conversation is deliberately left
visible for human inspection.

To exercise a natural prompt while still requiring a new rendered answer, add
`--prompt hi`. Both the phone-side agent and the host-side evidence validator
reject an empty answer and offline, queued, or failed delivery states.

## Phone-side Huawei installer agent

For Huawei builds that require an on-screen confirmation during an authorized
ADB package update, `device_installer_agent.sh` runs on the phone and derives
button coordinates from the phone's own UI hierarchy. It is fail-closed: it
only acts inside Android Package Installer and only accepts the exact
install/continue/done labels for the exact staged APK path and SHA-256 passed to
the agent; cancel, open, settings, and every other control are ignored. The
agent also requires the matching live package-install process before its first
action. A successful host workflow must still pull the installed APK and verify
the same release hash. Push the agent to `/data/local/tmp`, run it with
`sh AGENT_PATH APK_PATH SHA256`, then remove it.
