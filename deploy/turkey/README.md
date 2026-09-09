# Turkey Edge release bundle — SUPERSEDED

> **Historical evidence only.** This direct Gateway/Codex public-IP
> Edge/certbot path is superseded by the outbound Connector plus the independent
> fixed-IP Relay Edge. Do not activate this bundle for the Android APK. Current
> entrypoints are `docker-compose.connector.yml` and
> `../relay-edge/docker-compose.yml`; see `README_CONNECTOR.md` and
> `docs/deployment/TURKEY_EDGE_RUNBOOK.md`.

This directory is a reviewable, fail-closed release package. The r16 immutable
runtime/Edge build and vulnerability/secret scan gate passed on `remote-213`.
The package does not deploy anything by itself, has not been activated as a
production service, and has not passed the public-IP acceptance gates.

The production shape is deliberately small:

- `edge` is the only service with host bindings. It binds the ECS-local address
  mapped to the dedicated EIP on TCP 80 and 443 only. Port 80 serves only
  HTTP-01 challenges and otherwise redirects to the literal HTTPS IP.
- `runtime` contains the Python Gateway and pinned Codex CLI 0.144.6. The
  Gateway spawns Codex app-server over stdio as the same non-root UID, with
  Code Mode Host disabled so tool execution remains native to Codex.
  Runtime port 8080 is private to the fixed internal Docker network. Its
  default 18 GiB/8 CPU ceiling is shared by Codex xhigh and Gateway overhead.
- `certbot` uses the official Certbot 5.7.0 image and webroot validation.
  `cert-export` validates the exact IP SAN, key match, 160-hour lifetime, chain
  signatures/constraints, and system trust before atomically switching the
  read-only certificate view used by Edge. Activation failure restores and
  publicly revalidates the previous release, or returns a first deployment to
  bootstrap mode with no TLS listener.
- OAuth state, workspace, Gateway SQLite data, asset staging, ACME state, and
  the exported Edge certificate are separate persistent volumes.

All containers use a read-only root filesystem, no-new-privileges, bounded
memory/PIDs/CPU, no added capabilities, and exact `cap_drop: ALL`. The
capability-free certificate exporter leaves root-owned certificate files
readable only through the dedicated Edge volume. Edge access logging is off;
no request body, Authorization header, pairing secret, refresh token, Codex
protocol frame, or internal Thread ID is logged by the deployment layer.

Upstream container bases and Certbot are digest-pinned. The native Codex
platform tarball is checked against the official npm SHA-512 integrity value,
and every Python dependency (including transitives) is version- and hash-locked.
The Docker context is deny-by-default and excludes tests, virtual environments,
bytecode, caches, and unreviewed development files. The runtime also bounds
Uvicorn WebSocket messages/queues and disables per-message compression. The
build gate is not a pass until the context-built Trivy
`0.72.0-cheby.1` scanner checks the exact materialized Docker context and every
deployable or locally built tooling image with both vulnerability and secret
scanners, finds no HIGH/CRITICAL vulnerability or secret, and proves the vulnerability database
is at most 48 hours old. The scanner is built in digest-pinned Go 1.26.5 from
the exact upstream Trivy commit archive; only `oras-go/v2` is changed from
2.6.0 to 2.6.2. Source, pre/post module files, module graph, static binary, CA
bundle, Dockerfile, and scanner lock hashes are all checked. Its scratch image
must contain only that binary and CA bundle, run as UID/GID 65532, and expose
the exact reviewed labels and entrypoint.
Both Go tooling builds disable workspace/user configuration, clear every
private/no-sum/no-proxy/insecure override, materialize verified dependencies
through the one pinned proxy, and perform the final reproducible compile in a
separate `RUN --network=none` step.
Their scratch runtime metadata explicitly locks the standard absolute `PATH`
and `/` working directory in addition to the non-root user, exact entrypoint,
labels, two-file rootfs, and empty command/volume/port/healthcheck surface.
For Cosign, the untouched upstream `go.sum` is checked before dependency
materialization. Go 1.26.5 `go mod download all` deterministically adds the
missing all-module checksum records, and that resulting `go.sum` is checked
against a separate reviewed hash before the offline build. The upstream and
materialized hashes are both immutable locks; no checksum is accepted from a
mutable build result or waived.

The official GHCR Trivy 0.72.0 index remains a bootstrap cross-scanner only.
Before it can run, a source-locked Cosign `3.1.2-cheby.1` verifier, rebuilt with
Go 1.26.5 into a two-file scratch image, must pass the custom scanner. That
hardened local verifier then checks the official Trivy image's keyless GitHub
Actions identity and OIDC issuer. The official Cosign 3.1.2 image is recorded
only as upstream provenance and is never pulled or executed. After verification,
official-to-Cosign, official-to-custom, custom-to-Cosign, and custom-to-custom
scans must be clean. Official-to-official and custom-to-official scans must both
reproduce the same locked set of exactly 16 HIGH findings and zero secrets, so
the patched scanner cannot silently lose detection coverage.

The custom scanner downloads the DB into a fresh ephemeral host directory as
UID/GID 65532. Its cgroup memory ceiling is exactly 2 GiB: Trivy 0.72's OCI DB
download and extraction exceeds 1 GiB, while a remote isolated reproduction
completed within the 2 GiB bound. A failure retains Trivy's own diagnostics
and reports the scanner exit status before the gate returns BLOCKED. Only this
bounded downloader and the later hardened Cosign
keyless verification have network access. Every context, archive, and cross-scan
runs network-none, read-only, with explicit offline-scan semantics; metadata and
version checks also run network-none. None receives a Docker socket, and all use
the DB mounted read-only with memory-only scan
cache. Every Trivy filesystem/image operation is bounded to 15 minutes and
signature verification to 3 minutes. DB metadata and database hashes must remain identical before and after
all scans. Images are exported by exact ID, first validated root-owned/mode
0600, then changed to UID 65532/mode 0400 and validated again before non-root
scanning. Each report is bound to its expected archive path, artifact type,
artifact ID, image ID, and exact scanner version. Missing scanner, signature,
database metadata, scan result, target binding, or reviewed host Docker socket
is BLOCKED.
Package inventories are field-, type-, string-, list-, per-result-, and
report-size bounded. Trivy 0.72 represents a Go binary's main module without a
`Version` when the binary lacks a module version. That one exception requires
the exact versionless `gobinary` root shape: `Name == ID`, a canonical
domain-qualified Go module path, root relationship, `AnalyzedBy=gobinary`, a
nonempty non-self dependency inventory, an unversioned matching Go PURL, exact
identifier/layer, and no extra field. The path check also enforces Go's
canonical `/vN` and `gopkg.in` major-version suffix rules. Official and custom
Trivy produced the same 253-package Cosign inventory; any other missing-version
package remains BLOCKED.
The custom scanner archive itself must pass both ownership/mode validation
stages before the scanner may execute even its first networked DB download.
Each archive config's ordered rootfs diff IDs are also checked against streaming
SHA-256 hashes of the corresponding uncompressed layer tar streams; an extra,
missing, reordered, oversized, or modified layer is BLOCKED.
Outer tar members are validated and capped while iterating, before an
attacker-controlled inventory can be retained in memory. DB directory entries
are likewise rejected on the first path outside the three-entry allowlist.
Classic Docker archives may omit `LayerSources`. Docker 26 archives may include
it only when every canonical blob path, diff ID, digest, OCI media type, and
positive bounded tar-member size forms one exact layer-by-layer binding; any
missing, extra, mismatched, or unknown source field is BLOCKED.
Manifest, image-config, and Trivy evidence JSON are parsed without last-key-wins
semantics: a duplicate object key at any nesting depth is BLOCKED.
Release manifest/state files use an `O_NOFOLLOW`/`fstat` identity-bound reader
and a 16 MiB ceiling. Trivy JSON evidence is rejected before parsing if it is a
symlink, non-regular file, or exceeds 256 MiB; the database itself is hashed as
a stream rather than read into host memory. The current fresh Trivy 0.72 DB was
measured at exactly 1,188,278,272 bytes; the validator permits at most 1.25 GiB
(1,342,177,280 bytes), leaving about 13% reviewed growth headroom while still
rejecting an unexpectedly inflated cache before hashing it.

The exact image PUT route keeps Nginx request buffering and its 8 MiB body
ceiling. Dedicated Edge connection limits permit one concurrent upload per
source and two globally, so at most about 16 MiB of upload bodies can occupy the
fixed 32 MiB `/tmp` tmpfs. The build gate verifies the limits with overlapping,
rate-controlled requests and requires HTTP 429 for excess concurrency.
Production Compose explicitly enables the Gateway's local-image capability,
but startup still advertises it only when the Codex version matches and the
runtime-owned `/asset-staging` directory is an exact private `0700` directory.
Normalized images are stored as `0600` files and passed to Codex by internal
path only. The public API has one authenticated PUT route and no asset download
or static-file route; neither staging paths nor asset references enter public
conversation summaries.
This build-only probe creates no Docker network and changes no firewall rule.
It first proves its three high ports are unused, then runs all test containers
on host networking with services bound only to `127.0.0.1:28080`, `:28081`, and
`:28443`; clients bind `127.0.0.2` through `.4` to exercise source identity.
Nothing is published publicly and existing Docker networks are untouched. The
gate parses the complete active listener/upstream/include set before and after
the loopback rewrite, requires per-run upstream and Edge identity tokens, and
checks the owned container IDs before and after each evidence phase. A separate
policy validator locks every probe container to its reviewed read-only,
capability-free, resource-bounded, tmpfs, healthcheck, network, and host-mount
shape.

Production Compose has no `build:` directive or application tag. A passed scan
emits a schema-v3 release manifest binding the context hash, scanner and verifier
source/build provenance, their dynamic image IDs, binary/CA/DB hashes, signed
bootstrap and exact anti-blindness evidence, and
every immutable image ID. Activation rechecks those IDs, records current and
previous manifests atomically, and restores only the recorded previous IDs if
candidate health activation fails. A symlink-safe, root-only inter-process lock
is held until success or the entire rollback/stop path has finished, so two
operators cannot overlap release transactions.

Run static checks from the repository root:

```sh
tools/release/check_turkey_bundle.sh
tools/release/check_turkey_bundle.sh --build \
  --manifest-output deploy/turkey/generated/release-manifest.json
```

The second form requires a reachable Docker daemon and external image/package
registries. It validates only this superseded historical bundle. For current
remote actions use `docs/deployment/TURKEY_EDGE_RUNBOOK.md` and its
`deploy/relay-edge/validate_bundle.py` gate.
If host Python lacks `cryptography` or PyYAML, the gate builds the
digest-pinned, hash-locked `Dockerfile.checker` image and runs only the static
Python checks inside it with no network, a read-only root, no capabilities, and
bounded resources. The checker is never deployed.

The official Cosign 3.1.2 image's HIGH `CVE-2026-39822` in Go stdlib 1.26.4 is
not waived: the release gate replaces that executable with the reviewed hardened
build above. The r16 build/scan evidence is complete for its immutable image
IDs. Production activation, certificate publication, device-to-public-IP
acceptance, restart/renewal, and external-alert gates remain **NO-GO**.
