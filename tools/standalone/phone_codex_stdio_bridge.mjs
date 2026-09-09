#!/usr/bin/env node

import { spawn } from "node:child_process";
import crypto from "node:crypto";
import http from "node:http";
import fs from "node:fs";

const MAX_CLIENT_FRAME_BYTES = 2 * 1024 * 1024;
const MAX_CLIENT_BUFFER_BYTES = MAX_CLIENT_FRAME_BYTES + 16 * 1024;
const MAX_CODEX_LINE_BYTES = 4 * 1024 * 1024;
const HEARTBEAT_INTERVAL_MS = 20_000;
const CLIENT_STALE_MS = 50_000;
const PROVIDER_SETTINGS_PATH = "/root/.cheby/provider-settings.json";
const MAX_PROVIDER_SETTINGS_BYTES = 65_536;
let cachedProviderSettings = null;

const options = parseArgs(process.argv.slice(2));
const tokenSha256 = requireSha256(options.tokenSha256);
const listenHost = options.listenHost || "127.0.0.1";
const listenPort = parsePort(options.listenPort || "4500");
if (listenHost !== "127.0.0.1") throw new Error("Codex bridge must listen on IPv4 loopback");

let activeConnection = null;
const server = http.createServer((req, res) => {
  if (req.method === "GET" && req.url === "/readyz") {
    const body = "ready\n";
    res.writeHead(200, {
      "content-type": "text/plain; charset=utf-8",
      "content-length": Buffer.byteLength(body),
      "cache-control": "no-store"
    });
    res.end(body);
    return;
  }
  res.writeHead(404, { "content-length": "0", "cache-control": "no-store" });
  res.end();
});

server.on("upgrade", (req, socket) => {
  try {
    validateUpgrade(req, tokenSha256);
  } catch {
    rejectUpgrade(socket, 401, "Unauthorized");
    return;
  }
  try {
    // Read only after authenticating. A replacement file supersedes the memory
    // cache; malformed replacement settings must never fall back to old keys.
    const providerSettings = consumeProviderSettings();
    activeConnection?.close("replaced by authenticated client");
    const key = String(req.headers["sec-websocket-key"] || "");
    const accept = crypto
      .createHash("sha1")
      .update(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
      .digest("base64");
    socket.write([
      "HTTP/1.1 101 Switching Protocols",
      "Upgrade: websocket",
      "Connection: Upgrade",
      `Sec-WebSocket-Accept: ${accept}`,
      "",
      ""
    ].join("\r\n"));
    activeConnection = new CodexConnection(socket, providerSettings, () => {
      activeConnection = null;
    });
  } catch {
    rejectUpgrade(socket, 503, "Model settings unavailable");
  }
});

server.listen(listenPort, listenHost, () => {
  process.stdout.write("codex_stdio_bridge_ready=true\n");
});

const heartbeat = setInterval(() => {
  activeConnection?.heartbeat();
}, HEARTBEAT_INTERVAL_MS);
heartbeat.unref();

for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(signal, () => shutdown(signal));
}

function shutdown(signal) {
  clearInterval(heartbeat);
  activeConnection?.close("bridge stopping");
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(signal === "SIGTERM" ? 0 : 1), 3_000).unref();
}

class CodexConnection {
  constructor(socket, providerSettings, onClose) {
    this.socket = socket;
    this.onClose = onClose;
    this.clientBuffer = Buffer.alloc(0);
    this.codexBuffer = Buffer.alloc(0);
    this.lastSeenAt = Date.now();
    this.closed = false;
    this.child = spawn("/usr/bin/python3", ["-I", "/opt/cheby/appserver/provider-launcher.py", "app-server"], {
      stdio: ["pipe", "pipe", "ignore"],
      env: { ...process.env, CHEBY_PROVIDER_SETTINGS: providerSettings }
    });
    socket.setKeepAlive(true, 25_000);
    socket.setNoDelay(true);
    socket.on("data", (chunk) => this.receiveClient(chunk));
    socket.on("close", () => this.finish());
    socket.on("error", () => this.finish());
    this.child.stdout.on("data", (chunk) => this.receiveCodex(chunk));
    this.child.stdout.on("error", () => this.close("Codex output failed"));
    this.child.stdin.on("error", () => this.close("Codex input failed"));
    this.child.on("error", () => this.close("Codex failed to start"));
    this.child.on("exit", () => this.close("Codex stopped"));
  }

  receiveClient(chunk) {
    try {
      if (this.clientBuffer.length + chunk.length > MAX_CLIENT_BUFFER_BYTES) {
        throw new Error("client buffer limit exceeded");
      }
      this.clientBuffer = Buffer.concat([this.clientBuffer, chunk]);
      while (true) {
        const frame = decodeClientFrame(this.clientBuffer);
        if (!frame) return;
        this.clientBuffer = this.clientBuffer.subarray(frame.consumed);
        this.lastSeenAt = Date.now();
        if (frame.opcode === 0x8) return this.close("client closed");
        if (frame.opcode === 0x9) {
          this.socket.write(encodeServerFrame(frame.payload, 0xA));
          continue;
        }
        if (frame.opcode === 0xA) continue;
        if (frame.opcode !== 0x1 || !frame.fin) throw new Error("unsupported websocket frame");
        const text = new TextDecoder("utf-8", { fatal: true }).decode(frame.payload);
        const value = JSON.parse(text);
        if (value === null || typeof value !== "object" || Array.isArray(value)) {
          throw new Error("JSON-RPC message must be an object");
        }
        const line = JSON.stringify(value) + "\n";
        if (!this.child.stdin.write(line)) {
          this.socket.pause();
          this.child.stdin.once("drain", () => this.socket.resume());
        }
      }
    } catch {
      this.close("invalid client message");
    }
  }

  receiveCodex(chunk) {
    try {
      if (this.codexBuffer.length + chunk.length > MAX_CODEX_LINE_BYTES) {
        throw new Error("Codex line limit exceeded");
      }
      this.codexBuffer = Buffer.concat([this.codexBuffer, chunk]);
      while (true) {
        const newline = this.codexBuffer.indexOf(0x0a);
        if (newline < 0) return;
        const raw = this.codexBuffer.subarray(0, newline);
        this.codexBuffer = this.codexBuffer.subarray(newline + 1);
        if (raw.length === 0) continue;
        JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
        if (!this.socket.write(encodeServerFrame(raw, 0x1))) {
          this.child.stdout.pause();
          this.socket.once("drain", () => this.child.stdout.resume());
        }
      }
    } catch {
      this.close("invalid Codex message");
    }
  }

  heartbeat() {
    if (Date.now() - this.lastSeenAt > CLIENT_STALE_MS) {
      this.close("client stale");
      return;
    }
    if (!this.socket.destroyed) this.socket.write(encodeServerFrame(Buffer.alloc(0), 0x9));
  }

  close(reason) {
    if (this.closed) return;
    this.closed = true;
    try {
      this.socket.write(encodeServerFrame(Buffer.from(reason), 0x8));
    } catch {
      // The socket may already be gone.
    }
    this.socket.destroy();
    if (this.child.exitCode === null && this.child.signalCode === null) {
      this.child.kill("SIGTERM");
      setTimeout(() => {
        if (this.child.exitCode === null && this.child.signalCode === null) {
          this.child.kill("SIGKILL");
        }
      }, 3_000).unref();
    }
    this.finish();
  }

  finish() {
    if (!this.closed) {
      this.closed = true;
      if (this.child.exitCode === null && this.child.signalCode === null) this.child.kill("SIGTERM");
    }
    if (this.onClose) {
      const callback = this.onClose;
      this.onClose = null;
      callback();
    }
  }
}

function consumeProviderSettings() {
  let fd;
  try {
    fd = fs.openSync(PROVIDER_SETTINGS_PATH, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  } catch (error) {
    if (error.code === "ENOENT" && cachedProviderSettings !== null) return cachedProviderSettings;
    cachedProviderSettings = null;
    throw new Error("Model settings unavailable");
  }
  cachedProviderSettings = null;
  let metadata;
  try {
    metadata = fs.fstatSync(fd);
    if (!metadata.isFile() || (metadata.mode & 0o077) !== 0 || metadata.size > MAX_PROVIDER_SETTINGS_BYTES) {
      throw new Error("Invalid model settings file");
    }
    const bytes = Buffer.alloc(MAX_PROVIDER_SETTINGS_BYTES + 1);
    let count = 0;
    while (count < bytes.length) {
      const read = fs.readSync(fd, bytes, count, bytes.length - count, count);
      if (read === 0) break;
      count += read;
    }
    if (count > MAX_PROVIDER_SETTINGS_BYTES) throw new Error("Model settings too large");
    const raw = new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, count));
    const value = JSON.parse(raw);
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("Invalid model settings");
    }
    cachedProviderSettings = raw;
    return raw;
  } finally {
    fs.closeSync(fd);
    try {
      // Android may atomically stage a newer profile while this fd is open.
      // Leave that replacement for the next reconnect instead of deleting it.
      const current = fs.lstatSync(PROVIDER_SETTINGS_PATH);
      if (metadata && current.ino === metadata.ino && current.dev === metadata.dev) fs.unlinkSync(PROVIDER_SETTINGS_PATH);
    } catch (error) {
      if (error.code !== "ENOENT") throw new Error("Model settings could not be consumed");
    }
  }
}

function validateUpgrade(req, expectedSha256) {
  if (req.method !== "GET" || req.url !== "/") throw new Error("invalid path");
  if (String(req.headers.upgrade || "").toLowerCase() !== "websocket") throw new Error("missing upgrade");
  if (!String(req.headers.connection || "").toLowerCase().split(/\s*,\s*/).includes("upgrade")) {
    throw new Error("missing connection upgrade");
  }
  if (String(req.headers["sec-websocket-version"] || "") !== "13") throw new Error("invalid version");
  const key = String(req.headers["sec-websocket-key"] || "");
  if (!/^[A-Za-z0-9+/]{22}==$/.test(key)) throw new Error("invalid websocket key");
  const authorization = String(req.headers.authorization || "");
  const match = /^Bearer ([\x21-\x7e]{32,256})$/.exec(authorization);
  if (!match) throw new Error("missing bearer");
  const actual = crypto.createHash("sha256").update(match[1], "ascii").digest("hex");
  if (!timingSafeEqual(actual, expectedSha256)) throw new Error("invalid bearer");
}

function rejectUpgrade(socket, status, reason) {
  if (socket.destroyed) return;
  socket.end([
    `HTTP/1.1 ${status} ${reason}`,
    "Connection: close",
    "Content-Length: 0",
    "Cache-Control: no-store",
    "",
    ""
  ].join("\r\n"));
}

function decodeClientFrame(buffer) {
  if (buffer.length < 2) return null;
  const first = buffer[0];
  const second = buffer[1];
  const fin = (first & 0x80) !== 0;
  if ((first & 0x70) !== 0) throw new Error("websocket extensions are unsupported");
  if ((second & 0x80) === 0) throw new Error("client frames must be masked");
  let offset = 2;
  let length = second & 0x7f;
  if (length === 126) {
    if (buffer.length < offset + 2) return null;
    length = buffer.readUInt16BE(offset);
    offset += 2;
  } else if (length === 127) {
    if (buffer.length < offset + 8) return null;
    const bigLength = buffer.readBigUInt64BE(offset);
    if (bigLength > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("frame too large");
    length = Number(bigLength);
    offset += 8;
  }
  const opcode = first & 0x0f;
  if (opcode >= 0x8 && (!fin || length > 125)) throw new Error("invalid control frame");
  if (length > MAX_CLIENT_FRAME_BYTES) throw new Error("frame limit exceeded");
  if (buffer.length < offset + 4 + length) return null;
  const mask = buffer.subarray(offset, offset + 4);
  offset += 4;
  const payload = Buffer.from(buffer.subarray(offset, offset + length));
  for (let index = 0; index < payload.length; index += 1) payload[index] ^= mask[index % 4];
  return { consumed: offset + length, fin, opcode, payload };
}

function encodeServerFrame(payload, opcode) {
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

function parseArgs(args) {
  const result = {};
  for (let index = 0; index < args.length; index += 1) {
    const name = args[index];
    const value = args[index + 1];
    if (name === "--token-sha256") result.tokenSha256 = value;
    else if (name === "--listen-host") result.listenHost = value;
    else if (name === "--listen-port") result.listenPort = value;
    else throw new Error("unsupported argument");
    index += 1;
  }
  return result;
}

function requireSha256(value) {
  if (!/^[0-9a-f]{64}$/.test(String(value || ""))) throw new Error("invalid token verifier");
  return String(value);
}

function parsePort(value) {
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error("invalid listen port");
  return port;
}

function timingSafeEqual(left, right) {
  const leftBuffer = Buffer.from(String(left), "ascii");
  const rightBuffer = Buffer.from(String(right), "ascii");
  return leftBuffer.length === rightBuffer.length && crypto.timingSafeEqual(leftBuffer, rightBuffer);
}
