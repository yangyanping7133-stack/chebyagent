# Standalone Self-Contained Runtime 4.1

## Outcome

The 4.1 appliance is a single APK whose first-run runtime provisioning does not
download Debian, Codex, Node.js, Python, Git, PRoot, or another package. The APK
contains two pinned ARM64 Zstandard archives:

- a 165 KiB Termux-prefix overlay containing PRoot 5.1.107.89 and its two shared
  library dependencies;
- a 207 MiB clean Debian 13.6 root filesystem containing the unchanged Codex CLI
  0.147.0 release, Node.js 20.19.2, Python 3.13.5, Git, and the runtime tools.

The current debug APK is 271 MiB. On the pristine real-device gate, the Debian
root filesystem was 640 MiB immediately after offline provisioning, 685 MiB
after authentication and real Codex turns, and the complete application files
tree was 762 MiB. The copied provisioning archives are deleted after a
successful atomic activation.

## State boundary

The Debian archive was captured from the passed 4.0D ARM64 runtime with these
trees omitted before any data left the Android application sandbox:

- `/root` and `/home`, including `auth.json`, Codex sessions, memory, plugins,
  shell snapshots, and Cheby controller tokens;
- APT package caches and package indexes;
- runtime logs, temporary files, Android bind-mount placeholders, machine ID,
  and SSH host keys.

First run creates a new local PhoneBridge token and Codex MCP configuration.
ChatGPT authentication remains a user-owned import after installation.

## Runtime path

`TermuxEmbeddedRuntime` installs the bundled PRoot overlay, atomically extracts
Debian into `files/usr/var/lib/cheby-runtime/debian-rootfs`, and launches it with
the fixed `cheby-enter-debian` wrapper. `proot-distro`, Termux Python, and the
online Docker-image installer are no longer part of the runtime path.

Every APK runtime asset is covered by `runtime-assets.sha256`. The two large
archives are also pinned in `runtime.lock` and verified by Gradle before they can
enter the APK. The APK keeps `.zst` assets uncompressed to avoid double
compression.

## Current evidence

- clean archive extracted into an independent device path: Codex CLI, app-server
  help, Node, and Python passed;
- `:appliance:assembleDebug`: PASS;
- `:appliance:lintDebug`: PASS;
- `tools/standalone/appliance_static_gate.py`: PASS, including the offline
  provisioning rule and stored runtime archives;
- non-destructive APK upgrade on JUY-AL00: the 4.1 root filesystem activated,
  Codex 0.147.0 and the code-mode host verified, large staging assets were
  removed, and local app-server plus PhoneBridge health checks passed;
- the new root filesystem had no `auth.json` before credential restoration.
- after private credential restoration and an application force-stop, the local
  app-server, enabled PhoneBridge MCP, online PhoneBridge device, and a real
  Codex UI turn all passed from the new 4.1 root filesystem.
- destructive pristine-sandbox gate on JUY-AL00: `pm clear com.termux` removed
  the complete previous application state, Wi-Fi and mobile data were disabled,
  Android reported no active default network, and an OpenAI probe failed with a
  DNS timeout;
- the exact final APK reached `ready-4.1.0-dev1` in about 15 seconds without a
  network, with Codex CLI 0.147.0, Node.js 20.19.2, Python 3.13.5, local
  app-server, and local PhoneBridge all passing;
- the pristine Termux prefix contained PRoot but no `proot-distro` or Termux
  Python, the provisioning log contained no online installer or URL, both large
  staging archives were removed, and the fresh Debian root had no `auth.json`;
- after restoring the original phone network switches and importing an
  operator-authorized, independently validated ChatGPT login file, a real UI
  turn returned `STANDALONE41E2EOK`;
- application force-stop and cold restart recovered the same thread and reply,
  app-server readiness, PhoneBridge online status, and the Codex login state.

The detailed destructive gate and evidence hashes are recorded in
`docs/testing/STANDALONE_4_1_PRISTINE_OFFLINE_GATE_20260809.md`.

## Delivery boundary

The private one-APK delivery gate is complete. A user installs one APK, supplies
their own phone VPN/network path where required, lets the first launch initialize
from embedded assets without downloading a runtime, imports their own current
Codex ChatGPT login file, and starts a conversation. No separate Termux APK,
Debian download, Codex download, or computer-assisted bootstrap is part of this
path.

Public redistribution also remains blocked until the project records an explicit
license/legal decision for embedding the upstream Codex release binary and
ships complete corresponding notices/source obligations for all bundled Termux
and Debian components.
