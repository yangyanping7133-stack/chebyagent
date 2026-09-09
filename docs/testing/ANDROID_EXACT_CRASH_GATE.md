# Android exact process-death gate

This gate is release evidence, not an object-reconstruction test. For each
Android-owned boundary, an instrumentation process:

1. sends A1 through the production `AppViewModel`, production
   `SharedPreferencesDurableOutboxStore`, and production Relay transport;
2. crosses exactly one boundary;
3. inserts A2 behind A1 and B1 on another Thread as durable `QUEUED` recovery
   sentinels;
4. atomically writes an app-private checkpoint containing the four-field
   business-key digest and the live PID;
5. blocks forever.

The host confirms that the instrumentation process is still alive, validates
the target and sentinel Outbox states, and runs:

```text
adb shell am force-stop com.cheby.codex.mobile.gate
```

A normal JUnit return is a failure. A separate instrumentation process then
reconstructs the production gateway and ViewModel, verifies A1 exactly once,
same-Thread A1/A2 FIFO, completed B1 on the other Thread, one canonical user
bubble per business key, and a globally empty durable Outbox. The fixed
read-only root audit proves the same 375 tuples across Relay, Connector,
Gateway, and scripted Codex, with one Turn and at most one effect per key.

The runner is fixed at five boundaries × 25 force-stops:

- `ENQUEUE`: A1 is durably `QUEUED` immediately after `enqueue` commits.
- `NETWORK`: the target `turns.start` frame write returned, Relay acceptance
  has not been released to the request, and A1 remains `QUEUED`.
- `ACCEPTED`: the Android reader matched the target Relay `messageId` and
  received its Accepted frame, but has not released the request; A1 remains
  `QUEUED`.
- `ANDROID`: Android durably committed `ACCEPTED(turnId)`.
- `CLEANED`: Android durably removed A1 after its matching terminal Turn.

The Gate-only hook implementation is compiled under `androidTest`. Production
constructors default to a no-op hook and reject every non-no-op implementation
unless the application is the debuggable `.gate` package on TCP 27462. Release
and migration APKs therefore cannot arm or pause a delivery checkpoint.

## Preconditions and runner entry

- Provision the isolated Gate APK once with the unconsumed one-use CXC1 flow in
  `tools/gate/README.md`. Its `assistantId` must match the Gate Connector Node.
- After that provisioning PASS, do not run `pm clear`, uninstall, or provision
  again. Every runner reinstall is `install -r -t` and preserves the encrypted
  app-private session.
- Gate Edge, Relay, and fake Connector must be isolated from production and
  reachable only through temporary TCP 27462. The phone must already be online.
- The reviewed `/usr/local/sbin/chebycodex-audit-gate-business-keys` must be
  root-owned mode `0755`, support `--mode exact-crash`, and be callable through
  `sudo -n` on the reviewed SSH alias.
- The manifest parent must already exist with private permissions, and the
  manifest path must be new.

Run the complete, non-configurable five-by-25 matrix:

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

The runner itself installs and verifies both APK hashes. PASS requires exactly
125 live prepare checkpoints terminated by the host, 125 independent recovery
processes, 375 unique Turns over 250 cycle-local Threads, zero duplicate
bubbles, a globally empty Outbox after every recovery, and a matching
four-layer read-only audit.

The Mac must not operate the phone screen. This flow permits only package
installation, app-private `run-as` evidence, `pidof`, `am instrument`, and
`am force-stop` for the two isolated Gate packages. It forbids
`adb shell input`, tap, text, keyevent, Monkey, UIAutomator, Settings, and
Activity launch.

## Server SIGKILL release-evidence gate

After the Android exact-crash manifest passes, commit the reviewed source and
run the server release gate from a clean worktree. The Android manifest must
remain mode `0600`. Supply the independently calculated SHA-256 values; the
gate hashes the three files before and after the run, verifies that both
installed APK hashes in the Android evidence match, and verifies that the Git
revision and tree remain unchanged.

The output path must be new. Replace only the all-caps values in this exact
command:

```sh
test -z "$(git status --porcelain=v1 --untracked-files=all)"
test ! -e /absolute/private/evidence/server-sigkill-manifest.json

connector/.venv/bin/python tools/gate/server_sigkill_release_gate.py \
  --source-revision COMMITTED_SOURCE_REVISION \
  --android-manifest /absolute/private/evidence/exact-crash-manifest.json \
  --android-manifest-sha256 ANDROID_MANIFEST_SHA256 \
  --gate-apk /absolute/path/to/app-gate.apk \
  --gate-apk-sha256 GATE_APK_SHA256 \
  --test-apk /absolute/path/to/app-gate-androidTest.apk \
  --test-apk-sha256 GATE_TEST_APK_SHA256 \
  --manifest /absolute/private/evidence/server-sigkill-manifest.json
```

The command has no test-selection or repetition override. It disables pytest
plugin auto-loading and retry injection, loads only the required asyncio
plugin, and selects eight exact test node IDs. Each reviewed test performs 25
child-process `SIGKILL` and recovery iterations, for 200 server crashes. PASS
also statically verifies that the reviewed test source retains the exact five
parameter values, four fixed `builtins.range(25)` loops, required direct
SIGKILL calls, and a runtime-completed iteration counter asserted at 25.
It requires pytest exit `0`, exactly eight distinct JUnit cases, eight passes,
zero failures, zero errors, zero skips, zero retries, and no missing,
duplicate, or unexpected case. A skipped, flaky/retried, partial, timed-out,
or malformed run is never PASS.

The fresh manifest is atomically created mode `0600` without replacement. It
binds the source revision and tree, supplied and observed Android/APK hashes,
all eight checkpoint names, 25 repetitions per checkpoint, exact case results,
and the ten-boundary composition below. A completed failing run writes
`outcome: FAIL` and exits `1`; configuration or safe-write failure exits `2`
and cannot overwrite an existing manifest.

## Public ten-boundary mapping

The release manifest composes the five Android process-death boundaries from
the supplied Android-manifest hash with five public server checkpoints from
the eight-checkpoint server matrix:

| Public boundary | Evidence owner |
| --- | --- |
| 1. ENQUEUE_PERSISTED | Android `ENQUEUE` force-stop ×25 |
| 2. NETWORK_SENT_UNCONFIRMED | Android `NETWORK` force-stop ×25 |
| 3. RELAY_SUBMITTED | server `relay_command_committed` SIGKILL ×25 |
| 4. ACCEPTED_SENT/RECEIVED | Android `ACCEPTED` force-stop ×25 |
| 5. CONNECTOR_INTENT | server `connector_execution_intent` SIGKILL ×25 |
| 6. GATEWAY_RESERVATION | server `gateway_turn_reservation` SIGKILL ×25 |
| 7. CODEX_ACCEPTED | server `scripted_codex_accepted` SIGKILL ×25 |
| 8. ANDROID_ACCEPTED | Android `ANDROID` force-stop ×25 |
| 9. TERMINAL_COMMITTED | server `gateway_terminal_committed` SIGKILL ×25 |
| 10. OUTBOX_CLEANED | Android `CLEANED` force-stop ×25 |

The same server manifest also requires
`connector_response_committed`, `relay_response_committed`, and
`connector_response_outbox_removed`, each ×25. Those three internal response
boundaries are recorded but do not inflate the public count beyond ten.

The exact-crash audit does not impose an ordering race between A2 and B1. It
requires A1 before A2 and successful, isolated B1 completion. The separate
two-Thread load barrier is the deterministic proof that different Threads are
not globally serialized.
