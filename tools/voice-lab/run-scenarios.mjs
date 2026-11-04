#!/usr/bin/env node
/**
 * Drives the voice-lab probes: creates a fresh chat + telemetry observation per
 * scenario, launches Chrome against the probe page, waits for the collected run,
 * then merges the backend-side telemetry and chat turns into a summary file.
 *
 *   node run-scenarios.mjs --scenarios s1,s2 --tag baseline
 *
 * Requires the backend (:8000) and harness-server (:8787) to be running.
 */

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";

const args = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i].replace(/^--/, "");
  args.set(key, process.argv[i + 1]);
}

const SCENARIOS = (args.get("scenarios") ?? "s1").split(",").map((s) => s.trim()).filter(Boolean);
const TAG = args.get("tag") ?? "baseline";
const BACKEND = (args.get("backend") ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const HARNESS = (args.get("harness") ?? "http://127.0.0.1:8787").replace(/\/$/, "");
const TOKEN = args.get("token") ?? readToken();
const CHARACTER_ID = args.get("character") ?? "7419dcae-bc5f-4f4b-85f5-bbbebcdb61d8";
const CHROME = args.get("chrome") ?? "C:/Program Files/Google/Chrome/Application/chrome.exe";
const HEADLESS = (args.get("headless") ?? "new") !== "false";
const RUNS_DIR = resolve(args.get("runs-dir") ?? join(import.meta.dirname, "..", "..", "tmp", "voice-lab", "runs"));
const TIMEOUT_S = Number(args.get("timeout") ?? 150);

mkdirSync(RUNS_DIR, { recursive: true });

function readToken() {
  const path = join(import.meta.dirname, "..", "..", "tmp", "voice-lab", "test-token.txt");
  if (!existsSync(path)) throw new Error("missing token; pass --token or run jwks_server.py --mint");
  return readFileSync(path, "utf8").trim();
}

const TELEMETRY_SCENARIO = { s3: "barge_in" };

async function api(path, init = {}) {
  const headers = new Headers(init.headers ?? {});
  headers.set("Authorization", `Bearer ${TOKEN}`);
  if (init.body) headers.set("Content-Type", "application/json");
  const response = await fetch(`${BACKEND}${path}`, { ...init, headers });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new Error(`${path} -> ${response.status} ${JSON.stringify(payload)}`);
  return payload?.data ?? payload;
}

async function createChat() {
  const created = await api("/v1/chats", {
    method: "POST",
    body: JSON.stringify({ character_id: CHARACTER_ID }),
  });
  return created.chat.id;
}

async function createObservation(chatId, scenario) {
  const created = await api("/v1/realtime/telemetry/sessions", {
    method: "POST",
    body: JSON.stringify({
      chat_id: chatId,
      character_id: CHARACTER_ID,
      mode: "chat",
      scenario: TELEMETRY_SCENARIO[scenario] ?? "normal",
      experiment_tag: TAG,
      client_build: "voice-lab-1",
      browser_name: "chrome",
      os_name: "windows",
    }),
  });
  return created.observation_id ?? created.id;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function waitForRun(runId, timeoutMs) {
  const path = join(RUNS_DIR, `${runId}.json`);
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (existsSync(path)) {
      try {
        const text = readFileSync(path, "utf8");
        if (text.trim().endsWith("}")) return JSON.parse(text);
      } catch {
        /* still writing */
      }
    }
    await sleep(500);
  }
  return null;
}

function launchChrome(url, runId) {
  const profile = join(tmpdir(), `voice-lab-chrome-${runId}`);
  rmSync(profile, { recursive: true, force: true });
  const flags = [
    `--user-data-dir=${profile}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-features=WebRtcHideLocalIpsWithMdns",
    "--allow-loopback-in-peer-connection",
    "--window-size=900,700",
  ];
  if (HEADLESS === "new") flags.push("--headless=new");
  if (HEADLESS === "old") flags.push("--headless");
  flags.push(url);
  const child = spawn(CHROME, flags, { stdio: "ignore", detached: false });
  return { child, profile };
}

async function fetchTelemetry(observationId) {
  try {
    return await api(`/v1/realtime/telemetry/sessions/${observationId}`);
  } catch (error) {
    return { error: String(error) };
  }
}

async function fetchTurns(chatId) {
  try {
    const page = await api(`/v1/chats/${chatId}/turns?limit=50`);
    return page;
  } catch (error) {
    return { error: String(error) };
  }
}

async function runScenario(scenario, index) {
  const stamp = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
  const runId = `${TAG}-${scenario}-${stamp}-${index}`;
  console.log(`\n=== ${runId} ===`);
  const chatId = await createChat();
  const observationId = await createObservation(chatId, scenario);
  console.log(`chat=${chatId} observation=${observationId}`);

  const url =
    `${HARNESS}/?run=${encodeURIComponent(runId)}&scenario=${scenario}` +
    `&token=${encodeURIComponent(TOKEN)}&chat=${chatId}&character=${CHARACTER_ID}` +
    `&obs=${observationId}`;
  const { child, profile } = launchChrome(url, runId);
  child.on("exit", (code) => console.log(`chrome exited code=${code}`));

  const collected = await waitForRun(runId, TIMEOUT_S * 1000);
  try {
    child.kill("SIGKILL");
  } catch {
    /* already gone */
  }
  if (!collected) {
    console.error(`!! ${runId} produced no result within ${TIMEOUT_S}s`);
    return { run: runId, scenario, error: "timeout" };
  }

  await sleep(1200); // let the server-side recorder flush its queue
  const telemetry = await fetchTelemetry(observationId);
  const turns = await fetchTurns(chatId);

  const summary = {
    run: runId,
    tag: TAG,
    scenario,
    chat_id: chatId,
    observation_id: observationId,
    collected,
    telemetry,
    turns,
  };
  writeFileSync(join(RUNS_DIR, `${runId}.summary.json`), JSON.stringify(summary, null, 2));
  rmSync(profile, { recursive: true, force: true });

  const turnMetrics = collected.turns ?? [];
  console.log(JSON.stringify(turnMetrics, null, 1).slice(0, 4000));
  return summary;
}

const results = [];
for (let i = 0; i < SCENARIOS.length; i += 1) {
  const summary = await runScenario(SCENARIOS[i], i);
  results.push(summary);
}

const aggregatePath = join(RUNS_DIR, `${TAG}-aggregate-${Date.now()}.json`);
writeFileSync(aggregatePath, JSON.stringify(results, null, 2));
console.log(`\naggregate -> ${aggregatePath}`);
