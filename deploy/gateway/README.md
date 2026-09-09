# Gateway deployment status

This directory is not a Turkey production deployment package yet.

- `docker-compose.dev.yml` is the explicitly named Fake Bridge development
  runtime. It never connects to Codex.
- `docker-compose.yml` has no Bridge default and fails closed unless
  `CHEBY_BRIDGE_MODE` is explicitly set.
- The current image contains the Edge/API process only. It does not contain a
  Codex executable, authenticated Codex home, or an Edge-to-Bridge transport.

The Turkey Edge-to-Bridge boundary, transport authentication, Codex runtime
placement, and live WSS/TLS ingress must be designed and gated before this can
be called deployable. Setting `CHEBY_BRIDGE_MODE=stdio` in the current image is
expected to fail because Codex is intentionally not bundled yet.

Gateway now has a durable single-SQLite rate-limit gate, but that does not
replace Edge enforcement for a multi-replica service. The future Turkey Edge
must sanitize forwarded headers and apply independent global pairing, request,
and WSS connection limits. Configure `CHEBY_TRUSTED_PROXY_CIDRS` only after the
Edge socket CIDR is known; it is intentionally empty here.
