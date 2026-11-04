#!/usr/bin/env node
/**
 * Real-UI end-to-end voice test.
 *
 * Opens the production chat page in Chrome with a real better-auth session
 * cookie, injects a synthetic microphone + remote-audio probe, starts realtime
 * voice through the real composer button, runs a scenario, and records
 * latency/quality metrics from the page clock plus the backend telemetry API.
 *
 *   node e2e-real-ui.mjs --scenario s1 --tag baseline
 */

import { createRequire } from "node:module";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import http from "node:http";
import https from "node:https";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const backendRoot = resolve(here, "..", "..");
const toolsRequire = createRequire(`${join(backendRoot, "tmp", "voice-lab", "tools")}/`);
const { chromium } = toolsRequire("playwright-core");

const args = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  args.set(process.argv[i].replace(/^--/, ""), process.argv[i + 1]);
}

const SCENARIO = args.get("scenario") ?? "s1";
const TAG = args.get("tag") ?? "baseline";
const BACKEND = (args.get("backend") ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const APP = (args.get("app") ?? "http://localhost:3002").replace(/\/$/, "");
const CHARACTER_ID = args.get("character") ?? "8f675602-3590-4907-afd0-f1e03c2ff425";
const AUDIO_BASE = args.get("audio-base") ?? "http://127.0.0.1:8787/audio";
const CHROME = args.get("chrome") ?? "C:/Program Files/Google/Chrome/Application/chrome.exe";
const HEADLESS = (args.get("headless") ?? "false") !== "false";
const RUNS_DIR = resolve(args.get("runs-dir") ?? join(backendRoot, "tmp", "voice-lab", "runs"));
const LOGIN_EMAIL = (args.get("login-email") ?? "").trim();
const NEON_FILE = resolve(
  args.get("neon-file") ?? join(backendRoot, "tmp", "voice-lab", "neon_url.txt"),
);
const FRONTEND_ROOT = resolve(backendRoot, "..", "parlasoul-frontend");
const SESSION_COOKIE = readSessionCookie();

mkdirSync(RUNS_DIR, { recursive: true });

let TOKEN = "";

function requestText(url, headers, timeoutMs = 120000) {
  const lib = url.startsWith("https:") ? https : http;
  return new Promise((resolve, reject) => {
    const request = lib.request(url, { method: "GET", headers }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        body += chunk;
      });
      response.on("end", () => {
        resolve({ status: response.statusCode ?? 0, headers: response.headers, body });
      });
    });
    request.setTimeout(timeoutMs, () => {
      request.destroy(new Error("token request timed out"));
    });
    request.on("error", reject);
    request.end();
  });
}

async function refreshToken() {
  let response;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      response = await requestText(`${APP}/api/auth/token`, {
        Cookie: `${SESSION_COOKIE.name}=${SESSION_COOKIE.value}`,
        Origin: APP,
      });
      break;
    } catch (error) {
      if (attempt === 1) throw error;
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }
  const payload = JSON.parse(response.body || "null");
  if (!payload?.token) {
    throw new Error(`could not refresh better-auth JWT (status=${response.status})`);
  }
  // better-auth can rotate the session token while minting the JWT.  Keep the
  // rotated value so the browser context uses the same session as the API.
  const setCookie = response.headers["set-cookie"] ?? [];
  for (const cookie of Array.isArray(setCookie) ? setCookie : [setCookie]) {
    if (!cookie.includes("better-auth.session_token")) continue;
    const pair = cookie.split(";")[0];
    const name = pair.split("=")[0];
    SESSION_COOKIE.name = name;
    SESSION_COOKIE.value = pair.slice(name.length + 1);
  }
  TOKEN = payload.token;
}

function readSessionCookie() {
  if (LOGIN_EMAIL) {
    return { name: "better-auth.session_token", value: "" };
  }
  const path = resolve(
    args.get("cookies") ?? join(backendRoot, "tmp", "voice-lab", "cookies.txt"),
  );
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    if (line.includes("better-auth.session_token")) {
      const parts = line.split("\t");
      return {
        name: parts[parts.length - 2],
        value: parts[parts.length - 1],
      };
    }
  }
  throw new Error("session cookie not found; run the OTP sign-in step first");
}

async function api(path, init = {}) {
  const headers = new Headers(init.headers ?? {});
  headers.set("Authorization", `Bearer ${TOKEN}`);
  if (init.body) headers.set("Content-Type", "application/json");
  const response = await fetch(`${BACKEND}${path}`, { ...init, headers });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new Error(`${path} -> ${response.status} ${JSON.stringify(payload)}`);
  return payload?.data ?? payload;
}

const TELEMETRY_SCENARIO = { s3: "barge_in" };

async function createChat() {
  const created = await api("/v1/chats", {
    method: "POST",
    body: JSON.stringify({ character_id: CHARACTER_ID }),
  });
  return created.chat.id;
}

async function createObservation(chatId) {
  const created = await api("/v1/realtime/telemetry/sessions", {
    method: "POST",
    body: JSON.stringify({
      chat_id: chatId,
      character_id: CHARACTER_ID,
      mode: "chat",
      scenario: TELEMETRY_SCENARIO[SCENARIO] ?? "normal",
      experiment_tag: TAG,
      client_build: "voice-lab-real-ui",
      browser_name: "chrome",
      os_name: "windows",
    }),
  });
  return created.observation_id;
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function probeState(page) {
  return page.evaluate(() => window.__PROBE__?.state() ?? null);
}

/** Poll the page clock for a condition, with a hard timeout. */
async function waitPage(page, fn, arg, timeoutMs, label) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const value = await page.evaluate(fn, arg);
    if (value) return value;
    await wait(40);
  }
  await page.evaluate((name) => window.__PROBE__?.mark("probe.timeout", { label: name }), label);
  return null;
}

const waits = {
  async firstAudible(page, afterT, timeoutMs = 20000) {
    return waitPage(
      page,
      (t) => window.__PROBE__.firstAudibleAfter(t),
      afterT,
      timeoutMs,
      "first_audible",
    );
  },
  async serverEvent(page, type, timeoutMs = 20000, since = 0) {
    return waitPage(
      page,
      (wanted) =>
        window.__PROBE__.serverEvents.filter((e) => e.type === wanted.type).length >
        wanted.since,
      { type, since },
      timeoutMs,
      `event:${type}`,
    );
  },
  async silence(page, afterT, timeoutMs = 4000) {
    return waitPage(
      page,
      (t) => window.__PROBE__.silenceAfter(t),
      afterT,
      timeoutMs,
      "silence",
    );
  },
};

async function startSpeech(page, asset) {
  return page.evaluate(async (name) => {
    const h = await window.__PROBE__.speak(name);
    window.__PROBE__.micDone = false;
    h.ended.then(() => {
      window.__PROBE__.micDone = true;
    });
    return {
      startT: h.startT,
      endT: h.endT,
      durationMs: h.durationMs,
      leadMs: h.leadMs,
      asset: name,
    };
  }, asset);
}

// ---------------------------------------------------------------------------
// Scenarios
// ---------------------------------------------------------------------------

async function runTurn(page, asset, { waitDone = true } = {}) {
  const doneBefore = await page.evaluate(
    () => window.__PROBE__.serverEvents.filter((e) => e.type === "response.done").length,
  );
  const handle = await startSpeech(page, asset);
  await waitPage(page, () => window.__PROBE__.micDone === true, null, 30000, "mic_done");
  const audible = await waits.firstAudible(page, handle.endT, 25000);
  if (waitDone) {
    await waits.serverEvent(page, "response.done", 90000, doneBefore);
    await wait(600);
  }
  return { ...handle, audibleT: audible };
}

async function scenarioS1(page) {
  return [await runTurn(page, "q_single")];
}

async function scenarioS2(page) {
  const out = [];
  for (const asset of ["q_multi1", "q_multi2", "q_multi3"]) {
    out.push(await runTurn(page, asset));
    await wait(250);
  }
  return out;
}

async function scenarioS3(page) {
  const doneBefore = await page.evaluate(
    () => window.__PROBE__.serverEvents.filter((e) => e.type === "response.done").length,
  );
  const first = await runTurn(page, "q_long", { waitDone: false });
  const audibleAt = first.audibleT ?? (await waits.firstAudible(page, first.endT, 25000));
  await wait(1200);
  const barge = await startSpeech(page, "q_barge");
  const bargeVoicedT = barge.startT + (barge.leadMs ?? 0);
  const before = await page.evaluate(
    (t) => window.__PROBE__.energyBetween(t - 250, t),
    bargeVoicedT,
  );
  const silenceAt = await waits.silence(page, bargeVoicedT, 6000);
  await waitPage(page, () => window.__PROBE__.micDone === true, null, 20000, "barge_done");
  await waits.serverEvent(page, "response.done", 90000, doneBefore);
  await wait(400);
  return [{ ...first, interrupt: { ...barge, audibleAt, silenceAt, energyBefore: before } }];
}

async function scenarioS4(page) {
  const doneBefore = await page.evaluate(
    () => window.__PROBE__.serverEvents.filter((e) => e.type === "response.done").length,
  );
  const first = await runTurn(page, "q_multi1", { waitDone: false });
  await waits.firstAudible(page, first.endT, 25000);
  await wait(700);
  const ack = await startSpeech(page, "q_backchannel");
  await waitPage(page, () => window.__PROBE__.micDone === true, null, 15000, "ack_done");
  const done = await waits.serverEvent(page, "response.done", 90000);
  await wait(300);
  return [{ ...first, backchannel: { ...ack, responseDone: Boolean(done) } }];
}

async function scenarioS5(page) {
  return [await runTurn(page, "q_long_detail")];
}

async function scenarioS6(page) {
  await wait(3000);
  return [{ silenceOnly: true }];
}

async function scenarioS7(page) {
  const first = await runTurn(page, "q_multi2", { waitDone: false });
  await waits.firstAudible(page, first.endT, 25000);
  await wait(400);
  const doneBefore = await page.evaluate(
    () => window.__PROBE__.serverEvents.filter((e) => e.type === "response.done").length,
  );
  const second = await runTurn(page, "q_multi3", { waitDone: false });
  await waits.serverEvent(page, "response.done", 90000, doneBefore);
  await wait(300);
  return [first, second];
}

const SCENARIOS = {
  s1: scenarioS1,
  s2: scenarioS2,
  s3: scenarioS3,
  s4: scenarioS4,
  s5: scenarioS5,
  s6: scenarioS6,
  s7: scenarioS7,
};

// ---------------------------------------------------------------------------
// Metric derivation
// ---------------------------------------------------------------------------

function deriveMetrics(state, turns) {
  const events = state.serverEvents;
  const timeline = state.timeline;
  const findBy = (name) => timeline.filter((e) => e.name === name);
  const serverByType = (type) => events.filter((e) => e.type === type);
  const first = (arr) => (arr.length ? arr[0].t : null);

  const speechStarts = serverByType("input_audio_buffer.speech_started").map((e) => e.t);
  const speechStops = serverByType("input_audio_buffer.speech_stopped").map((e) => e.t);
  const responseStarts = serverByType("response.started").map((e) => e.t);
  const responseDones = serverByType("response.done").map((e) => e.t);
  const audioStops = serverByType("response.audio_stopped").map((e) => e.t);

  const speechTurns = turns.filter((turn) => typeof turn?.endT === "number");
  const perTurn = speechTurns.map((turn, index) => {
    const speechEnd = turn.endT;
    const audible = turn.audibleT ?? null;
    // Match server events by time, not by array index: a turn may contain
    // several VAD segments and the reply may start after the previous one ends.
    const stopEvent = speechStops.find((t) => t >= speechEnd - 80) ?? null;
    const startedEvent =
      responseStarts.find((t) => t >= (stopEvent ?? speechEnd)) ?? null;
    const doneEvent =
      responseDones.find((t) => t >= (startedEvent ?? speechEnd)) ?? null;
    const pauses = pauseStats(state, speechEnd, doneEvent ?? speechEnd + 30000);
    return {
      turn: index + 1,
      asset: turn.asset ?? null,
      user_speech_ms: turn.durationMs ?? null,
      ttfa_ms: audible !== null ? round(audible - speechEnd) : null,
      endpointing_ms: stopEvent !== null ? round(stopEvent - speechEnd) : null,
      speech_stop_to_response_start_ms:
        startedEvent !== null && stopEvent !== null ? round(startedEvent - stopEvent) : null,
      response_start_to_audible_ms:
        audible !== null && startedEvent !== null ? round(audible - startedEvent) : null,
      speech_end_to_response_done_ms: doneEvent !== null ? round(doneEvent - speechEnd) : null,
      bot_speech_pause_count: pauses.count,
      bot_max_pause_ms: pauses.max,
      bot_speech_ms: pauses.speechMs,
      audio_stopped_seen: audioStops.length > 0,
      interrupt: turn.interrupt
        ? {
            barge_speech_ms: turn.interrupt.durationMs,
            barge_lead_silence_ms: turn.interrupt.leadMs ?? 0,
            barge_to_silence_ms:
              turn.interrupt.silenceAt !== null
                ? round(
                    turn.interrupt.silenceAt -
                      turn.interrupt.startT -
                      (turn.interrupt.leadMs ?? 0),
                  )
                : null,
            energy_before_interrupt: turn.interrupt.energyBefore ?? null,
          }
        : turn.backchannel
          ? {
              backchannel_ms: turn.backchannel.durationMs,
              response_completed: turn.backchannel.responseDone,
            }
          : null,
    };
  });

  return {
    turns: perTurn,
    timeline_marks: timeline.reduce((acc, item) => {
      acc[item.name] = (acc[item.name] ?? 0) + 1;
      return acc;
    }, {}),
    speech_starts: speechStarts,
    speech_stops: speechStops,
    response_starts: responseStarts,
    response_dones: responseDones,
    audio_stops: audioStops,
    first_audio_played: round(first(findBy("remote.captured")) ?? 0),
    errors: state.errors,
  };
}

function pauseStats(state, from, to) {
  const threshold = 0.006;
  let start = null;
  let last = null;
  const segments = [];
  for (const [t, rms] of state.rms) {
    if (t < from || t > to) continue;
    if (rms >= threshold) {
      if (start === null) start = t;
      last = t;
    } else if (start !== null && t - last >= 150) {
      segments.push([start, last]);
      start = null;
    }
  }
  if (start !== null) segments.push([start, last]);
  const pauses = [];
  for (let i = 1; i < segments.length; i += 1) {
    pauses.push(round(segments[i][0] - segments[i - 1][1]));
  }
  const speechMs = segments.reduce((sum, [a, b]) => sum + (b - a), 0);
  return {
    count: pauses.length,
    max: pauses.length ? Math.max(...pauses) : 0,
    pauses,
    speechMs: Math.round(speechMs),
  };
}

function round(value) {
  return Math.round(value * 10) / 10;
}

async function readOtpFromNeon(email) {
  const { Client } = createRequire(`${FRONTEND_ROOT}/package.json`)("pg");
  const neonUrl = readFileSync(NEON_FILE, "utf8").trim();
  const client = new Client({
    connectionString: neonUrl,
    ssl: { rejectUnauthorized: false },
  });
  await client.connect();
  try {
    for (let attempt = 0; attempt < 45; attempt += 1) {
      const result = await client.query(
        'select value from verification where identifier=$1 order by "createdAt" desc limit 1',
        [`sign-in-otp-${email}`],
      );
      if (result.rows.length > 0) {
        return result.rows[0].value.split(":")[0];
      }
      await wait(1000);
    }
    throw new Error("OTP not found in the auth database");
  } finally {
    await client.end();
  }
}

async function completeProfileSetup(page) {
  if (!page.url().includes("/setup")) return;
  const username = `voicelab${Date.now().toString().slice(-8)}`;
  const input = page.locator("#username");
  await input.waitFor({ state: "visible", timeout: 20000 });
  await input.fill(username);
  await page.click('button:has-text("完成设置")');
  await wait(4000);
}

async function loginViaBrowser(page, email) {
  await page.goto(`${APP}/login`, {
    waitUntil: "domcontentloaded",
    timeout: 90000,
  });
  await page.fill("#email-code", email);
  await page.click('button:has-text("Send code")');
  const otp = await readOtpFromNeon(email);
  await page.fill("#code", otp);
  await page.click('button:has-text("Continue with email")');
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const session = await page
      .evaluate(async () => {
        const response = await fetch("/api/auth/get-session", {
          credentials: "include",
        });
        if (!response.ok) return null;
        return response.json();
      })
      .catch(() => null);
    if (session?.session) {
      await wait(1500);
      await completeProfileSetup(page);
      return;
    }
    await wait(1000);
  }
  throw new Error("browser login did not create a session");
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: HEADLESS,
    args: [
      "--no-first-run",
      "--no-default-browser-check",
      "--autoplay-policy=no-user-gesture-required",
      "--disable-features=WebRtcHideLocalIpsWithMdns",
      "--allow-loopback-in-peer-connection",
      "--disable-background-timer-throttling",
      "--disable-backgrounding-occluded-windows",
      "--disable-renderer-backgrounding",
      "--window-size=1100,820",
    ],
  });
  const context = await browser.newContext({
    viewport: { width: 1100, height: 820 },
    permissions: ["microphone"],
    ignoreHTTPSErrors: true,
  });
  await context.addInitScript((base) => {
    window.__VOICE_LAB_AUDIO_BASE__ = base;
  }, AUDIO_BASE);
  await context.addInitScript({ path: join(here, "e2e-probe.js") });

  const page = await context.newPage();

  let chatId;
  let observationId;
  if (LOGIN_EMAIL) {
    await loginViaBrowser(page, LOGIN_EMAIL);
    TOKEN = await page.evaluate(async () => {
      const response = await fetch("/api/auth/token", { credentials: "include" });
      const payload = await response.json().catch(() => null);
      return payload?.token ?? "";
    });
    if (!TOKEN) throw new Error("browser login did not mint a JWT");
    chatId = await createChat();
    observationId = await createObservation(chatId);
    await context.addInitScript(
      (obs) => {
        window.__PROBE_OBS__ = obs;
      },
      observationId,
    );
  } else {
    await refreshToken();
    chatId = await createChat();
    observationId = await createObservation(chatId);
    await context.addInitScript(
      (obs) => {
        window.__PROBE_OBS__ = obs;
      },
      observationId,
    );
    const cookies = [
      {
        name: SESSION_COOKIE.name,
        value: SESSION_COOKIE.value,
        url: APP,
        httpOnly: true,
        secure: SESSION_COOKIE.name.startsWith("__Secure-"),
        sameSite: "Lax",
      },
    ];
    if (SESSION_COOKIE.name !== "better-auth.session_token") {
      cookies.push({
        name: "better-auth.session_token",
        value: SESSION_COOKIE.value,
        url: APP,
        httpOnly: true,
        secure: SESSION_COOKIE.name.startsWith("__Secure-"),
        sameSite: "Lax",
      });
    }
    await context.addCookies(cookies);
  }

  const stamp = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
  const runId = `${TAG}-${SCENARIO}-${stamp}`;
  console.log(`run=${runId} chat=${chatId} observation=${observationId}`);
  page.on("console", (msg) => {
    if (msg.type() === "error") console.log("[page.error]", msg.text().slice(0, 300));
  });
  page.on("pageerror", (err) => console.log("[page.exception]", String(err).slice(0, 300)));

  let result = null;
  try {
    await page.goto(`${APP}/chat/${chatId}`, { waitUntil: "domcontentloaded", timeout: 90000 });
    await completeProfileSetup(page);
    if (page.url().includes("/setup")) {
      await page.goto(`${APP}/chat/${chatId}`, { waitUntil: "domcontentloaded", timeout: 90000 });
    }
    await page.locator("#composer-plus-btn").waitFor({ state: "visible", timeout: 90000 });
    await page.locator("#composer-plus-btn").click();
    const voiceEntry = page.locator('button:has-text("语音通话")').first();
    await voiceEntry.waitFor({ state: "visible", timeout: 15000 });
    await voiceEntry.click();
    await page.locator('button[aria-label="退出语音模式"]').waitFor({ state: "visible", timeout: 45000 });
    await page.evaluate(() => {
      window.__PROBE__.mark("ui.voice_active");
      window.__PROBE__._lastT = 0;
    });

    const turns = await SCENARIOS[SCENARIO](page);
    if (!turns) throw new Error(`unknown scenario ${SCENARIO}`);
    await wait(400);

    const state = await probeState(page);
    const wav = await page.evaluate(() => window.__PROBE__.finishWav());
    result = {
      run: runId,
      tag: TAG,
      scenario: SCENARIO,
      chat_id: chatId,
      observation_id: observationId,
      metrics: deriveMetrics(state, turns),
      state,
      turns,
    };
    if (wav) {
      writeFileSync(join(RUNS_DIR, `${runId}.wav`), Buffer.from(wav, "base64"));
      result.wav_path = `${runId}.wav`;
    }
  } catch (error) {
    const pageUrl = page.url();
    const pageText = await page
      .locator("body")
      .innerText()
      .catch(() => "");
    console.log("[debug] page_url", pageUrl);
    console.log("[debug] page_text", pageText.slice(0, 500).replace(/\n/g, " | "));
    await page
      .screenshot({ path: join(RUNS_DIR, `${runId}-error.png`) })
      .catch(() => {});
    result = {
      run: runId,
      tag: TAG,
      scenario: SCENARIO,
      chat_id: chatId,
      observation_id: observationId,
      error: String(error),
      page_url: pageUrl,
      page_text: pageText.slice(0, 2000),
      state: await probeState(page).catch(() => null),
    };
  } finally {
    writeFileSync(join(RUNS_DIR, `${runId}.json`), JSON.stringify(result, null, 2));
    await browser.close();
  }

  await wait(3500);
  try {
    const telemetry = await api(`/v1/realtime/telemetry/sessions/${observationId}`);
    writeFileSync(join(RUNS_DIR, `${runId}.telemetry.json`), JSON.stringify(telemetry, null, 2));
    result.telemetry = {
      session_status: telemetry.session?.status ?? telemetry.status ?? null,
      event_count: telemetry.events?.length ?? null,
      turn_metrics: telemetry.turn_metrics ?? telemetry.turns ?? null,
    };
  } catch (error) {
    result.telemetry_error = String(error);
  }
  writeFileSync(join(RUNS_DIR, `${runId}.json`), JSON.stringify(result, null, 2));

  console.log(JSON.stringify(result.metrics ?? result.error, null, 2).slice(0, 6000));
  console.log(`saved -> ${join(RUNS_DIR, `${runId}.json`)}`);
}

await main();
