# ChebyCodex single-user production acceptance — 2026-08-08

This is the retained short gate for the private one-phone/one-Codex product.
It contains identifiers and hashes only; no credential, pairing secret, token,
or prompt-sensitive payload is retained.

## Release identity

- Android package: `com.cheby.codex.mobile`, version `0.4.5` (`versionCode=12`).
- Signed production APK SHA-256:
  `e1a19079eaa2aa317829180a459b17807070470a9586bde0c7b595a5e986beee`.
- Production Connector image:
  `sha256:154e5278eedd0d7a2064e75acf2f55d282de83e460927d6fa0b5e43c97f7a714`.
- Production endpoint: `192.0.0.8:27461` open; TCP `27462`, `80`, and
  `443` closed after the test window.
- Cloud runtime: `gpt-5.6-sol`, reasoning `xhigh`, approval policy `never`.
- Connector runtime postflight: PASS; container healthy with restart count 0.

## Real production message and sandbox gate

The phone retained one unchanged durable binding across the cases below. A
temporary same-signature diagnostic build read only the redacted identity and
was immediately replaced by the exact signed production APK. The raw
`bindingScope` is not retained; its SHA-256 is
`57207e8e5816bf187996381074462ea431f629b5921ece611b89ef9e52df34db`, and
the observed App gateway generation was `0`.

Case `SECURITY_LIVE_20260808_H73`:

- Business key: `(bindingScopeSha256=57207e8e5816bf187996381074462ea431f629b5921ece611b89ef9e52df34db, generation=0, threadId=thr_a3c39679f2134882aaf0c036eafaf5ff, clientMessageId=bca657b2-09f0-48fc-b535-b8a33c67a5fb)`.
- Thread: `thr_a3c39679f2134882aaf0c036eafaf5ff`.
- Client message: `bca657b2-09f0-48fc-b535-b8a33c67a5fb`.
- Turn: `turn_0d5a28b9b12941cc9896a190d587ba12`.
- Result: `completed/terminal`, attempt count 1, one canonical user message and
  one canonical assistant message.
- Retained safe artifact: `/workspace/.cheby/security-live-h73.json` with
  `auth_hidden=true`, `secret_hidden=true`, `proc_hidden=true`,
  `workspace_rw=true`, and `network_ok=true`.
- Relay evidence: one device-to-node delivery for the client message; the two
  later node-to-device deliveries were the turn-submission response and a
  subsequent thread read. Connector durable outbox and request intents
  converged to empty.
- Fresh phone UI showed the unique terminal answer and `服务已连接`; no
  `执行中` label remained.

Case `MCP_ISOLATION_20260808_K91`:

- Business key: `(bindingScopeSha256=57207e8e5816bf187996381074462ea431f629b5921ece611b89ef9e52df34db, generation=0, threadId=thr_a3c39679f2134882aaf0c036eafaf5ff, clientMessageId=23803731-d266-40ad-bc3c-3d66af5fd32c)`.
- Thread: `thr_a3c39679f2134882aaf0c036eafaf5ff`.
- Client message: `23803731-d266-40ad-bc3c-3d66af5fd32c`.
- Turn: `turn_cb091a31a31c40c6966e2d8f0eaced1d`.
- Result: `completed/terminal`, attempt count 1, structured
  `android_phone_status` MCP item completed.
- Before Connector restart, the writable workspace contained deliberate
  `sitecustomize.py` and fake `cheby_connector.local_mcp` import traps. All MCP
  processes used Python isolated mode, a read-only absolute entrypoint, and a
  read-only working directory. The trap marker remained absent after direct
  MCP starts and the real Codex Turn. Test traps were then deleted.

## Real café three-part gate

Case `CAFE_E2E_20260808_C31`:

- Business key: `(bindingScopeSha256=57207e8e5816bf187996381074462ea431f629b5921ece611b89ef9e52df34db, generation=0, threadId=thr_9be7ac9b9ae54ad18c22402e8a6f1761, clientMessageId=263b7095-ef47-4ed8-ba14-aa8ef5aebbf7)`.
- Thread: `thr_9be7ac9b9ae54ad18c22402e8a6f1761`.
- Client message: `263b7095-ef47-4ed8-ba14-aa8ef5aebbf7`.
- Turn: `turn_4d67a04afef8487389f30fb08cc923ed`.
- Result: `completed/terminal`, attempt count 1.
- PhoneBridge opened Yandex Maps in UC Browser and observed the real result
  `Santarest`, Brest, `Savieckaja vulica, 30` (as rendered by the page).
- Open URL command: `cmd_msjmn718_3a1e7475b1`.
- Fresh UI tree command: `cmd_msjmopb0_5ec6618de9`.
- Fresh screenshot command: `cmd_msjmoubc_3ce68e63b8`.
- Screenshot artifact:
  `/workspace/.cheby/phone-artifacts/phone-20260808T002124Z-e2f0dcef.jpg`,
  166365 bytes, SHA-256
  `145b5d5f85035c9b040c885afee15bed2a70545f5b183f20fb20bef1304ff5b5`.

## Short deterministic test record

- Gateway: 203 passed.
- Connector: 78 passed.
- Relay: 65 passed.
- Deployment, gate tooling, and release-security suite: 261 passed.
- ChebyCodex Android unit tests: 319 passed, zero skipped.
- ChebyNode unit tests: 30 passed, zero skipped.
- Real-device Android suite: 38 discovered; 26 executed and passed, 12
  parameter-gated 27462/fault-injection cases intentionally not selected in
  this short production gate; zero failures.
- Static Connector bundle, raw Compose invariants, staged diff whitespace,
  signed APK restoration, and production runtime postflight: PASS.
- Independent final code review: P0=0, P1=0, GO.

The long soak, multi-tenant, iOS, central-control, billing, HA, app-store, and
multi-server product tracks are outside this private single-user product scope.
