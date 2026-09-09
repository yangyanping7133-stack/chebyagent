# Standalone 4.1 Pristine Offline Gate — 2026-08-09

## Decision

Private one-APK delivery: **GO**.

Public redistribution: **HOLD** until the license and corresponding-notice
obligations described in the 4.1 architecture record are completed.

## Candidate

- package: `com.termux`
- version: `0.6.0-embedded-offline-dev` (`versionCode 2`)
- APK: `Android/appliance/build/outputs/apk/debug/appliance-debug.apk`
- APK size: 271 MiB
- APK SHA-256:
  `d336b563414f5c23f869fb0520ecfc839c59d430c6f168d7219816bc755f15fa`
- device family: Huawei JUY-AL00, ARM64

The candidate is the exact APK archived for 4.1 delivery. No APK rebuild or
runtime substitution occurred between the offline and online portions of this
gate.

## Destructive boundary

The Founder separately authorized clearing `com.termux` application data. The
gate then executed `pm clear com.termux` and verified that both `files` and
`shared_prefs` were absent before launch. This removed all previous embedded
Debian installations, Codex sessions, local settings, and the phone-side login
file. The deleted phone state is not recoverable from the release archive; the
APK and source artifacts are recoverable, and authentication must be imported
again.

No other Android package was cleared or uninstalled.

## Offline first launch

Before launch:

- Wi-Fi: off;
- mobile data: off;
- airplane mode: unchanged and off;
- active default Android network: none;
- OpenAI HTTPS probe: failed during DNS resolution with exit 28.

The app cold-launched at approximately `2026-08-08T17:45:12Z`. The embedded
runtime wrote `ready-4.1.0-dev1` at approximately `17:45:27Z`, so pristine
offline initialization completed in about 15 seconds.

The completed runtime passed all of these checks while the phone still had no
default network:

- `provision-failed` absent and `ready-4.1.0-dev1` present;
- every staged asset hash passed;
- provisioning log had no `apt-get`, `curl`, `wget`, `proot-distro`, `http://`,
  or `https://` match;
- PRoot present in the minimal embedded prefix;
- `proot-distro` absent from the prefix;
- Termux Python absent from the prefix;
- Debian root filesystem: 640 MiB before online Codex state was created;
- Codex CLI: `0.147.0`;
- Node.js: `20.19.2`;
- Python: `3.13.5`;
- `/root/.codex/auth.json` absent;
- app-server `readyz`: ready;
- PhoneBridge health: okay, with the embedded phone executor online;
- large Debian and PRoot staging archives removed after activation.

The offline UI correctly displayed the login-file import requirement rather
than claiming a usable online model session.

## Authentication and online turn

The test restored the original phone switch state: Wi-Fi on, mobile data on,
airplane mode off. VPN ownership remains with the user and is not provisioned by
the APK.

An older archived login file was rejected by OpenAI with `token_expired`; it was
not counted as a product failure or a successful login. A newer
operator-authorized login file was first validated with a real, read-only Codex
request on its source runtime, then copied through a mode-0600 temporary file.
The temporary Mac copy was deleted immediately after the phone import, no
credential content entered logs or evidence, and the source copy was not
modified by the transfer.

The phone then passed a real UI turn. The unambiguous prompt and assistant reply
were both `STANDALONE41E2EOK`.

## Recovery

After the successful turn, the application was force-stopped and cold-started.
It recovered:

- the same thread and visible model reply;
- local app-server readiness;
- PhoneBridge health and phone-online state;
- `codex login status`: `Logged in using ChatGPT`.

Final phone switches remained Wi-Fi on, mobile data on, and airplane mode off.

## Evidence

- offline login-required UI:
  `artifacts/private/real-device/standalone-4.1-20260809/chebycodex-4.1-pristine-offline-ready.png`
  (`d8d727f443dab4cd7912d2d265cd4c1e8d4a90f9efce33412d1ff1e077963dcf`)
- real online prompt and reply:
  `artifacts/private/real-device/standalone-4.1-20260809/chebycodex-4.1-pristine-online-e2e-ok.png`
  (`4321c49ee2e3aa788aa69505e7c89982b3185e1c1143329a129cd099fe401cc1`)
- post-force-stop recovery:
  `artifacts/private/real-device/standalone-4.1-20260809/chebycodex-4.1-pristine-restart-recovery.png`
  (`de684f0afbf1f325913d5a60fa77de9132d7f5ef53134723faf47894b79fe57b`)

Private evidence intentionally contains no `auth.json`, token, pairing secret,
SSH material, or credential-bearing log.

## User path proven by this gate

1. Install the single ChebyCodex APK on the Android phone.
2. Provide the phone's ordinary network/VPN path as needed.
3. Open ChebyCodex and wait roughly 15 seconds for the embedded offline runtime
   to initialize; no runtime download or separate Termux installation occurs.
4. Import a current user-owned Codex ChatGPT login file.
5. Create a thread and use the complete embedded Codex CLI-backed experience.
