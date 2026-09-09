# ChebyAgent Contribution Rules

## Product

- Build an Android-first client paired one-to-one with the user's dedicated remote Codex container.
- The primary UX is a Feishu-like single bot identity with native Codex multi-thread management.
- Opening the app restores the last active Thread; user messages create Turns in that Thread.
- Rich messages are typed, stateful, patchable, and business-agnostic. Never hard-code rental, maps, or another vertical into the rendering/runtime layer.
- The signature interaction is an in-place evolving task card plus an execution handoff rail.

## Architecture

- The Android app talks to an authenticated gateway. Never expose raw Codex app-server to the public Internet.
- Mobile conversation identity maps one-to-one to a Codex Thread.
- Use versioned event envelopes, monotonic sequence numbers, idempotency keys, and resumable event cursors.
- Unknown rich blocks must degrade safely to readable text.
- Sensitive side effects require explicit confirmation and an audit event.

## Delivery

- Keep commits coherent and run the smallest relevant test before submitting them.
- GitHub Actions is disabled for this repository. Do not add files under
  `.github/workflows/`, use GitHub-hosted runners, or compile, sign, package, or
  publish ChebyAgent through GitHub automation. Run verification locally or on
  explicitly controlled project hardware and preserve bounded evidence.
- Treat external checkouts and device state as independent inputs; never absorb an
  unrelated dirty worktree into this repository.
- Android 12 or newer on arm64 is the primary appliance target.
- Do not call a static mock screen complete. The fake and real gateways must share the same protocol-facing interface and reducer.
- Before completion, preserve build, install, launch, screenshot, session, reconnect, and failure evidence.

## Safety

- Never log or render pairing secrets, bearer tokens, raw app-server endpoints, internal Thread IDs, stack traces, or debug JSON.
- Store pairing credentials with Android system-backed secure storage.
- Archive and permanent delete are distinct; permanent delete requires explicit confirmation.
- Do not perform destructive phone operations, permission escalation, root, bootloader, or device reset without a separate explicit Founder instruction.

## Licensing and releases

- The project is distributed under GPL-3.0-only; retain `LICENSE`, `NOTICE`, and
  `THIRD_PARTY_NOTICES.md` in source and binary distributions.
- Do not commit release APKs, signing keys, Linux root filesystems, Codex CLI
  binaries, Termux bootstrap archives, or large corresponding-source archives.
  Publish those as GitHub Release assets with SHA-256 checksums.
- A release that contains GPL-covered Termux or Linux components must publish the
  matching corresponding-source archives described in
  `docs/RELEASE_AND_CORRESPONDING_SOURCE.md`.
- Never publish production credentials, device serials, operator paths, or live
  deployment addresses. Use documented placeholders in examples.
- Upload Release assets only from a locally verified staging directory. GitHub is
  an artifact host for this project, not a build or signing environment.
