#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
loadOptionalEnvFiles();

const DEFAULT_PUBLIC_PORT = 3438;
const DEFAULT_LOCAL_PORT = 3437;
const COMMAND_TIMEOUT_MS = Number(process.env.PHONEBRIDGE_COMMAND_TIMEOUT_MS || 45000);
const NONCE_WINDOW_MS = Number(process.env.PHONEBRIDGE_NONCE_WINDOW_MS || 300000);
const TLS_HANDSHAKE_TIMEOUT_MS = Number(process.env.PHONEBRIDGE_TLS_HANDSHAKE_TIMEOUT_MS || 20000);
const MAX_WS_FRAME_BYTES = Number(process.env.PHONEBRIDGE_MAX_WS_FRAME_BYTES || 16 * 1024 * 1024);
const MAX_WS_BUFFER_BYTES = Number(process.env.PHONEBRIDGE_MAX_WS_BUFFER_BYTES || 17 * 1024 * 1024);
const MAX_LOCAL_BODY_BYTES = Number(process.env.PHONEBRIDGE_MAX_LOCAL_BODY_BYTES || 64 * 1024);
const MAX_PENDING_COMMANDS = Number(process.env.PHONEBRIDGE_MAX_PENDING_COMMANDS || 32);
const MAX_QUEUED_COMMANDS_PER_DEVICE = Number(process.env.PHONEBRIDGE_MAX_QUEUED_COMMANDS_PER_DEVICE || 8);
const MAX_QUEUED_COMMANDS_GLOBAL = Number(process.env.PHONEBRIDGE_MAX_QUEUED_COMMANDS_GLOBAL || 32);
const LOCAL_BODY_TIMEOUT_MS = Number(process.env.PHONEBRIDGE_LOCAL_BODY_TIMEOUT_MS || 10000);

const state = {
  startedAt: Date.now(),
  devices: new Map(),
  pending: new Map(),
  nonces: new Map(),
  queuedCommands: 0
};

function loadOptionalEnvFiles() {
  const candidates = [
    process.env.PHONEBRIDGE_ENV_FILE || "",
    path.join(__dirname, "phonebridge.env"),
    path.join(__dirname, "certs", "phonebridge-server.env")
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (!fs.existsSync(candidate)) continue;
    const lines = fs.readFileSync(candidate, "utf8").split(/\r?\n/);
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const match = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$/.exec(trimmed);
      if (!match) continue;
      const key = match[1];
      if (process.env[key] !== undefined) continue;
      process.env[key] = unquoteEnvValue(match[2].trim());
    }
  }
}

function unquoteEnvValue(value) {
  if ((value.startsWith("'") && value.endsWith("'")) || (value.startsWith('"') && value.endsWith('"'))) {
    return value.slice(1, -1);
  }
  return value;
}

function usage() {
  return `Usage:
  phonebridge serve
  phonebridge status [--device <device_id>]
  phonebridge observe [--device <device_id>]
  phonebridge screenshot [--device <device_id>]
  phonebridge ui-tree [--device <device_id>]
  phonebridge tap --x <x> --y <y> [--device <device_id>]
  phonebridge swipe --start-x <x> --start-y <y> --end-x <x> --end-y <y> [--duration-ms <ms>]
  phonebridge input-text <text>
  phonebridge back|home|recents
  phonebridge open-app <package_name>
  phonebridge open-url <url>`;
}

async function main() {
  const [command, ...args] = process.argv.slice(2);
  if (!command || command === "--help" || command === "-h") {
    console.log(usage());
    return;
  }
  if (command === "serve") {
    await serve();
    return;
  }
  const result = await callLocal(command, parseArgs(args));
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
}

async function serve() {
  const deviceSecrets = loadDeviceSecrets();
  const insecure = process.env.PHONEBRIDGE_ALLOW_INSECURE === "1";
  if (deviceSecrets.size === 0 && !insecure) {
    throw new Error("PHONEBRIDGE_DEVICE_TOKEN or PHONEBRIDGE_DEVICE_SECRETS_JSON is required.");
  }

  const publicHost = process.env.PHONEBRIDGE_PUBLIC_HOST || "0.0.0.0";
  const publicPort = Number(process.env.PHONEBRIDGE_PUBLIC_PORT || DEFAULT_PUBLIC_PORT);
  const localHost = process.env.PHONEBRIDGE_LOCAL_HOST || "127.0.0.1";
  const localPort = Number(process.env.PHONEBRIDGE_LOCAL_PORT || DEFAULT_LOCAL_PORT);
  if (insecure && !isLoopbackHost(publicHost)) {
    throw new Error("PHONEBRIDGE_ALLOW_INSECURE=1 is permitted only on a loopback public listener.");
  }
  if (!insecure) validateProductionPhoneIdentity(publicHost);
  const localCredential = loadLocalCredential();
  if (!localCredential.token && !(insecure && isLoopbackHost(localHost))) {
    throw new Error("A private local controller credential is required.");
  }
  const mutationController = String(process.env.PHONEBRIDGE_MUTATION_CONTROLLER || "").trim();
  if (!mutationController) {
    throw new Error("PHONEBRIDGE_MUTATION_CONTROLLER must be set before accepting commands.");
  }
  if (localCredential.token && localCredential.controller !== mutationController) {
    throw new Error("The authenticated local credential is not bound to the mutation controller.");
  }

  const publicServer = createPublicServer(deviceSecrets);
  const localServer = http.createServer((req, res) => handleLocalRequest(req, res));
  const publicProtocol = publicServer instanceof https.Server ? "WSS" : "WS";

  await listen(publicServer, publicPort, publicHost);
  await listen(localServer, localPort, localHost);

  console.error(`PhoneBridge public ${publicProtocol} endpoint ready on ${publicHost}:${publicPort}/phone/connect`);
  console.error(`PhoneBridge local command endpoint ready on ${localHost}:${localPort}`);
  if (process.env.PHONEBRIDGE_ALLOW_INSECURE === "1") {
    console.error("WARNING: PHONEBRIDGE_ALLOW_INSECURE=1 accepts unauthenticated phones.");
  }

  setInterval(() => {
    pruneNonces();
    for (const connection of state.devices.values()) {
      connection.ping();
      if (Date.now() - connection.lastSeenAt > 90000) {
        connection.close("stale phone connection");
      }
    }
  }, 25000).unref();
}

function isLoopbackHost(host) {
  return ["127.0.0.1", "::1", "localhost"].includes(String(host || "").toLowerCase());
}

function validateProductionPhoneIdentity(publicHost) {
  const deviceId = String(process.env.PHONEBRIDGE_DEVICE_ID || "");
  if (!deviceId) throw new Error("PHONEBRIDGE_DEVICE_ID is required for one-to-one phone pairing.");
  if (!isLoopbackHost(publicHost)) {
    if (!shouldRequireMtls() || !process.env.PHONEBRIDGE_MTLS_CA_FILE) {
      throw new Error("A non-loopback PhoneBridge public listener requires mTLS.");
    }
    if (loadAllowedClientCertFingerprints().size === 0) {
      throw new Error("A non-loopback PhoneBridge public listener requires an allowed client certificate fingerprint.");
    }
    const binding = loadDeviceCertFingerprints()[deviceId];
    if (!binding) throw new Error("The paired device id must be bound to its mTLS client certificate fingerprint.");
  }
}

function createPublicServer(deviceSecrets) {
  const tlsOptions = loadTlsOptions();
  const server = tlsOptions
    ? https.createServer(tlsOptions)
    : http.createServer();

  server.on("request", (req, res) => {
    if (req.url === "/health") return sendJson(res, 200, publicStatus());
    sendJson(res, 404, { ok: false, error: "not_found" });
  });

  server.on("upgrade", (req, socket) => {
    try {
      const url = new URL(req.url || "/", "http://phonebridge.local");
      if (!url.pathname.endsWith("/phone/connect")) {
        socket.destroy();
        return;
      }
      const tlsAuth = verifyTlsPeer(req);
      const auth = verifyPhoneHandshake(req, url, deviceSecrets, tlsAuth);
      const key = req.headers["sec-websocket-key"];
      if (!key) throw new Error("missing Sec-WebSocket-Key");
      const accept = crypto
        .createHash("sha1")
        .update(String(key) + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
        .digest("base64");
      socket.write([
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        `Sec-WebSocket-Accept: ${accept}`,
        "",
        ""
      ].join("\r\n"));
      const connection = new PhoneConnection(socket, auth.deviceId);
      const existing = state.devices.get(auth.deviceId);
      if (existing) existing.close("replaced by new phone connection");
      state.devices.set(auth.deviceId, connection);
      connection.onClose = () => {
        if (state.devices.get(auth.deviceId) === connection) state.devices.delete(auth.deviceId);
      };
    } catch (error) {
      socket.write("HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n");
      socket.destroy();
      console.error(`PhoneBridge phone connection rejected: ${error.message}`);
    }
  });

  return server;
}

function loadTlsOptions() {
  const tlsKey = process.env.PHONEBRIDGE_TLS_KEY_FILE;
  const tlsCert = process.env.PHONEBRIDGE_TLS_CERT_FILE;
  if (!tlsKey && !tlsCert) {
    if (process.env.PHONEBRIDGE_ALLOW_INSECURE === "1") return null;
    throw new Error("PhoneBridge public TLS is required. Set PHONEBRIDGE_TLS_KEY_FILE and PHONEBRIDGE_TLS_CERT_FILE, or explicitly opt in to insecure local development with PHONEBRIDGE_ALLOW_INSECURE=1.");
  }
  if (!tlsKey || !tlsCert) throw new Error("Both PHONEBRIDGE_TLS_KEY_FILE and PHONEBRIDGE_TLS_CERT_FILE are required for TLS.");
  const caFile = process.env.PHONEBRIDGE_MTLS_CA_FILE || "";
  if (shouldRequireMtls() && !caFile) throw new Error("PHONEBRIDGE_MTLS_CA_FILE is required when PHONEBRIDGE_MTLS_REQUIRED is enabled.");
  const options = {
    key: fs.readFileSync(tlsKey),
    cert: fs.readFileSync(tlsCert),
    handshakeTimeout: TLS_HANDSHAKE_TIMEOUT_MS
  };
  if (caFile) {
    options.ca = fs.readFileSync(caFile);
    options.requestCert = true;
    options.rejectUnauthorized = shouldRequireMtls();
  }
  return options;
}

function shouldRequireMtls() {
  const value = String(process.env.PHONEBRIDGE_MTLS_REQUIRED || "").trim().toLowerCase();
  if (!value) return Boolean(process.env.PHONEBRIDGE_MTLS_CA_FILE);
  return ["1", "true", "yes", "on", "required"].includes(value);
}

function verifyTlsPeer(req) {
  const mtlsCa = process.env.PHONEBRIDGE_MTLS_CA_FILE || "";
  const mtlsRequired = shouldRequireMtls();
  if (!mtlsCa && !mtlsRequired) return null;
  if (!req.client || typeof req.client.getPeerCertificate !== "function") {
    if (mtlsRequired) throw new Error("mTLS is required, but the public endpoint is not TLS.");
    return null;
  }
  if (mtlsRequired && !req.client.authorized) {
    throw new Error(`mTLS client certificate rejected: ${req.client.authorizationError || "unauthorized"}`);
  }
  const cert = req.client.getPeerCertificate();
  if (!cert || !cert.fingerprint256) {
    if (mtlsRequired) throw new Error("mTLS client certificate is required.");
    return null;
  }
  const fingerprint = normalizeFingerprint(cert.fingerprint256);
  const allowed = loadAllowedClientCertFingerprints();
  if (allowed.size > 0 && !allowed.has(fingerprint)) throw new Error("unexpected mTLS client certificate fingerprint");
  return {
    fingerprint,
    subject: cert.subject || {},
    issuer: cert.issuer || {}
  };
}

function loadAllowedClientCertFingerprints() {
  const values = [
    process.env.PHONEBRIDGE_CLIENT_CERT_FINGERPRINT_SHA256 || "",
    process.env.PHONEBRIDGE_CLIENT_CERT_FINGERPRINTS_SHA256 || ""
  ].join(",");
  return new Set(values.split(",").map(normalizeFingerprint).filter(Boolean));
}

function loadDeviceCertFingerprints() {
  const raw = process.env.PHONEBRIDGE_DEVICE_CERT_FINGERPRINTS_JSON || "";
  if (!raw.trim()) return {};
  const parsed = JSON.parse(raw);
  return Object.fromEntries(Object.entries(parsed).map(([deviceId, fingerprint]) => [
    deviceId,
    normalizeFingerprint(fingerprint)
  ]));
}

function verifyDeviceCertificateBinding(deviceId, tlsAuth) {
  const bindings = loadDeviceCertFingerprints();
  const expected = bindings[deviceId];
  if (!expected) return;
  if (!tlsAuth?.fingerprint) throw new Error("device certificate binding requires mTLS client certificate");
  if (expected !== tlsAuth.fingerprint) throw new Error("mTLS client certificate does not match device id");
}

function normalizeFingerprint(value) {
  return String(value || "").trim().replace(/[^a-fA-F0-9]/g, "").toLowerCase();
}

function verifyPhoneHandshake(req, url, deviceSecrets, tlsAuth) {
  const deviceId = String(req.headers["x-chebyagent-device-id"] || "");
  const timestamp = String(req.headers["x-chebyagent-timestamp"] || "");
  const nonce = String(req.headers["x-chebyagent-nonce"] || "");
  const signature = String(req.headers["x-chebyagent-signature"] || "");
  if (process.env.PHONEBRIDGE_ALLOW_INSECURE === "1") {
    return { deviceId: deviceId || "insecure-phone" };
  }
  if (!deviceId || !timestamp || !nonce || !signature) throw new Error("missing phone auth headers");
  const allowedDeviceId = process.env.PHONEBRIDGE_DEVICE_ID || "";
  if (allowedDeviceId && allowedDeviceId !== deviceId) throw new Error("unexpected device id");
  const token = deviceSecrets.get(deviceId) || deviceSecrets.get("*");
  if (!token) throw new Error("unknown device id");
  const timestampMs = Date.parse(timestamp);
  if (!Number.isFinite(timestampMs) || Math.abs(Date.now() - timestampMs) > NONCE_WINDOW_MS) {
    throw new Error("phone auth timestamp outside allowed window");
  }
  const nonceKey = `${deviceId}:${nonce}`;
  if (state.nonces.has(nonceKey)) throw new Error("replayed phone auth nonce");
  const expected = hmacBase64Url(token, [
    "GET",
    url.pathname || "/",
    timestamp,
    nonce,
    sha256Base64Url(""),
    deviceId
  ].join("\n"));
  if (!timingSafeEqual(signature, expected)) throw new Error("invalid phone auth signature");
  verifyDeviceCertificateBinding(deviceId, tlsAuth);
  state.nonces.set(nonceKey, Date.now() + NONCE_WINDOW_MS);
  return { deviceId };
}

class PhoneConnection {
  constructor(socket, deviceId) {
    this.socket = socket;
    socket.setKeepAlive(true, 25000);
    socket.setNoDelay(true);
    this.deviceId = deviceId;
    this.buffer = Buffer.alloc(0);
    this.connectedAt = Date.now();
    this.lastSeenAt = Date.now();
    this.lastStatus = null;
    this.onClose = null;
    this.commandChain = Promise.resolve();
    this.queuedCommands = 0;
    socket.on("data", (chunk) => {
      try {
        this.receive(chunk);
      } catch (error) {
        console.error(`PhoneBridge closed an invalid phone frame from ${this.deviceId}: ${error.message}`);
        this.close("invalid or oversized phone frame");
      }
    });
    socket.on("close", () => this.closed());
    socket.on("error", () => this.closed());
    console.error(`PhoneBridge phone online: ${deviceId}`);
  }

  sendCommand(tool, args, timeoutMs = COMMAND_TIMEOUT_MS) {
    if (state.pending.size >= MAX_PENDING_COMMANDS) {
      return Promise.reject(httpError(429, "PhoneBridge pending command limit reached"));
    }
    const commandId = `cmd_${Date.now().toString(36)}_${crypto.randomBytes(5).toString("hex")}`;
    const payload = { type: "command", command_id: commandId, tool, arguments: args || {} };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        state.pending.delete(commandId);
        reject(new Error(`phone command timed out: ${tool}`));
      }, timeoutMs);
      state.pending.set(commandId, { resolve, reject, timer, deviceId: this.deviceId, tool });
      this.sendText(payload);
    });
  }

  enqueueCommand(tool, args, timeoutMs = COMMAND_TIMEOUT_MS) {
    if (this.queuedCommands >= MAX_QUEUED_COMMANDS_PER_DEVICE
        || state.queuedCommands >= MAX_QUEUED_COMMANDS_GLOBAL) {
      return Promise.reject(httpError(429, "PhoneBridge device command queue limit reached"));
    }
    this.queuedCommands += 1;
    state.queuedCommands += 1;
    const deadline = Date.now() + timeoutMs;
    const run = () => {
      const remaining = deadline - Date.now();
      if (remaining <= 0) throw new Error(`phone command timed out in queue: ${tool}`);
      return this.sendCommand(tool, args, remaining);
    };
    const queued = this.commandChain.then(run, run);
    this.commandChain = queued.catch(() => undefined);
    return queued.finally(() => {
      this.queuedCommands = Math.max(0, this.queuedCommands - 1);
      state.queuedCommands = Math.max(0, state.queuedCommands - 1);
    });
  }

  sendText(value) {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    this.socket.write(encodeFrame(Buffer.from(text, "utf8"), 0x1));
  }

  ping() {
    if (!this.socket.destroyed) this.socket.write(encodeFrame(Buffer.from("ping"), 0x9));
  }

  close(reason) {
    try {
      this.socket.write(encodeFrame(Buffer.from(reason || ""), 0x8));
    } catch {
      // Ignore close write failures.
    }
    this.socket.destroy();
  }

  closed() {
    for (const [commandId, pending] of state.pending.entries()) {
      if (pending.deviceId === this.deviceId) {
        clearTimeout(pending.timer);
        pending.reject(new Error("phone disconnected"));
        state.pending.delete(commandId);
      }
    }
    if (this.onClose) this.onClose();
    console.error(`PhoneBridge phone offline: ${this.deviceId}`);
  }

  receive(chunk) {
    if (this.buffer.length + chunk.length > MAX_WS_BUFFER_BYTES) {
      throw new Error("websocket receive buffer limit exceeded");
    }
    this.buffer = Buffer.concat([this.buffer, chunk]);
    while (true) {
      const frame = decodeFrame(this.buffer, MAX_WS_FRAME_BYTES);
      if (!frame) return;
      this.buffer = this.buffer.subarray(frame.consumed);
      this.lastSeenAt = Date.now();
      if (frame.opcode === 0x8) {
        this.close("phone closed");
        return;
      }
      if (frame.opcode === 0x9) {
        this.socket.write(encodeFrame(frame.payload, 0xA));
        continue;
      }
      if (frame.opcode === 0xA) continue;
      if (frame.opcode !== 0x1) continue;
      this.handleText(frame.payload.toString("utf8"));
    }
  }

  handleText(text) {
    let message;
    try {
      message = JSON.parse(text);
    } catch (error) {
      console.error(`PhoneBridge ignored invalid phone JSON from ${this.deviceId}: ${error.message}`);
      return;
    }
    if (message.type === "hello" || message.type === "status") {
      this.lastStatus = message.status || message;
      return;
    }
    if (message.type === "result") {
      const commandId = String(message.command_id || "");
      const pending = state.pending.get(commandId);
      if (!pending) return;
      clearTimeout(pending.timer);
      state.pending.delete(commandId);
      if (message.ok === false) {
        pending.reject(new Error(message.error || "phone command failed"));
      } else {
        pending.resolve({
          ok: true,
          device_id: this.deviceId,
          command_id: commandId,
          tool: pending.tool,
          result: message.result || {}
        });
      }
    }
  }
}

async function handleLocalRequest(req, res) {
  try {
    if (req.method === "GET" && req.url === "/health") return sendJson(res, 200, localStatus());
    if (req.method !== "POST" || req.url !== "/command") return sendJson(res, 404, { ok: false, error: "not_found" });
    let controller;
    try {
      controller = authenticateLocalController(req.headers.authorization || "");
    } catch {
      return sendJson(res, 401, { ok: false, error: "unauthorized" });
    }
    const body = await readBody(req);
    const request = body.trim() ? JSON.parse(body) : {};
    const result = await runCommand(request.tool || "status", request.arguments || {}, request.device_id || "", controller);
    sendJson(res, 200, result);
  } catch (error) {
    const status = Number.isInteger(error.httpStatus) ? error.httpStatus : 500;
    sendJson(res, status, { ok: false, error: error.message || String(error) });
  }
}

function httpError(status, message) {
  const error = new Error(message);
  error.httpStatus = status;
  return error;
}

async function runCommand(tool, args, requestedDeviceId, controller = "cli") {
  if (tool === "status") {
    const connection = resolveStatusDevice(requestedDeviceId);
    if (!connection) return localStatus(requestedDeviceId);
    const response = await connection.enqueueCommand("status", args || {});
    if (response?.result && typeof response.result === "object") {
      connection.lastStatus = response.result;
    }
    return localStatus(requestedDeviceId);
  }
  const connection = resolveDevice(requestedDeviceId);
  const phoneTool = normalizeTool(tool);
  enforceMutationController(phoneTool, controller);
  return await connection.enqueueCommand(phoneTool, args || {});
}

function resolveStatusDevice(requestedDeviceId) {
  if (requestedDeviceId) return state.devices.get(requestedDeviceId) || null;
  const preferred = process.env.PHONEBRIDGE_DEVICE_ID || "";
  if (preferred && state.devices.has(preferred)) return state.devices.get(preferred);
  if (state.devices.size === 1) return [...state.devices.values()][0];
  return null;
}

function enforceMutationController(phoneTool, controller) {
  const readOnly = new Set(["status", "capabilities", "observe", "ui_tree", "screenshot"]);
  if (readOnly.has(phoneTool)) return;
  const configured = String(process.env.PHONEBRIDGE_MUTATION_CONTROLLER || "").trim();
  if (!configured) throw new Error("PHONEBRIDGE_MUTATION_CONTROLLER must select the one authorized phone controller.");
  if (configured !== controller) throw new Error("phone mutation is reserved for the configured controller");
}

function localStatus(requestedDeviceId = "") {
  const devices = [...state.devices.values()].map((connection) => ({
    device_id: connection.deviceId,
    connected_at: new Date(connection.connectedAt).toISOString(),
    last_seen_at: new Date(connection.lastSeenAt).toISOString(),
    status: connection.lastStatus
  }));
  return {
    ok: true,
    name: "phonebridge",
    uptime_ms: Date.now() - state.startedAt,
    requested_device_id: requestedDeviceId || "",
    phone_online: requestedDeviceId
      ? state.devices.has(requestedDeviceId)
      : devices.length > 0,
    devices
  };
}

function publicStatus() {
  const output = {
    ok: true,
    name: "phonebridge",
    public_endpoint: "/phone/connect",
    phone_online: state.devices.size > 0
  };
  if (process.env.PHONEBRIDGE_PUBLIC_HEALTH_DETAIL === "1") output.devices = [...state.devices.keys()];
  return output;
}

function resolveDevice(requestedDeviceId) {
  if (requestedDeviceId) {
    const connection = state.devices.get(requestedDeviceId);
    if (!connection) throw new Error(`phone offline: ${requestedDeviceId}`);
    return connection;
  }
  const preferred = process.env.PHONEBRIDGE_DEVICE_ID || "";
  if (preferred && state.devices.has(preferred)) return state.devices.get(preferred);
  if (state.devices.size === 1) return [...state.devices.values()][0];
  if (state.devices.size === 0) throw new Error("phone offline");
  throw new Error("multiple phones online; pass --device <device_id>");
}

function normalizeTool(tool) {
  const value = String(tool || "").replace(/-/g, "_");
  const map = {
    "ui_tree": "ui_tree",
    "ui-tree": "ui_tree",
    "screenshot": "screenshot",
    "capture_screenshot": "screenshot",
    "tap": "tap_screen",
    "swipe": "swipe_screen",
    "input_text": "input_text",
    "input-text": "input_text",
    "back": "press_back",
    "home": "press_home",
    "recents": "press_recents",
    "open_app": "open_app",
    "open-app": "open_app",
    "open_url": "open_url",
    "open-url": "open_url"
  };
  return map[value] || value;
}

async function callLocal(command, parsed) {
  const tool = normalizeCliCommand(command);
  const body = JSON.stringify({
    tool,
    device_id: parsed.device || process.env.PHONEBRIDGE_DEVICE_ID || "",
    arguments: commandArguments(tool, parsed)
  });
  const port = Number(process.env.PHONEBRIDGE_LOCAL_PORT || DEFAULT_LOCAL_PORT);
  const host = process.env.PHONEBRIDGE_LOCAL_HOST || "127.0.0.1";
  const headers = {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(body)
  };
  const credential = loadLocalCredential();
  if (credential.token) headers.authorization = `Bearer ${credential.token}`;
  return await httpRequest({ host, port, path: "/command", method: "POST", headers }, body);
}

function loadLocalCredential() {
  const tokenFile = String(process.env.PHONEBRIDGE_LOCAL_TOKEN_FILE || "").trim();
  const token = tokenFile ? readPrivateTokenFile(tokenFile) : String(process.env.PHONEBRIDGE_LOCAL_TOKEN || "").trim();
  const controller = String(process.env.PHONEBRIDGE_LOCAL_TOKEN_CONTROLLER || "").trim();
  if (token && token.length < 32) throw new Error("PhoneBridge local controller token is too short");
  if (token && !controller) throw new Error("PHONEBRIDGE_LOCAL_TOKEN_CONTROLLER must bind the credential to a controller");
  return { token, controller };
}

function readPrivateTokenFile(fileName) {
  const metadata = fs.lstatSync(fileName);
  if (!metadata.isFile() || metadata.isSymbolicLink() || (metadata.mode & 0o077) !== 0 || metadata.size > 4096) {
    throw new Error("PhoneBridge local controller token file is unsafe");
  }
  return fs.readFileSync(fileName, "utf8").trim();
}

function authenticateLocalController(authorization) {
  const credential = loadLocalCredential();
  if (!credential.token) {
    if (process.env.PHONEBRIDGE_ALLOW_INSECURE === "1") return "insecure-development";
    throw new Error("local controller credential is missing");
  }
  const supplied = String(authorization || "").replace(/^Bearer\s+/i, "");
  if (!timingSafeEqual(supplied, credential.token)) throw new Error("invalid local controller credential");
  return credential.controller;
}

function normalizeCliCommand(command) {
  if (command === "ui-tree") return "ui_tree";
  if (command === "input-text") return "input_text";
  if (command === "open-app") return "open_app";
  if (command === "open-url") return "open_url";
  return command;
}

function commandArguments(tool, parsed) {
  if (tool === "tap") return { x: numberArg(parsed.x, "x"), y: numberArg(parsed.y, "y") };
  if (tool === "swipe") {
    return {
      start_x: numberArg(parsed.startX, "start-x"),
      start_y: numberArg(parsed.startY, "start-y"),
      end_x: numberArg(parsed.endX, "end-x"),
      end_y: numberArg(parsed.endY, "end-y"),
      duration_ms: parsed.durationMs ? Number(parsed.durationMs) : 350
    };
  }
  if (tool === "input_text") return { text: parsed._.join(" ") };
  if (tool === "open_app") return { package_name: parsed._[0] || parsed.packageName || "" };
  if (tool === "open_url") return { url: parsed._[0] || parsed.url || "" };
  return parsed.json ? JSON.parse(parsed.json) : {};
}

function parseArgs(args) {
  const parsed = { _: [] };
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === "--device") parsed.device = args[++i];
    else if (arg === "--x") parsed.x = args[++i];
    else if (arg === "--y") parsed.y = args[++i];
    else if (arg === "--start-x") parsed.startX = args[++i];
    else if (arg === "--start-y") parsed.startY = args[++i];
    else if (arg === "--end-x") parsed.endX = args[++i];
    else if (arg === "--end-y") parsed.endY = args[++i];
    else if (arg === "--duration-ms") parsed.durationMs = args[++i];
    else if (arg === "--package-name") parsed.packageName = args[++i];
    else if (arg === "--url") parsed.url = args[++i];
    else if (arg === "--json") parsed.json = args[++i];
    else parsed._.push(arg);
  }
  return parsed;
}

function numberArg(value, name) {
  const number = Number(value);
  if (!Number.isFinite(number)) throw new Error(`--${name} is required`);
  return number;
}

function loadDeviceSecrets() {
  const secrets = new Map();
  const json = process.env.PHONEBRIDGE_DEVICE_SECRETS_JSON || "";
  if (json.trim()) {
    const parsed = JSON.parse(json);
    for (const [deviceId, token] of Object.entries(parsed)) secrets.set(deviceId, String(token));
  }
  const token = process.env.PHONEBRIDGE_DEVICE_TOKEN || "";
  const deviceId = process.env.PHONEBRIDGE_DEVICE_ID || "";
  if (token && deviceId) secrets.set(deviceId, token);
  return secrets;
}

function encodeFrame(payload, opcode) {
  const length = payload.length;
  let header;
  if (length < 126) {
    header = Buffer.from([0x80 | opcode, length]);
  } else if (length <= 0xffff) {
    header = Buffer.alloc(4);
    header[0] = 0x80 | opcode;
    header[1] = 126;
    header.writeUInt16BE(length, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x80 | opcode;
    header[1] = 127;
    header.writeBigUInt64BE(BigInt(length), 2);
  }
  return Buffer.concat([header, payload]);
}

function decodeFrame(buffer, maxFrameBytes = MAX_WS_FRAME_BYTES) {
  if (buffer.length < 2) return null;
  const first = buffer[0];
  const second = buffer[1];
  let offset = 2;
  let length = second & 0x7f;
  if (length === 126) {
    if (buffer.length < offset + 2) return null;
    length = buffer.readUInt16BE(offset);
    offset += 2;
  } else if (length === 127) {
    if (buffer.length < offset + 8) return null;
    const bigLength = buffer.readBigUInt64BE(offset);
    if (bigLength > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("websocket frame too large");
    length = Number(bigLength);
    offset += 8;
  }
  if (length > maxFrameBytes) throw new Error("websocket frame exceeds the size limit");
  const masked = (second & 0x80) !== 0;
  let mask;
  if (masked) {
    if (buffer.length < offset + 4) return null;
    mask = buffer.subarray(offset, offset + 4);
    offset += 4;
  }
  if (buffer.length < offset + length) return null;
  const payload = Buffer.from(buffer.subarray(offset, offset + length));
  if (masked) {
    for (let i = 0; i < payload.length; i += 1) payload[i] ^= mask[i % 4];
  }
  return { opcode: first & 0x0f, payload, consumed: offset + length };
}

function sha256Base64Url(value) {
  return crypto.createHash("sha256").update(value).digest("base64url");
}

function hmacBase64Url(secret, payload) {
  return crypto.createHmac("sha256", secret).update(payload).digest("base64url");
}

function timingSafeEqual(left, right) {
  const leftBuffer = Buffer.from(String(left));
  const rightBuffer = Buffer.from(String(right));
  return leftBuffer.length === rightBuffer.length && crypto.timingSafeEqual(leftBuffer, rightBuffer);
}

function pruneNonces() {
  const now = Date.now();
  for (const [key, expiresAt] of state.nonces.entries()) {
    if (expiresAt <= now) state.nonces.delete(key);
  }
}

function sendJson(res, statusCode, value) {
  const body = JSON.stringify(value);
  res.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body)
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    let settled = false;
    const declared = Number(req.headers["content-length"] || 0);
    if (declared > MAX_LOCAL_BODY_BYTES) return reject(new Error("local request body exceeds the size limit"));
    const timer = setTimeout(() => fail(new Error("local request body timed out")), LOCAL_BODY_TIMEOUT_MS);
    const finish = (callback) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      callback();
    };
    const fail = (error) => finish(() => reject(error));
    req.on("data", (chunk) => {
      total += chunk.length;
      if (total > MAX_LOCAL_BODY_BYTES) {
        fail(new Error("local request body exceeds the size limit"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => finish(() => resolve(Buffer.concat(chunks).toString("utf8"))));
    req.on("error", fail);
  });
}

function httpRequest(options, body) {
  return new Promise((resolve, reject) => {
    const req = http.request(options, (res) => {
      const chunks = [];
      res.on("data", (chunk) => chunks.push(chunk));
      res.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        if (res.statusCode < 200 || res.statusCode >= 300) {
          reject(new Error(`PhoneBridge local command HTTP ${res.statusCode}: ${text}`));
          return;
        }
        resolve(text.trim() ? JSON.parse(text) : {});
      });
    });
    req.on("error", reject);
    req.end(body);
  });
}

function listen(server, port, host) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => {
      server.off("error", reject);
      resolve();
    });
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    console.error(error.stack || error.message || String(error));
    process.exit(1);
  });
}

export {
  MAX_LOCAL_BODY_BYTES,
  MAX_WS_FRAME_BYTES,
  authenticateLocalController,
  decodeFrame,
  enforceMutationController,
  isLoopbackHost,
  readBody
};
