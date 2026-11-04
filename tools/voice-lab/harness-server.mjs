#!/usr/bin/env node
/**
 * Static server + backend proxy + result collector for the voice-lab harness.
 *
 *   node harness-server.mjs --port 8787 --backend http://127.0.0.1:8000 \
 *        --static ../harness-dist --audio ../audio --runs-dir ../../tmp/voice-lab/runs
 *
 * Routes:
 *   GET  /                probe page
 *   GET  /audio/*.wav     synthesized speech assets
 *   GET  /health
 *   ANY  /v1/*            proxied to the backend (Authorization forwarded as-is)
 *   POST /collect         run payload -> runs-dir/<run>.json (+ <run>.wav)
 */

import { createServer } from "node:http";
import { createReadStream, existsSync, mkdirSync, statSync, writeFileSync } from "node:fs";
import { extname, join, normalize, resolve } from "node:path";

const args = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i].replace(/^--/, "");
  args.set(key, process.argv[i + 1]);
}

const PORT = Number(args.get("port") ?? 8787);
const BACKEND = args.get("backend") ?? "http://127.0.0.1:8000";
const STATIC_DIR = resolve(args.get("static") ?? join(import.meta.dirname, "harness-dist"));
const AUDIO_DIR = resolve(args.get("audio") ?? join(import.meta.dirname, "audio"));
const RUNS_DIR = resolve(args.get("runs-dir") ?? join(import.meta.dirname, "..", "..", "tmp", "voice-lab", "runs"));

mkdirSync(RUNS_DIR, { recursive: true });

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".wav": "audio/wav",
  ".json": "application/json; charset=utf-8",
};

function sendFile(res, path, status = 200) {
  const type = MIME[extname(path).toLowerCase()] ?? "application/octet-stream";
  res.writeHead(status, {
    "Content-Type": type,
    "Cache-Control": "no-store",
    "Access-Control-Allow-Origin": "*",
  });
  createReadStream(path).pipe(res);
}

function readBody(req) {
  return new Promise((resolveBody, reject) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => resolveBody(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

async function proxy(req, res, url) {
  const target = new URL(url.pathname + url.search, BACKEND);
  const headers = new Headers();
  for (const [key, value] of Object.entries(req.headers)) {
    if (value === undefined) continue;
    if (["host", "connection", "content-length", "transfer-encoding"].includes(key)) continue;
    headers.set(key, Array.isArray(value) ? value.join(",") : value);
  }
  const method = req.method ?? "GET";
  let body;
  if (method !== "GET" && method !== "HEAD") {
    const raw = await readBody(req);
    body = raw.length ? raw : undefined;
  }
  try {
    const upstream = await fetch(target, { method, headers, body, redirect: "manual" });
    const outHeaders = {};
    upstream.headers.forEach((value, key) => {
      if (["content-encoding", "content-length", "transfer-encoding", "connection"].includes(key)) return;
      outHeaders[key] = value;
    });
    res.writeHead(upstream.status, outHeaders);
    const buffer = Buffer.from(await upstream.arrayBuffer());
    res.end(buffer);
  } catch (error) {
    res.writeHead(502, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ code: "bad_gateway", message: String(error) }));
  }
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", `http://127.0.0.1:${PORT}`);

  if (url.pathname === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok" }));
    return;
  }

  if (url.pathname.startsWith("/v1/") && req.method !== "OPTIONS") {
    await proxy(req, res, url);
    return;
  }

  if (url.pathname === "/collect" && req.method === "POST") {
    try {
      const raw = await readBody(req);
      const payload = JSON.parse(raw.toString("utf8"));
      const run = String(payload.run ?? "unknown").replace(/[^a-zA-Z0-9._-]/g, "_");
      if (payload.remote_wav_b64) {
        const wav = Buffer.from(payload.remote_wav_b64, "base64");
        writeFileSync(join(RUNS_DIR, `${run}.wav`), wav);
        payload.remote_wav_path = `${run}.wav`;
        payload.remote_wav_bytes = wav.length;
        delete payload.remote_wav_b64;
      }
      writeFileSync(join(RUNS_DIR, `${run}.json`), JSON.stringify(payload, null, 2));
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: true, run }));
      console.log(`[collect] ${run} scenario=${payload.scenario} turns=${payload.turns?.length ?? 0}`);
    } catch (error) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: false, message: String(error) }));
    }
    return;
  }

  if (url.pathname === "/" || url.pathname === "/index.html") {
    sendFile(res, join(STATIC_DIR, "index.html"));
    return;
  }

  if (url.pathname.startsWith("/audio/")) {
    const name = normalize(url.pathname.slice("/audio/".length)).replace(/^(\.\.[/\\])+/, "");
    const path = join(AUDIO_DIR, name);
    if (!path.startsWith(AUDIO_DIR)) {
      res.writeHead(403).end();
      return;
    }
    if (!existsSync(path) || !statSync(path).isFile()) {
      res.writeHead(404).end();
      return;
    }
    sendFile(res, path);
    return;
  }

  const staticPath = join(STATIC_DIR, normalize(url.pathname));
  if (staticPath.startsWith(STATIC_DIR) && existsSync(staticPath) && statSync(staticPath).isFile()) {
    sendFile(res, staticPath);
    return;
  }

  res.writeHead(404, { "Content-Type": "text/plain" });
  res.end("not found");
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`[harness] http://127.0.0.1:${PORT}  backend=${BACKEND}`);
  console.log(`[harness] static=${STATIC_DIR}`);
  console.log(`[harness] audio=${AUDIO_DIR}`);
  console.log(`[harness] runs=${RUNS_DIR}`);
});
