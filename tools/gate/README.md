# Relay Edge black-box gate

`edge_gate.py` probes the real fixed-IP Edge from macOS or CI without printing
credentials. It has no default service port and accepts only the explicit Edge
ports `27461` and `27462`; TCP 443 cannot be selected accidentally.

The baseline probe verifies:

- the supplied private CA and the served leaf's exact IP SAN;
- `GET /healthz`;
- rejection of an incorrect or wrong-port Host, repeated Host, query token,
  each supported forged forwarding header in an independent request, browser
  Origin, unknown path, and the public Node WebSocket;
- Relay close `4401` when device `Authorization` is missing.

An optional one-use Gate device pairing bootstrap enables the credentialed
checks: missing DeviceProofV1, incorrect DeviceProofV1, a valid authenticated
device WSS ready frame, an exact 12 MiB + 1 byte text message closing with
application code `4409`, and the next byte closing at the transport boundary
with standard code `1009`. The tool creates the P-256 device private key only in
process memory.
It never writes the key, access token, refresh token, pairing secret, request
body, response body, arbitrary exception text, or sensitive headers.

## Install and run

Use Python 3.10 or newer:

```sh
python3 -m venv /tmp/cheby-edge-gate-venv
/tmp/cheby-edge-gate-venv/bin/pip install \
  -r tools/gate/requirements.txt
```

Production boundary-only gate:

```sh
/tmp/cheby-edge-gate-venv/bin/python tools/gate/edge_gate.py \
  --ip 192.0.0.8 \
  --port 27461 \
  --ca-cert /absolute/path/to/relay-ca.pem \
  --boundary-only
```

Full isolated Gate gate:

```sh
chmod 0600 /absolute/private/edge-full/device.cxc1
/tmp/cheby-edge-gate-venv/bin/python tools/gate/edge_gate.py \
  --ip 192.0.0.8 \
  --port 27462 \
  --ca-cert /absolute/path/to/relay-ca.pem \
  --pairing-bootstrap /absolute/private/edge-full/device.cxc1 \
  --timeout 60
```

The pairing file must be the exact `CXC1.<base64url>` device output created
offline for the isolated Gate database. It must be an absolute, current-user
owned, regular file with mode `0600`. The file path is safe to pass on the
command line; the pairing credential itself is not. The explicit 60-second
per-operation timeout is required for the two 12 MiB public uplink probes; the
10-second default is intentionally retained for ordinary boundary checks.

The Gate database must contain three serially generated, mutually distinct
assistant bundles named `edge-full`, `reload-continuity`, and `android-e2e`.
They are created while every Gate service is stopped, using the exact
fail-closed flow in `docs/deployment/TURKEY_EDGE_RUNBOOK.md`. Its silent
validator matches only the public `assistantId` within each CXC1/Node pair and
requires all three assistants to differ; it never prints credentials. The
unused Node principals from `edge-full` and `reload-continuity` are revoked and
their raw Node JSON files deleted. Only the `android-e2e` Node is installed into
Gate Connector, after which its raw Node JSON is also deleted.
The execution order is fixed: Full Edge first, reload continuity second while
Gate is still running, Android provisioning/evidence third, and Gate teardown
last. Certificate timers are enabled only after teardown.

After the first Full Gate enrollment attempt, delete every local, Turkey, and
transfer copy of `edge-full/device.cxc1`, even if the command times out or its
outcome is unknown. Never retry it: an unknown outcome requires the complete
isolated three-role Gate state to be rebuilt from an empty Gate database.

## One-time headless Gate APK provisioning

Provisioning needs a distinct, unconsumed one-use CXC1 whose public
`assistantId` matches the Node bootstrap installed for the isolated Gate
Connector. Never reuse a CXC1 already consumed by `edge_gate.py`, and never use
a production pairing credential. This is exactly
`android-e2e/device.cxc1`; it is not the `edge-full` or
`reload-continuity` invitation. The host streams the credential through stdin
into the debuggable `.gate` package; the credential and its source path are not
instrumentation arguments. Run this section only after the reload-continuity
gate has passed.

Set the paths explicitly. Disable shell tracing before the credential is
opened:

```sh
set -euo pipefail
set +x

ADB=/absolute/path/to/adb
SERIAL=DEVICE_SERIAL
GATE_APK=/absolute/path/to/app-gate.apk
TEST_APK=/absolute/path/to/app-gate-androidTest.apk
CXC1_FILE=/absolute/private/android-e2e/device.cxc1

chmod 0600 "$CXC1_FILE"
"$ADB" -s "$SERIAL" install -r -t "$GATE_APK"
"$ADB" -s "$SERIAL" install -r -t "$TEST_APK"
```

The following clear is for first provisioning of a deliberately fresh
isolated Gate identity only:

```sh
"$ADB" -s "$SERIAL" shell am force-stop com.cheby.codex.mobile.gate.test
"$ADB" -s "$SERIAL" shell am force-stop com.cheby.codex.mobile.gate
"$ADB" -s "$SERIAL" shell pm clear com.cheby.codex.mobile.gate.test
"$ADB" -s "$SERIAL" shell pm clear com.cheby.codex.mobile.gate
```

Relay's offline bootstrap file may contain one terminal LF. Normalize only that
LF, validate the private file without printing it, and atomically stream it to
the fixed app-private filename:

```sh
python3 -c '
import os, stat, sys
path = sys.argv[1]
if not os.path.isabs(path):
    raise SystemExit("CXC1 path must be absolute")
descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit("CXC1 must be a regular file")
    if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid():
        raise SystemExit("CXC1 privacy check failed")
    raw = os.read(descriptor, 1026)
finally:
    os.close(descriptor)
if raw.endswith(b"\n"):
    raw = raw[:-1]
if not (6 <= len(raw) <= 1024 and raw.startswith(b"CXC1.")):
    raise SystemExit("Invalid CXC1 envelope")
if any(byte < 0x21 or byte > 0x7e for byte in raw):
    raise SystemExit("Invalid CXC1 characters")
sys.stdout.buffer.write(raw)
' "$CXC1_FILE" |
"$ADB" -s "$SERIAL" exec-out \
  run-as com.cheby.codex.mobile.gate sh -c \
  'set -eu; umask 077; mkdir -p files; tmp=files/.cheby-gate-enrollment.tmp; rm -f "$tmp"; cat >"$tmp"; chmod 600 "$tmp"; mv -f "$tmp" files/cheby-gate-enrollment.cxc1'
```

Run only the fixed provisioning instrumentation. It deletes the phone-side
invitation before beginning enrollment and reports only a sanitized exception
class on failure:

```sh
"$ADB" -s "$SERIAL" shell am instrument -w -r \
  -e cheby_relay_gate_provision true \
  -e class \
  'com.cheby.codex.mobile.gateway.RelayGateProvisioningInstrumentedTest#consumesPrivateInvitationAndPersistsRelaySession' \
  'com.cheby.codex.mobile.gate.test/androidx.test.runner.AndroidJUnitRunner'
```

After the first provisioning attempt, securely remove the Turkey, local, and
transfer copies regardless of success, timeout, or unknown outcome.
Do not run `pm clear`, uninstall the Gate package, or provision again between
release gates. Never retry an outcome-unknown CXC1. Instead stop every Gate service,
discard Gate Relay/Connector/App state and all three role bundles, and rebuild
from an empty Gate database. The runners below use `install -r -t`, which
preserves the app-private session while verifying the installed APK hash.

All Mac-side phone actions in this flow are package installation, one-time
app-private clearing, `run-as` file streaming, force-stop, state probes, and
instrumentation. Do not use `adb shell input`, tap, text, keyevent, Monkey,
UIAutomator, Settings, or Activity launch.

## Android restart and load evidence

`android_relay_gate.py` is the host-side runner for the isolated Gate APK. It
installs the exact Gate application and instrumentation APKs, verifies the
installed APK hashes, force-stops only those two packages, and runs
instrumentation without launching an Activity. It has no screen-coordinate,
text-input, UIAutomator, Settings or Wi-Fi operation.

The headless mode does not merely send two adjacent Turns. After the first
Turn, Android emits the whitelisted status checkpoint
`connector_restart_required` with status code `2`. The runner then restarts
only the exact
`chebycodex-turkey-gate-connector-gate-connector-1` isolated Gate Connector
over the reviewed SSH alias; every other `--connector-container` value is
rejected before SSH. It reads the
container `StartedAt` before and after and requires a changed epoch, then
streams the checkpoint nonce into an app-private mode-`0600` acknowledgement
file. Android had already armed its connection-state observer before emitting
that nonce; it requires both the matching acknowledgement and a non-online to
online transition before it sends the second Turn. The second terminal must
contain the exact line prefix `Remembered:` followed by the first marker. A
PASS also requires the final
`headless_gate_complete` checkpoint.

```sh
python3 tools/gate/android_relay_gate.py \
  --mode headless-restart \
  --adb /absolute/path/to/adb \
  --serial DEVICE_SERIAL \
  --gate-apk /absolute/path/to/app-gate.apk \
  --test-apk /absolute/path/to/app-gate-androidTest.apk \
  --ssh /usr/bin/ssh \
  --ssh-host remote-213 \
  --connector-container chebycodex-turkey-gate-connector-gate-connector-1 \
  --manifest /absolute/private/evidence/headless-manifest.json
```

The load mode sends every message through the production `AppViewModel.send`
composer, `SharedPreferencesDurableOutboxStore`, per-Thread planner and UI
timeline. It never calls `sendTurnInput` directly. The Gate Connector holds the
first load Turn at a server-side latch until a first Turn from a distinct
Thread arrives; a globally serialized Android queue therefore times out
instead of producing a false concurrency pass. The gate then requires complete
evidence for every assigned Turn, exact four-part business-key uniqueness,
same-Thread FIFO, first/last evidence for every Thread, zero duplicate
canonical/UI bubbles, reconstruction, at least two distinct latch-proven
Threads, and a zero-entry active-generation durable outbox. Android emits only
the public 32-hex run token, a SHA-256 of sorted
`threadId NUL clientMessageId NUL turnId NUL` tuples, and a one-pair
scope/generation stability digest/count. The host then invokes only the fixed
root-owned `/usr/local/sbin/chebycodex-audit-gate-business-keys` program over
SSH with fixed `--mode load --run-token` arguments. That read-only auditor
checks the same 500 tuples across Gate Relay,
Connector, Gateway and scripted fake-Codex persistence, plus request/response,
terminal, message, id-mapping, intent/outbox and authenticated-binding
invariants. The manifest passes only when Android and all four host-layer
digests match:

For the one-person demo release, `quick-demo` runs the same production queue,
server concurrency latch, reconstruction, UI de-duplication, Outbox convergence
and four-layer host audit with exactly 8 Threads and 16 Turns. Its
instrumentation budget is fixed to 120--480 seconds, so the normal invocation
fits inside the project's ten-minute verification round instead of inheriting
the old soak/load budget:

```sh
python3 tools/gate/android_relay_gate.py \
  --mode quick-demo \
  --adb /absolute/path/to/adb \
  --serial DEVICE_SERIAL \
  --gate-apk /absolute/path/to/app-gate.apk \
  --test-apk /absolute/path/to/app-gate-androidTest.apk \
  --ssh /usr/bin/ssh \
  --ssh-host remote-213 \
  --threads 8 \
  --turns 16 \
  --timeout-seconds 480 \
  --manifest /absolute/private/evidence/quick-demo-manifest.json
```

The legacy 500-Turn load mode remains available only as an optional diagnostic,
not as a release requirement:

```sh
python3 tools/gate/android_relay_gate.py \
  --mode load \
  --adb /absolute/path/to/adb \
  --serial DEVICE_SERIAL \
  --gate-apk /absolute/path/to/app-gate.apk \
  --test-apk /absolute/path/to/app-gate-androidTest.apk \
  --ssh /usr/bin/ssh \
  --ssh-host remote-213 \
  --threads 8 \
  --turns 500 \
  --manifest /absolute/private/evidence/load-manifest.json
```

The expected reply is Base64URL-encoded by the runner and decoded by a bounded
Android test helper; its raw value never enters an `adb shell` command.
Quick-demo defaults to 480 seconds and rejects larger budgets. Headless
instrumentation defaults to 1,800 seconds and load defaults to 7,200 seconds;
both are optional diagnostics rather than this product's release gate.

All modes require a JUnit `OK`, final instrumentation code `-1`, evidence
status code `2`, and the complete ordered sanitized status transcript including
standard start `1`, completion `0`, and final `-1`. Skipped or
assumption-failed statuses `-3` and `-4`, any other negative status, and
unknown statuses fail closed. Manifests are created as new mode-`0600` files
and contain hashes, changed Connector epoch hashes and whitelisted counters
only, never raw instrumentation output, credentials, nonces, raw business
tuple IDs, markers, internal Thread IDs or exception text. Android's
scope/generation digest proves one stable local pair for all 500 submissions;
the service-side audit independently proves one authenticated Gate
assistant/device/node generation mapping. Those values are not falsely treated
as the same cross-layer field because Relay/Connector/Gateway do not persist the
Android scope string.

## Android exact process-death gate

The exact-crash runner is fixed at five Android-owned boundaries and 25 real
host force-stops per boundary. The repetition count and boundary list cannot be
lowered by CLI options. It installs and hashes the same provisioned Gate APKs,
requires 125 abnormal prepare terminations and 125 independent recovery
processes, then invokes only the root-owned fixed auditor with
`--mode exact-crash --run-token`.

Create a new private manifest parent first. Do not clear or re-provision the
Gate package before this command:

```sh
install -d -m 0700 /absolute/private/evidence
test ! -e /absolute/private/evidence/exact-crash-manifest.json

python3 tools/gate/android_exact_crash_gate.py \
  --adb /absolute/path/to/adb \
  --serial DEVICE_SERIAL \
  --gate-apk /absolute/path/to/app-gate.apk \
  --test-apk /absolute/path/to/app-gate-androidTest.apk \
  --ssh /usr/bin/ssh \
  --ssh-host remote-213 \
  --expected-reply 'CHEBY_GATE_REPLY_V1_b71f3d69f2f14e25a4c71c5db6632f8e' \
  --manifest /absolute/private/evidence/exact-crash-manifest.json
```

This runner uses only APK install/hash verification, app-private `run-as`
evidence, `pidof`, `am instrument`, and `am force-stop` against the isolated
Gate packages. It never launches an Activity or sends coordinates, clicks,
keyevents, or text input. The full evidence contract and public ten-boundary
mapping are in `docs/testing/ANDROID_EXACT_CRASH_GATE.md`.

## Android fault-held queued recovery and eight-hour soak gates

`android_fault_soak_gate.py` extends the isolated Gate release check without
pretending that a reconnect alone proves queue recovery.

Fault mode runs ten cycles for each required boundary:

- App process kill: the host first stops Gate Edge and acknowledges the
  observed stopped state. Android observes non-ONLINE, uses two production
  `AppViewModel` composers sharing the one permitted Relay socket to enqueue
  A1/A2 and B1, and proves all three four-part business keys remain `QUEUED` in
  `SharedPreferencesDurableOutboxStore`. It persists only app-private recovery
  identities and emits `app_kill_outbox_ready`. The host requires that the
  instrumentation process is still alive at that checkpoint, force-stops the
  target and test packages, verifies their processes are gone, and restores
  Edge. This prepare invocation is expected to be killed; a normal JUnit
  completion is a failure. A new instrumentation process reconstructs the
  production queue, requires one canonical user bubble and one completed
  assistant bubble per business key, distinct Turn IDs, A1-before-A2 FIFO,
  B1 progress, zero remaining Outbox/fallback entries, and successful A3/B2
  post-recovery Turns.
- Edge, Relay, Connector and Wi-Fi: every cycle emits a down checkpoint before
  the host action. The host observes the component stopped (or Wi-Fi disabled)
  and acknowledges it. Only then does Android enqueue A1/A2/B1 and emit a
  three-entry Outbox checkpoint. The host re-verifies that the component is
  still down before acknowledging the held Outbox, then starts the component
  into a changed ready epoch (or restores Wi-Fi). Android applies the same
  unique-Turn, unique-bubble, FIFO, cross-Thread, A3/B2 and convergence checks.
  This is fault-held `QUEUED` recovery, not an in-flight network submission;
  it must not be used as `NETWORK_SENT` or `ACCEPTED` crash-boundary evidence.

All Docker actions use fixed validated container names and state predicates.
Before fault injection the runner records the read-only initial state of all
three fixed containers. It registers a restoration intent before issuing each
`docker stop`, so a stop that succeeds before its acknowledgement is lost
cannot escape cleanup. Finally it probes Edge, Relay and Connector
individually, starts any currently stopped component that was initially
running, and never starts one that was initially stopped. It also restores
initially enabled Wi-Fi; an unsafe restore failure keeps the gate failed.
Wi-Fi uses only USB ADB infrastructure commands `svc wifi disable/enable`,
never Activity launch, coordinates, key events, screen text, Settings UI or
UIAutomator.

Fault PASS also requires the fixed root-owned
`/usr/local/sbin/chebycodex-audit-gate-business-keys` program. The runner
passes only the fixed `--mode fault` and `--run-token`; it cannot supply a
remote command, persistence path, count or alternate contract. The auditor
requires the exact 50-cycle matrix
(App/Edge/Relay/Connector/Wi-Fi, ten times each), 250 business keys and 100
cycle-local Threads. It proves identical `(threadId, clientMessageId, turnId)`
sets at Relay, Connector, Gateway and fake Codex, `turnCount == 1`,
`effectCount <= 1`, acknowledged Relay deliveries, zero relevant Connector
intent/outbox/processed-delivery residue, and no Thread reuse between cycles.
Both Relay submission sequence and fake-Codex persisted `gate-turn` effect
sequence must prove A1→A2→A3 and B1→B2. Android independently proves every
cycle and the final checkpoint have a globally empty production
`store.list()` across every scope/generation, so a permanent `ACCEPTED` cannot
be hidden by active-scope filtering. Bubble evidence counts every assistant
for the Turn; one completed assistant plus any extra non-terminal assistant is
a failure. The reported duplicate count is summed from those real checks.

```sh
python3 tools/gate/android_fault_soak_gate.py \
  --mode faults \
  --adb /absolute/path/to/adb \
  --serial DEVICE_SERIAL \
  --gate-apk /absolute/path/to/app-gate.apk \
  --test-apk /absolute/path/to/app-gate-androidTest.apk \
  --ssh /usr/bin/ssh \
  --ssh-host remote-213 \
  --edge-container chebycodex-relay-edge-edge-gate-1 \
  --relay-container chebycodex-relay-edge-relay-gate-1 \
  --connector-container chebycodex-turkey-gate-connector-gate-connector-1 \
  --manifest /absolute/private/evidence/faults-manifest.json
```

Soak mode is not a fast load test with an eight-hour label. Android uses
`SystemClock.elapsedRealtime()` to burst the first Turn on all eight Threads
for the server concurrency barrier, then monotonically paces the remaining
work. The 500th Turn is not admitted until at least 28,800,000 ms have elapsed.
PASS independently requires both Android and host monotonic durations of at
least eight hours, exactly 500 production Turns over eight Threads, zero
global `store.list()` entries across all scope/generations, zero duplicate
bubbles derived from exact per-Turn assistant checks, reconstruction, and the same read-only
Relay/Connector/Gateway/fake-Codex business-key audit used by load mode.

```sh
python3 tools/gate/android_fault_soak_gate.py \
  --mode soak \
  --adb /absolute/path/to/adb \
  --serial DEVICE_SERIAL \
  --gate-apk /absolute/path/to/app-gate.apk \
  --test-apk /absolute/path/to/app-gate-androidTest.apk \
  --ssh /usr/bin/ssh \
  --ssh-host remote-213 \
  --manifest /absolute/private/evidence/soak-manifest.json
```

The private mode-`0600` manifests contain APK hashes, aggregate counts,
business-key digests and hashed container epochs. They exclude raw
instrumentation, nonces, markers, credentials and internal Thread IDs. The
fault gate is executable only against the temporary isolated Gate stack; it
must not target production container names.

`--ca-cert` accepts one PEM/DER private CA. During an approved CA-overlap
rotation it also accepts exactly two distinct PEM CAs in current-then-next
order, so the old authenticated socket and the new candidate connection can
both validate. More than two authorities, a duplicate, or a non-CA certificate
fails closed.

Exactly one mode is required. `--boundary-only` schedules only the
credential-free checks and therefore reports no artificial skips. A Gate
pairing bootstrap schedules the full credentialed checks. If enrollment fails,
dependent credential checks are marked failed rather than skipped. Any executed
failure or setup failure returns nonzero; a successful release report has
`fail=0 skip=0`.

## Certificate reload continuity

The release proof holds two authenticated WSS connections across the same
reload: the isolated `27462` role and a fresh continuity-only assistant on the
real production port `27461`. The production assistant is not the phone
assistant. Its Node credential is born revoked, it cannot route messages, and
pairing accepts only the exact continuity device name during the first hour.
The guarded revoke and purge remain idempotent after that hour so stale cleanup
cannot become stranded; they still require the exact continuity purpose and
assistant/Node binding with no deliveries, requests, idempotency rows, or
stream movement. The Runbook creates its one-use CXC1 and public-only metadata
through `bootstrap-ephemeral-continuity`.

Create one fresh private signal directory. Every output path must be new:

```sh
SIGNAL_DIR=$(mktemp -d /tmp/cheby-relay-reload.XXXXXX)
chmod 0700 "$SIGNAL_DIR"
EXPECTED_NEW_LEAF_SHA256=$(
  python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["leafCertificateSha256"])' \
    /absolute/path/to/candidate/release.json
)

/tmp/cheby-edge-gate-venv/bin/python tools/gate/edge_gate.py \
  --ip 192.0.0.8 \
  --port 27462 \
  --ca-cert /absolute/path/to/relay-ca.pem \
  --reload-continuity-bootstrap /absolute/private/reload-continuity/device.cxc1 \
  --expected-new-leaf-sha256 "$EXPECTED_NEW_LEAF_SHA256" \
  --ready-signal "$SIGNAL_DIR/gate-ready" \
  --reloaded-signal "$SIGNAL_DIR/gate-reloaded" \
  --manifest "$SIGNAL_DIR/gate-continuity.json" \
  --reload-wait-timeout 600

/tmp/cheby-edge-gate-venv/bin/python tools/gate/edge_gate.py \
  --ip 192.0.0.8 \
  --port 27461 \
  --ca-cert /absolute/path/to/relay-ca.pem \
  --reload-continuity-bootstrap "$SIGNAL_DIR/production-device.cxc1" \
  --expected-new-leaf-sha256 "$EXPECTED_NEW_LEAF_SHA256" \
  --ready-signal "$SIGNAL_DIR/production-ready" \
  --reloaded-signal "$SIGNAL_DIR/production-reloaded" \
  --cleanup-ready-signal "$SIGNAL_DIR/production-cleanup-ready" \
  --revoked-signal "$SIGNAL_DIR/production-revoked" \
  --manifest "$SIGNAL_DIR/production-continuity.json" \
  --reload-wait-timeout 600
```

Run the commands in separate terminals. Wait for both mode-`0600` ready files,
then perform exactly one reviewed rotation. Only after the rotation returns
success, create both completion signals:

```sh
python3 -c \
  'import os,stat,sys; ss=[os.stat(p,follow_symlinks=False) for p in sys.argv[1:]]; assert all(stat.S_ISREG(s.st_mode) and stat.S_IMODE(s.st_mode)==0o600 and s.st_uid==os.getuid() for s in ss)' \
  "$SIGNAL_DIR/gate-ready" "$SIGNAL_DIR/production-ready"
TURKEY_SSH_HOST=replace-with-reviewed-ssh-alias
ssh "$TURKEY_SSH_HOST" \
  'sudo systemctl start chebycodex-relay-cert-rotate.service'
(umask 077
 printf 'RELOADED\n' > "$SIGNAL_DIR/gate-reloaded"
 printf 'RELOADED\n' > "$SIGNAL_DIR/production-reloaded")
```

After the production process has verified its old socket and the new
certificate, it creates `production-cleanup-ready` and waits at most 60 seconds.
Use the public-only metadata with the guarded
`revoke-ephemeral-continuity` command from the Runbook, then create
`production-revoked`. The gate sends one more application ping and requires
the already-established `27461` socket to close with `4401`. Only a PASS
manifest permits `purge-ephemeral-continuity`; purge removes the fresh
assistant, Node, Device, credentials, streams, and its proof nonces without
changing the phone assistant.

Each immutable mode-`0600` continuity manifest records the target port, old and
new leaf digests, pre/post-reload pings, new-connection result, and (for
`27461`) verified revocation. It contains no assistant/device IDs, credentials,
private keys, signal paths, or arbitrary exception text. A stale signal, wrong
mode, symlink, unexpected leaf, dropped old socket, failed pong, missing
revocation, or pre-existing manifest fails closed. Delete every CXC1 copy after
its first attempt and never retry it.

Run the fixed negative-certificate gate before the live proof:

```sh
mkdir -m 0700 /absolute/private/certificate-evidence
relay/.venv/bin/python tools/gate/certificate_failure_gate.py \
  --python "$(pwd)/relay/.venv/bin/python" \
  --manifest \
    /absolute/private/certificate-evidence/negative-certificate.json
```

The executable suite has 15 zero-skip tests for wrong key, wrong IP SAN, wrong
chain, missing/invalid/failed previous rollback, Nginx validation, reload, and
public fingerprint failures. It clears inherited pytest injection variables,
disables plugin autoload, and forces empty addopts plus strict xfail handling.
Its immutable mode-`0600` manifest contains fixed case IDs, exact test counts,
and the pre-test source hashes only when the post-test hashes match; it never
stores raw pytest output.

## Final isolated-state teardown

After every Android manifest and server audit is captured, use the
realpath-guarded teardown in `docs/deployment/TURKEY_EDGE_RUNBOOK.md`. It stops
and verifies Gate Connector, Gate Edge, and Gate Relay, revokes the Android
Node offline, and empties and verifies all active Gate Connector, Relay
DB/WAL/SHM, bootstrap-role, and public-identity state. It then uninstalls
`com.cheby.codex.mobile.gate.test` and `com.cheby.codex.mobile.gate` over USB
ADB without `-k`, verifies that neither package nor process remains, switches
Turkey to the verified production-only firewall, and closes cloud TCP `27462`.
The Gate Relay and bootstrap roots must both be empty before another
three-role bootstrap is allowed. These final ADB commands never launch an
Activity or send coordinates, key events, text, or UI automation.

## Local tests

```sh
/tmp/cheby-edge-gate-venv/bin/python -m pytest -q \
  tools/gate/test_edge_gate.py \
  deploy/relay-edge/tests/test_certificate_tools.py \
  deploy/relay-edge/tests/test_certificate_shell_recovery.py
```
