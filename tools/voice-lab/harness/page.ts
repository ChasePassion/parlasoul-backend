/**
 * End-to-end realtime voice probe page.
 *
 * It imports the *production* `RealtimeVoiceSessionClient` and drives it through
 * a real WebRTC + DataChannel session against a locally running backend.  The
 * microphone is a synthetic MediaStream fed from AudioBuffers, and the remote
 * track is analysed sample-by-sample, so every latency number is measured on
 * one monotonic clock (`performance.now()`).
 */

import { RealtimeVoiceSessionClient } from "@/lib/realtime/realtime-voice-session-client";

type Json = Record<string, unknown>;

const params = new URLSearchParams(location.search);
const RUN_ID = params.get("run") ?? "run";
const SCENARIO = params.get("scenario") ?? "s1";
const TOKEN = params.get("token") ?? "";
const CHAT_ID = params.get("chat") ?? "";
const CHARACTER_ID = params.get("character") ?? "";
const OBSERVATION_ID = params.get("obs") ?? "";
const AUDIO_BASE = params.get("audioBase") ?? "/audio";
const COLLECT_URL = "/collect";

const timeline: Json[] = [];
const t0 = performance.now();
let elapsed = () => performance.now() - t0;

function mark(name: string, extra: Json = {}): number {
  const t = elapsed();
  timeline.push({ name, t: Math.round(t * 10) / 10, ...extra });
  return t;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function quantiles(values: number[], qs: number[]): Record<string, number> {
  const sorted = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  const out: Record<string, number> = {};
  if (!sorted.length) return out;
  for (const q of qs) {
    const index = Math.min(sorted.length - 1, Math.max(0, Math.round((sorted.length - 1) * q)));
    out[`p${Math.round(q * 100)}`] = Math.round(sorted[index] * 10) / 10;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Remote audio analysis
// ---------------------------------------------------------------------------

interface RmsSample {
  t: number;
  rms: number;
  ctxTime: number;
}

let remoteCtx: AudioContext | null = null;
let remoteAnalyser: AnalyserNode | null = null;
let remoteSamples: RmsSample[] = [];
let remoteCapture: { stop: () => void } | null = null;
let rmsTimer: number | null = null;

function setupRemoteCapture(stream: MediaStream): void {
  if (remoteCapture) return;
  const ctx = new AudioContext({ sampleRate: 48000, latencyHint: "interactive" });
  remoteCtx = ctx;
  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  analyser.smoothingTimeConstant = 0;
  source.connect(analyser);
  remoteAnalyser = analyser;

  const mute = ctx.createGain();
  mute.gain.value = 0;
  source.connect(mute).connect(ctx.destination);

  // Raw PCM for offline verification (ASR / energy re-analysis).
  const recorder = ctx.createScriptProcessor(2048, 1, 1);
  const recorded: Float32Array[] = [];
  let recordedSamples = 0;
  const maxSamples = 48000 * 120; // 2 minutes ceiling
  recorder.onaudioprocess = (event) => {
    if (recordedSamples >= maxSamples) return;
    const input = event.inputBuffer.getChannelData(0);
    recorded.push(new Float32Array(input));
    recordedSamples += input.length;
  };
  source.connect(recorder);
  const sink = ctx.createGain();
  sink.gain.value = 0;
  recorder.connect(sink).connect(ctx.destination);

  const buffer = new Float32Array(analyser.fftSize);
  const started = performance.now();
  let lastPush = 0;
  rmsTimer = window.setInterval(() => {
    analyser.getFloatTimeDomainData(buffer);
    let sum = 0;
    for (let i = 0; i < buffer.length; i += 1) sum += buffer[i] * buffer[i];
    const rms = Math.sqrt(sum / buffer.length);
    const now = performance.now();
    // Browsers throttle timers in background tabs; headful runs keep 10ms.
    if (now - lastPush >= 8) {
      lastPush = now;
      remoteSamples.push({ t: now - t0, rms, ctxTime: now - started });
    }
  }, 10);

  remoteCapture = {
    stop: () => {
      if (rmsTimer !== null) window.clearInterval(rmsTimer);
      rmsTimer = null;
      try {
        source.disconnect();
      } catch {
        /* no-op */
      }
      void ctx.close().catch(() => {});
      (window as unknown as { __remotePcm?: Float32Array[] }).__remotePcm = recorded;
    },
  };
}

function installSrcObjectTap(): void {
  const proto = HTMLMediaElement.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(proto, "srcObject");
  if (!descriptor || !descriptor.set || !descriptor.get) return;
  Object.defineProperty(proto, "srcObject", {
    configurable: true,
    enumerable: descriptor.enumerable,
    get() {
      return descriptor.get!.call(this);
    },
    set(value: unknown) {
      descriptor.set!.call(this, value);
      if (value instanceof MediaStream) {
        mark("remote.srcObject_set");
        setupRemoteCapture(value);
      }
    },
  });
}

function encodeWav(chunks: Float32Array[], sampleRate: number): string {
  let total = 0;
  for (const chunk of chunks) total += chunk.length;
  const pcm = new Int16Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    for (let i = 0; i < chunk.length; i += 1) {
      const s = Math.max(-1, Math.min(1, chunk[i]));
      pcm[offset + i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    offset += chunk.length;
  }
  const buffer = new ArrayBuffer(44 + pcm.length * 2);
  const view = new DataView(buffer);
  const writeString = (at: number, text: string) => {
    for (let i = 0; i < text.length; i += 1) view.setUint8(at + i, text.charCodeAt(i));
  };
  writeString(0, "RIFF");
  view.setUint32(4, 36 + pcm.length * 2, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeString(36, "data");
  view.setUint32(40, pcm.length * 2, true);
  new Int16Array(buffer, 44).set(pcm);
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

// ---------------------------------------------------------------------------
// Synthetic microphone
// ---------------------------------------------------------------------------

const micCtx = new AudioContext({ sampleRate: 48000, latencyHint: "interactive" });
const micDest = micCtx.createMediaStreamDestination();
{
  const silent = micCtx.createBufferSource();
  silent.buffer = micCtx.createBuffer(1, 4800, 48000);
  silent.loop = true;
  silent.connect(micDest);
  silent.start();
}

interface SpeechHandle {
  startT: number;
  endT: number;
  stop: () => number;
  ended: Promise<number>;
}

function nowAudio(): number {
  return micCtx.currentTime;
}

function audioTimeToPerf(ctxTime: number): number {
  return performance.now() + (ctxTime - micCtx.currentTime) * 1000;
}

async function loadSpeech(name: string): Promise<AudioBuffer> {
  const response = await fetch(`${AUDIO_BASE}/${name}.wav`);
  if (!response.ok) throw new Error(`missing speech asset: ${name}`);
  const bytes = await response.arrayBuffer();
  return micCtx.decodeAudioData(bytes);
}

function speak(buffer: AudioBuffer, leadMs = 30): SpeechHandle {
  const source = micCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(micDest);
  const startAt = nowAudio() + leadMs / 1000;
  source.start(startAt);
  let stopT: number | null = null;
  const startT = audioTimeToPerf(startAt);
  const naturalEndT = startT + buffer.duration * 1000;
  let resolveEnded: (t: number) => void = () => {};
  const ended = new Promise<number>((resolve) => {
    resolveEnded = resolve;
  });
  source.onended = () => {
    const t = stopT ?? performance.now() - t0;
    if (stopT === null) stopT = t;
    resolveEnded(t);
  };
  mark("mic.speech_start", { asset: buffer.duration.toFixed(2) });
  return {
    startT: startT - t0,
    endT: naturalEndT - t0,
    stop: () => {
      try {
        source.stop();
      } catch {
        /* already stopped */
      }
      const t = performance.now() - t0;
      if (stopT === null) stopT = t;
      resolveEnded(t);
      mark("mic.speech_stop_injected");
      return t;
    },
    ended,
  };
}

navigator.mediaDevices.getUserMedia = async () =>
  new MediaStream([micDest.stream.getAudioTracks()[0]]);

// ---------------------------------------------------------------------------
// Server event tap
// ---------------------------------------------------------------------------

const serverEvents: Json[] = [];
let lastSpeechStartT: number | null = null;
let lastSpeechStopT: number | null = null;
let lastResponseStartedT: number | null = null;
let lastResponseDoneT: number | null = null;
let audioStoppedT: number | null = null;

function tapServerEvent(raw: unknown): void {
  let payload: Json;
  try {
    payload = typeof raw === "string" ? JSON.parse(raw) : JSON.parse(new TextDecoder().decode(raw as ArrayBuffer));
  } catch {
    return;
  }
  const type = String(payload.type ?? "unknown");
  const t = Math.round(elapsed() * 10) / 10;
  serverEvents.push({ type, t, generation_id: payload.generation_id ?? null });
  if (type === "input_audio_buffer.speech_started") lastSpeechStartT = t;
  if (type === "input_audio_buffer.speech_stopped") lastSpeechStopT = t;
  if (type === "response.started") lastResponseStartedT = t;
  if (type === "response.done") lastResponseDoneT = t;
  if (type === "response.audio_stopped") audioStoppedT = t;
  if (type !== "subtitle.delta" && type !== "subtitle.final") {
    mark(`server.${type}`);
  }
}

function installDataChannelTap(): void {
  const proto = RTCDataChannel.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(proto, "onmessage");
  if (!descriptor || !descriptor.set || !descriptor.get) return;
  Object.defineProperty(proto, "onmessage", {
    configurable: true,
    enumerable: descriptor.enumerable,
    get() {
      return descriptor.get!.call(this);
    },
    set(handler: ((this: RTCDataChannel, ev: MessageEvent) => unknown) | null) {
      if (typeof handler !== "function") {
        descriptor.set!.call(this, handler);
        return;
      }
      descriptor.set!.call(this, (event: MessageEvent) => {
        tapServerEvent(event.data);
        handler.call(this, event);
      });
    },
  });
}

function installSdpTap(): void {
  const proto = RTCPeerConnection.prototype;
  const setRemote = proto.setRemoteDescription;
  proto.setRemoteDescription = async function patched(this: RTCPeerConnection, description: RTCSessionDescriptionInit) {
    mark("webrtc.remote_description_applied");
    return setRemote.call(this, description);
  };
  const setLocal = proto.setLocalDescription;
  proto.setLocalDescription = async function patched(this: RTCPeerConnection, description?: RTCLocalSessionDescriptionInit) {
    const result = await setLocal.call(this, description);
    mark("webrtc.local_description_applied");
    return result;
  };
}

// ---------------------------------------------------------------------------
// Metrics derived from the RMS timeline
// ---------------------------------------------------------------------------

const AUDIBLE_RMS = 0.006; // ~ -44 dBFS; headless Chrome is silent-safe
const SILENCE_RMS = 0.004;

function firstAudibleAfter(t: number, sustainMs = 40): number | null {
  let streak = 0;
  let streakStart = 0;
  for (const sample of remoteSamples) {
    if (sample.t < t) continue;
    if (sample.rms >= AUDIBLE_RMS) {
      if (streak === 0) streakStart = sample.t;
      streak += 1;
      if ((streak - 1) * 10 >= sustainMs) return streakStart;
    } else {
      streak = 0;
    }
  }
  return null;
}

function silenceAfter(t: number, sustainMs = 120): number | null {
  let streak = 0;
  let streakStart = 0;
  for (const sample of remoteSamples) {
    if (sample.t < t) continue;
    if (sample.rms < SILENCE_RMS) {
      if (streak === 0) streakStart = sample.t;
      streak += 1;
      if ((streak - 1) * 10 >= sustainMs) return streakStart;
    } else {
      streak = 0;
    }
  }
  return null;
}

function speechSegments(threshold = AUDIBLE_RMS, minGapMs = 150): Array<{ start: number; end: number }> {
  const segments: Array<{ start: number; end: number }> = [];
  let start: number | null = null;
  let lastAudible = 0;
  for (const sample of remoteSamples) {
    if (sample.rms >= threshold) {
      if (start === null) start = sample.t;
      lastAudible = sample.t;
    } else if (start !== null && sample.t - lastAudible >= minGapMs) {
      segments.push({ start, end: lastAudible });
      start = null;
    }
  }
  if (start !== null) segments.push({ start, end: lastAudible });
  return segments;
}

function pausedSegments(): number[] {
  const segments = speechSegments();
  const pauses: number[] = [];
  for (let i = 1; i < segments.length; i += 1) {
    pauses.push(Math.round((segments[i].start - segments[i - 1].end) * 10) / 10);
  }
  return pauses;
}

function remoteEnergyAfter(t: number, windowMs = 200): number {
  let sum = 0;
  let count = 0;
  for (const sample of remoteSamples) {
    if (sample.t >= t && sample.t <= t + windowMs) {
      sum += sample.rms;
      count += 1;
    }
  }
  return count ? sum / count : 0;
}

function maxRmsBetween(from: number, to: number): number {
  let max = 0;
  for (const sample of remoteSamples) {
    if (sample.t >= from && sample.t <= to) max = Math.max(max, sample.rms);
  }
  return max;
}

// ---------------------------------------------------------------------------
// Session orchestration
// ---------------------------------------------------------------------------

interface TurnResult {
  turn: number;
  metrics: Json;
}

const turnResults: TurnResult[] = [];
let client: RealtimeVoiceSessionClient | null = null;
let connected = false;
let botSpeaking = false;
const subtitleLog: Json[] = [];
let lastSubtitle = { user: "", assistant: "" };

function makeClient(): RealtimeVoiceSessionClient {
  const audio = document.createElement("audio");
  audio.autoplay = true;
  audio.muted = false;
  document.body.appendChild(audio);
  const instance = new RealtimeVoiceSessionClient({
    onConnected: (sessionId, source) => {
      connected = true;
      mark("session.connected", { session_id: sessionId, source: source ?? null });
    },
    onDisconnected: () => {
      connected = false;
      mark("session.disconnected");
    },
    onUserSpeakingChange: (speaking) => {
      mark(speaking ? "client.user_speaking_true" : "client.user_speaking_false");
    },
    onBotSpeakingChange: (speaking) => {
      botSpeaking = speaking;
      if (speaking) {
        void remoteAudioCtxResume();
      }
      mark(speaking ? "client.bot_speaking_true" : "client.bot_speaking_false");
    },
    onSubtitleDelta: (channel, text) => {
      lastSubtitle[channel === "assistant_raw" ? "assistant" : "user"] += text;
      subtitleLog.push({ t: elapsed(), channel, kind: "delta" });
    },
    onSubtitleFinal: (channel, text) => {
      if (channel === "assistant_raw") lastSubtitle.assistant = text;
      else if (channel === "user_raw") lastSubtitle.user = text;
      subtitleLog.push({ t: elapsed(), channel, kind: "final", length: text.length });
      mark(`subtitle.final.${channel}`, { length: text.length });
    },
    onError: (error) => {
      mark("client.error", { message: error.message });
    },
  });
  instance.attachAudioElement(audio);
  return instance;
}

async function remoteAudioCtxResume(): Promise<void> {
  if (remoteCtx && remoteCtx.state === "suspended") {
    await remoteCtx.resume().catch(() => {});
  }
}

async function waitFor(
  predicate: () => boolean,
  timeoutMs: number,
  label: string,
): Promise<boolean> {
  const started = performance.now();
  while (performance.now() - started < timeoutMs) {
    if (predicate()) return true;
    await sleep(20);
  }
  mark("probe.timeout", { label, timeout_ms: timeoutMs });
  return false;
}

function buildTurnMetrics(turn: number, speech: SpeechHandle): Json {
  const speechEnd = speech.endT;
  const ttfa = firstAudibleAfter(speechEnd);
  const speechStopped = lastSpeechStopT;
  const endpointing = speechStopped !== null && speechStopped > speechEnd - 50 ? speechStopped - speechEnd : null;
  const pauses = pausedSegments();
  return {
    turn,
    user_speech_ms: Math.round(speech.endT - speech.startT),
    ttfa_ms: ttfa !== null ? Math.round((ttfa - speechEnd) * 10) / 10 : null,
    endpointing_to_speech_stopped_ms: endpointing !== null ? Math.round(endpointing * 10) / 10 : null,
    speech_stopped_to_response_started_ms:
      lastResponseStartedT !== null && speechStopped !== null
        ? Math.round((lastResponseStartedT - speechStopped) * 10) / 10
        : null,
    speech_stopped_to_first_audible_ms:
      ttfa !== null && speechStopped !== null ? Math.round((ttfa - speechStopped) * 10) / 10 : null,
    response_started_to_first_audible_ms:
      ttfa !== null && lastResponseStartedT !== null
        ? Math.round((ttfa - lastResponseStartedT) * 10) / 10
        : null,
    bot_speech_pauses_ms: pauses,
    bot_speech_pause_count: pauses.length,
    bot_max_pause_ms: pauses.length ? Math.max(...pauses) : 0,
    user_subtitle_chars: lastSubtitle.user.length,
    assistant_subtitle_chars: lastSubtitle.assistant.length,
  };
}

function resetTurnMarkers(): void {
  lastSpeechStartT = null;
  lastSpeechStopT = null;
  lastResponseStartedT = null;
  lastResponseDoneT = null;
  audioStoppedT = null;
  lastSubtitle = { user: "", assistant: "" };
}

async function runTurn(turn: number, asset: string, options: { waitDone?: boolean } = {}): Promise<TurnResult> {
  resetTurnMarkers();
  const before = remoteSamples.length;
  const speech = await loadSpeech(asset);
  const handle = speak(speech);
  mark("turn.started", { turn, asset });
  await handle.ended;
  mark("turn.user_audio_ended", { turn });
  const firstAudible = await waitFor(
    () => firstAudibleAfter(handle.endT) !== null,
    options.waitDone === false ? 12000 : 20000,
    `ttfa.turn${turn}`,
  );
  if (!firstAudible) {
    return { turn, metrics: { turn, error: "no_bot_audio" } };
  }
  if (options.waitDone !== false) {
    await waitFor(
      () => lastResponseDoneT !== null || botSpeaking === false,
      30000,
      `done.turn${turn}`,
    );
    // Give the pacer a moment to fully drain and the analyser to catch silence.
    await sleep(300);
  }
  const metrics = buildTurnMetrics(turn, handle);
  metrics.remote_samples = remoteSamples.length - before;
  metrics.response_done_t = lastResponseDoneT;
  metrics.audio_stopped_t = audioStoppedT;
  turnResults.push({ turn, metrics });
  return { turn, metrics };
}

// ---------------------------------------------------------------------------
// Scenarios
// ---------------------------------------------------------------------------

async function connect(): Promise<void> {
  client = makeClient();
  const started = performance.now();
  await client.connect({
    chatId: CHAT_ID,
    characterId: CHARACTER_ID,
    translationEnabled: false,
    getIceConfig: async () => {
      const response = await fetch("/v1/realtime/config", {
        headers: { Authorization: `Bearer ${TOKEN}` },
      });
      const payload = await response.json();
      mark("config.fetched");
      return payload.data;
    },
  });
  mark("session.connect_total", { total_ms: Math.round(performance.now() - started) });
  await waitFor(() => connected, 15000, "session.connected");
}

async function scenarioSingleTurn(): Promise<void> {
  await runTurn(1, "q_single");
}

async function scenarioMultiTurn(): Promise<void> {
  await runTurn(1, "q_multi1");
  await sleep(250);
  await runTurn(2, "q_multi2");
  await sleep(250);
  await runTurn(3, "q_multi3");
}

async function scenarioBargeIn(): Promise<void> {
  // Turn 1: let the bot speak, interrupt ~1.2s of audible audio.
  resetTurnMarkers();
  const question = await loadSpeech("q_long");
  const first = speak(question);
  await first.ended;
  const audible = await waitFor(() => firstAudibleAfter(first.endT) !== null, 20000, "barge.first_audible");
  if (!audible) throw new Error("barge_in: no bot audio");
  const audibleAt = firstAudibleAfter(first.endT)!;
  await sleep(1200);
  const beforeInterrupt = remoteEnergyAfter(elapsed(), 200);
  const barge = await loadSpeech("q_barge");
  const bargeHandle = speak(barge);
  const interruptStartT = bargeHandle.startT;
  mark("barge.speech_start", { before_rms: Math.round(beforeInterrupt * 1e5) / 1e5 });
  await waitFor(() => silenceAfter(interruptStartT, 120) !== null, 4000, "barge.silence");
  await bargeHandle.ended;
  const silenceAt = silenceAfter(interruptStartT, 120);
  const stopLatency = silenceAt !== null ? silenceAt - interruptStartT : null;
  await waitFor(() => lastResponseDoneT !== null, 30000, "barge.next_done");
  await sleep(400);
  turnResults.push({
    turn: 1,
    metrics: {
      turn: 1,
      scenario: "barge_in",
      interrupt_after_audible_ms: 1200,
      barge_speech_ms: Math.round(bargeHandle.endT - bargeHandle.startT),
      barge_to_silence_ms: stopLatency !== null ? Math.round(stopLatency * 10) / 10 : null,
      rms_before_interrupt: Math.round(beforeInterrupt * 1e5) / 1e5,
      rms_200ms_after_interrupt_start: Math.round(remoteEnergyAfter(interruptStartT, 200) * 1e5) / 1e5,
      rms_400ms_after_interrupt_start: Math.round(remoteEnergyAfter(interruptStartT + 200, 200) * 1e5) / 1e5,
      response_audio_stopped_seen: audioStoppedT !== null,
      user_subtitle_chars: lastSubtitle.user.length,
      assistant_subtitle_chars: lastSubtitle.assistant.length,
    },
  });
}

async function scenarioBackchannel(): Promise<void> {
  resetTurnMarkers();
  const question = await loadSpeech("q_multi1");
  const first = speak(question);
  await first.ended;
  const audible = await waitFor(() => firstAudibleAfter(first.endT) !== null, 20000, "backchannel.first_audible");
  if (!audible) throw new Error("backchannel: no bot audio");
  await sleep(700);
  const ack = await loadSpeech("q_backchannel");
  const ackHandle = speak(ack);
  await ackHandle.ended;
  const pausedAt = lastResponseStartedT;
  await waitFor(() => lastResponseDoneT !== null, 30000, "backchannel.done");
  await sleep(300);
  const metrics = buildTurnMetrics(1, first);
  const done = lastResponseDoneT !== null;
  turnResults.push({
    turn: 1,
    metrics: {
      ...metrics,
      scenario: "backchannel",
      backchannel_ms: Math.round(ackHandle.endT - ackHandle.startT),
      response_completed_after_backchannel: done,
      bot_speech_pause_count: pausedSegments().length,
      max_pause_ms: pausedSegments().length ? Math.max(...pausedSegments()) : 0,
      response_started_t: pausedAt,
      audio_stopped_seen: audioStoppedT !== null,
      assistant_subtitle_chars: lastSubtitle.assistant.length,
    },
  });
}

async function scenarioLongReply(): Promise<void> {
  await runTurn(1, "q_long_detail");
}

async function scenarioSilence(): Promise<void> {
  // 3s of silence must not create a turn or call STT.
  const before = serverEvents.length;
  await sleep(3000);
  const created = serverEvents
    .slice(before)
    .some((event) => String(event.type).startsWith("conversation.turn"));
  turnResults.push({
    turn: 0,
    metrics: { scenario: "silence_only", turn_created: created, duration_ms: 3000 },
  });
}

async function scenarioRapid(): Promise<void> {
  // Two questions back-to-back while the first reply is still being produced.
  resetTurnMarkers();
  const q1 = await loadSpeech("q_multi2");
  const first = speak(q1);
  await first.ended;
  await waitFor(() => firstAudibleAfter(first.endT) !== null, 20000, "rapid.first_audible");
  await sleep(400);
  const q2 = await loadSpeech("q_multi3");
  const second = speak(q2);
  await second.ended;
  await waitFor(() => lastResponseDoneT !== null, 30000, "rapid.done");
  await sleep(400);
  turnResults.push({
    turn: 1,
    metrics: {
      scenario: "rapid_second_question",
      ttfa_ms: firstAudibleAfter(second.endT) !== null
        ? Math.round((firstAudibleAfter(second.endT)! - second.endT) * 10) / 10
        : null,
      assistant_subtitle_chars: lastSubtitle.assistant.length,
    },
  });
}

const SCENARIOS: Record<string, () => Promise<void>> = {
  s1: scenarioSingleTurn,
  s2: scenarioMultiTurn,
  s3: scenarioBargeIn,
  s4: scenarioBackchannel,
  s5: scenarioLongReply,
  s6: scenarioSilence,
  s7: scenarioRapid,
};

// ---------------------------------------------------------------------------
// Entry
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  installSrcObjectTap();
  installDataChannelTap();
  installSdpTap();
  mark("probe.ready", { scenario: SCENARIO, run: RUN_ID });

  const runner = SCENARIOS[SCENARIO];
  if (!runner) throw new Error(`unknown scenario ${SCENARIO}`);

  try {
    await connect();
    // Chrome starts the audio context suspended until a gesture; resume it.
    await micCtx.resume();
    await remoteAudioCtxResume();
    mark("scenario.started");
    await runner();
  } catch (error) {
    mark("scenario.error", { message: error instanceof Error ? error.message : String(error) });
  } finally {
    await sleep(300);
    if (remoteCapture) remoteCapture.stop();
    try {
      await client?.disconnect();
    } catch {
      /* no-op */
    }
    await postResults();
  }
}

async function postResults(): Promise<void> {
  const pcm = (window as unknown as { __remotePcm?: Float32Array[] }).__remotePcm ?? [];
  const wav = pcm.length ? encodeWav(pcm, 48000) : "";
  const body = {
    run: RUN_ID,
    scenario: SCENARIO,
    chat_id: CHAT_ID,
    character_id: CHARACTER_ID,
    observation_id: OBSERVATION_ID || null,
    turns: turnResults,
    timeline,
    server_events: serverEvents,
    subtitles: subtitleLog,
    rms: remoteSamples.map((s) => [Math.round(s.t * 10) / 10, Math.round(s.rms * 1e6) / 1e6]),
    remote_wav_b64: wav,
    user_agent: navigator.userAgent,
    probe: { version: 1, audible_rms: AUDIBLE_RMS, silence_rms: SILENCE_RMS },
  };
  await fetch(COLLECT_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  mark("probe.posted");
  document.title = `DONE ${RUN_ID} ${SCENARIO}`;
  const status = document.createElement("pre");
  status.id = "status";
  status.textContent = "DONE";
  document.body.appendChild(status);
}

void main();
