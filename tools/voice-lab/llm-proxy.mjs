#!/usr/bin/env node
/**
 * Local LLM shim for the voice lab.
 *
 * The realtime chat route is pinned to the DeepSeek provider, but the DeepSeek
 * key in the local .env is rejected upstream (401).  This proxy keeps the
 * backend code untouched: it accepts OpenAI-compatible requests on
 * /v1/chat/completions, rewrites the model to the configured DashScope model,
 * swaps the Authorization header, and streams the upstream response back
 * verbatim (including SSE).
 *
 *   node llm-proxy.mjs --port 8898
 */

import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const envPath = resolve(here, "..", "..", ".env");
const env = {};
for (const line of readFileSync(envPath, "utf8").split(/\r?\n/)) {
  const trimmed = line.trim();
  if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;
  const at = trimmed.indexOf("=");
  env[trimmed.slice(0, at).trim()] = trimmed.slice(at + 1).trim();
}

const args = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  args.set(process.argv[i].replace(/^--/, ""), process.argv[i + 1]);
}
const PORT = Number(args.get("port") ?? 8898);
const UPSTREAM = (env.LLM_DASHSCOPE_API_BASE ?? "https://dashscope.aliyuncs.com/compatible-mode/v1").replace(/\/$/, "");
const API_KEY = env.LLM_DASHSCOPE_API_KEY;
const MODEL = args.get("model") ?? "qwen-flash";

if (!API_KEY) throw new Error("LLM_DASHSCOPE_API_KEY missing from .env");

function readBody(req) {
  return new Promise((resolveBody, reject) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => resolveBody(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

const server = createServer(async (req, res) => {
  if (!req.url?.startsWith("/v1/")) {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: { message: "not found" } }));
    return;
  }
  const raw = await readBody(req);
  let body = raw;
  if (raw.length) {
    try {
      const parsed = JSON.parse(raw.toString("utf8"));
      parsed.model = MODEL;
      if (parsed.enable_thinking === undefined) parsed.enable_thinking = false;
      body = Buffer.from(JSON.stringify(parsed));
    } catch {
      /* pass through non-JSON */
    }
  }
  const target = `${UPSTREAM}${req.url.replace(/^\/v1/, "")}`;
  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${API_KEY}`,
    Accept: req.headers.accept ?? "application/json",
  };
  const t0 = Date.now();
  try {
    const upstream = await fetch(target, {
      method: req.method,
      headers,
      body: req.method === "GET" || req.method === "HEAD" ? undefined : body,
    });
    const tHeaders = Date.now() - t0;
    const outHeaders = { "Content-Type": upstream.headers.get("content-type") ?? "application/json" };
    res.writeHead(upstream.status, outHeaders);
    if (!upstream.body) {
      res.end();
      return;
    }
    const reader = upstream.body.getReader();
    const decoder = new TextDecoder();
    let tFirst = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (tFirst === null) {
        tFirst = Date.now() - t0;
        console.log(`[llm-proxy] ${new Date().toISOString()} headers_ms=${tHeaders} first_chunk_ms=${tFirst} prompt_chars=${raw.length}`);
      }
      res.write(Buffer.from(value));
    }
    res.end();
  } catch (error) {
    console.error("[llm-proxy] error", String(error));
    res.writeHead(502, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: { message: String(error) } }));
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`[llm-proxy] http://127.0.0.1:${PORT}/v1 -> ${UPSTREAM} model=${MODEL}`);
});
