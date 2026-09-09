# ChebyAgent repository structure

This document records the source-of-truth and product boundaries of the current
repository. It is a maintenance map, not a proposal to refactor the stable
`0.7.0` Android package.

## Product paths

The repository contains two related delivery paths:

1. **Standalone Android appliance** — the current `0.7.0` user release. The
   output is one ARM64 APK from `Android/appliance`.
2. **Distributed mode** — the retained `gateway`, `relay`, `connector` and
   `deploy` stack. It remains available for server-backed deployments but is
   not required for normal standalone APK use.

The distinction should remain explicit in documentation, tests and release
assets. Do not make a distributed-service prerequisite part of the standalone
installation path.

## Android source-of-truth

```text
Android/app source -------------------> embedded/chebyUi ---+
Android/phoneNode source -------------> embedded/phoneNode -+--> appliance APK
embedded/runtimeApi ----------------------------------------+
embedded/termux* + third_party/termux-app ------------------+
appliance/runtime + connector + skills ---------------------+
```

- `Android/app` owns the conversation UI, Codex app-server protocol, model
  settings, image attachments, recovery and rich-result rendering.
- `Android/phoneNode` owns screen observation, screenshots, accessibility
  gestures, PhoneBridge and owner-only commerce authorization.
- `Android/embedded/chebyUi` and `Android/embedded/phoneNode` point their Gradle
  source sets at those two source trees. They are packaging adapters, not
  copied implementations.
- `Android/embedded/runtimeApi` is the narrow interface between the UI and an
  APK-owned runtime.
- `Android/appliance` is the final composition root. It embeds Termux source,
  runtime archives, project notices and bundled Skills.
- `Android/testIme` and `Android/deviceOwnerUpdater` are test/managed-device
  support applications; neither is part of the ordinary user installation.

## Non-Android areas

| Path | Ownership boundary |
|---|---|
| `connector` | PhoneBridge, local MCP and ACE memory adapters shared with runtime/deployment paths |
| `gateway` | Codex gateway and durable protocol projection for distributed mode |
| `relay` | Relay registration, connection and review service for distributed mode |
| `deploy` | Host-specific deployment configuration and audit tooling |
| `contracts` | Versioned wire contracts; changes require fixtures and compatibility tests |
| `fixtures` | Golden protocol examples used by Android and Python tests |
| `skills` | Human-readable task policies copied into the standalone APK at build time |
| `third_party` | Pinned upstream source that keeps its original license and provenance |
| `tools` | Local-only build, verification, inventory and release utilities |
| `docs` | Product, architecture, deployment, testing and licensing evidence |

## Current assessment

The layout is reasonable for a repository that preserves both standalone and
distributed modes. The strongest boundaries are the single appliance
composition root, the small runtime API, pinned upstream source and explicit
protocol fixtures. Large runtime binaries and private evidence are correctly
kept outside Git history.

The main readability cost is historical naming: the Gradle root and some
modules still say ChebyCodex, while the product is now ChebyAgent. The mixed
standalone/distributed history also makes the repository look larger than the
normal APK path. Renaming packages or moving shared source would create broad,
low-value risk for a stable release, so documentation is the preferred fix for
`0.7.x`.

For a future major version, the safe cleanup candidates are:

1. rename display-only Gradle/project labels without changing Android package
   or signing identity;
2. group historical acceptance notes under a dated archive index;
3. make standalone and distributed entrypoints visually distinct at the top
   level while retaining shared contracts;
4. split very large UI/gateway source files only with behavior-preserving tests.

Do not move or rename `com.termux`, the application ID, source-set paths,
runtime asset names or signing inputs as a cosmetic cleanup. Those values are
part of the installed-package and release-integrity contract.
