// tools/voice-lab/harness/api-shim.ts
function token() {
  return window.__VOICE_LAB_TOKEN__ ?? "";
}
function observationId() {
  return window.__VOICE_LAB_OBSERVATION__ ?? null;
}
async function request(path, init = {}) {
  const headers = new Headers(init.headers ?? {});
  headers.set("Authorization", `Bearer ${token()}`);
  if (init.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...init, headers });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const message = payload && typeof payload === "object" && "message" in payload ? String(payload.message) : `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload && typeof payload === "object" && "data" in payload ? payload.data : payload;
}
async function getRealtimeIceConfig(options) {
  return request("/v1/realtime/config", {
    signal: options?.signal
  });
}
async function createRealtimeSession(body, options) {
  const observation = observationId();
  return request("/v1/realtime/session", {
    method: "POST",
    signal: options?.signal,
    body: JSON.stringify({ ...body, observation_id: observation ?? void 0 })
  });
}
async function deleteRealtimeSession(sessionId, options) {
  await request(`/v1/realtime/session/${sessionId}`, {
    method: "DELETE",
    signal: options?.signal
  });
}

// ../parlasoul-frontend/src/lib/logger/format.ts
var SUMMARY_FIELDS = [
  "event",
  "character_id",
  "error_status",
  "error_code",
  "elapsed_ms",
  "user_id",
  "mode",
  "scheduled",
  "seq_from",
  "seq_to",
  "buffered_ms",
  "queue_remain",
  "active_sources",
  "next_start",
  "started",
  "finished"
];
function formatEntry(level, module, event, message, extra) {
  return {
    ts: (/* @__PURE__ */ new Date()).toISOString().slice(0, 19).replace("T", " "),
    level,
    module,
    event,
    message,
    ...extra
  };
}
function formatHumanReadable(entry) {
  const parts = [`[${entry.ts}]`, `[${entry.level}]`, `[${entry.module}]`, entry.message];
  for (const field of SUMMARY_FIELDS) {
    if (entry[field] !== void 0) {
      parts.push(`[${field}=${entry[field]}]`);
    }
  }
  return parts.join(" ");
}

// ../parlasoul-frontend/src/lib/token-store.ts
var ApiError = class extends Error {
  constructor(status, code, detail) {
    super(detail || `API Error: ${status}`);
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.name = "ApiError";
  }
};

// ../parlasoul-frontend/src/lib/logger/events.ts
var Module = {
  CHARACTER: "character",
  REALTIME: "realtime",
  AUTH: "auth",
  VOICE: "voice"
};
var RealtimeEvent = {
  START_REQUESTED: "realtime.start_requested",
  CONNECT_STARTED: "realtime.connect_started",
  CONNECT_STAGE: "realtime.connect_stage",
  CONNECT_SIGNALLED: "realtime.connect_signalled",
  CONNECT_TIMEOUT: "realtime.connect.timeout",
  DISCONNECTED: "realtime.disconnected",
  START_ABORTED: "realtime.start_aborted",
  START_FAILED: "realtime.start_failed",
  MIC_CAPTURE_REQUESTED: "realtime.mic_capture_requested",
  MIC_CAPTURE_APPLIED: "realtime.mic_capture_applied",
  MIC_CAPTURE_FAILED: "realtime.mic_capture_failed"
};

// ../parlasoul-frontend/src/lib/logger/index.ts
function consoleTransport(entry) {
  const readable = formatHumanReadable(entry);
  if (entry.level === "ERROR") {
    console.error(readable, entry);
  } else if (entry.level === "WARN") {
    console.warn(readable, entry);
  } else {
    console.log(readable, entry);
  }
}
var LOG_API = "/api/logs";
var LOG_BATCH_SIZE = 20;
var LOG_FLUSH_MS = 500;
var batchBuffer = [];
var flushTimer = null;
function flushBatch() {
  if (flushTimer) {
    clearTimeout(flushTimer);
    flushTimer = null;
  }
  if (batchBuffer.length === 0) return;
  const batch = batchBuffer.splice(0);
  fetch(LOG_API, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(batch)
  }).catch(() => {
  });
}
function batchFetchTransport(entry) {
  if (entry.level !== "ERROR" && false) return;
  batchBuffer.push(entry);
  if (batchBuffer.length >= LOG_BATCH_SIZE) {
    flushBatch();
  } else if (!flushTimer) {
    flushTimer = setTimeout(flushBatch, LOG_FLUSH_MS);
  }
}
var transports = [
  consoleTransport,
  batchFetchTransport
];
function emit(level, module, event, message, extra) {
  const entry = formatEntry(level, module, event, message, extra);
  transports.forEach((t) => t(entry));
}
var logger = {
  info: (module, event, message, extra) => emit("INFO", module, event, message, extra),
  warn: (module, event, message, extra) => emit("WARN", module, event, message, extra),
  error: (module, event, message, extra) => emit("ERROR", module, event, message, extra),
  fromError: (module, err, event, extra) => {
    if (err instanceof ApiError) {
      emit("ERROR", module, event, `API Error ${err.status}: ${err.detail || err.message}`, {
        error_status: err.status,
        error_code: err.code,
        error_detail: err.detail,
        ...extra
      });
    } else if (err instanceof Error) {
      emit("ERROR", module, event, err.message, {
        error_type: err.name,
        ...extra
      });
    } else {
      emit("ERROR", module, event, String(err), extra);
    }
  }
};

// ../parlasoul-frontend/src/lib/voice/mic-errors.ts
function mapMicStartError(err) {
  if (typeof window !== "undefined" && !window.isSecureContext) {
    return "MIC_INSECURE_CONTEXT";
  }
  if (err instanceof DOMException) {
    switch (err.name) {
      case "NotAllowedError":
      case "PermissionDeniedError":
      case "SecurityError":
        return "MIC_PERMISSION_DENIED";
      case "NotFoundError":
      case "DevicesNotFoundError":
        return "MIC_DEVICE_NOT_FOUND";
      case "NotReadableError":
      case "TrackStartError":
        return "MIC_DEVICE_BUSY";
      case "OverconstrainedError":
      case "ConstraintNotSatisfiedError":
        return "MIC_CONSTRAINT_UNSUPPORTED";
      default:
        return "MIC_START_FAILED";
    }
  }
  if (err instanceof TypeError) {
    return "MIC_API_UNAVAILABLE";
  }
  return "MIC_START_FAILED";
}

// ../parlasoul-frontend/src/lib/realtime/realtime-voice-session-client.ts
var ICE_GATHERING_TIMEOUT_MS = 2500;
var RealtimeVoiceSessionClient = class {
  pc = null;
  dc = null;
  localStream = null;
  localAudioTrack = null;
  micAudioContext = null;
  micAnalyser = null;
  micSource = null;
  audioSender = null;
  remoteStream = null;
  audioElement = null;
  sessionId = null;
  handlers;
  /** Monotonic counter bumped by disconnect(). connect() checks this after each
   *  await to bail out early when an external disconnect() raced with it. */
  _isMicCaptureEnabled = true;
  disconnectGeneration = 0;
  constructor(handlers = {}) {
    this.handlers = handlers;
  }
  attachAudioElement(element) {
    this.audioElement = element;
    if (element && this.remoteStream) {
      element.srcObject = this.remoteStream;
      element.muted = false;
      element.volume = 1;
      element.setAttribute("playsinline", "true");
      element.autoplay = true;
      void element.play().catch(() => {
      });
    }
  }
  getMicAnalyserNode() {
    return this._isMicCaptureEnabled ? this.micAnalyser : null;
  }
  async connect(params2) {
    const connectStartedAt = performance.now();
    const timings = {};
    const markDuration = (key, startedAt) => {
      timings[key] = Math.round(performance.now() - startedAt);
    };
    logger.info(
      Module.REALTIME,
      RealtimeEvent.CONNECT_STARTED,
      "Realtime WebRTC connect started",
      {
        chat_id: params2.chatId,
        character_id: params2.characterId,
        has_existing_session: Boolean(this.sessionId)
      }
    );
    await this.disconnect({ emitDisconnected: false });
    const generation = this.disconnectGeneration;
    const bailIfStale = () => {
      if (this.disconnectGeneration !== generation) {
        throw new Error("DISCONNECTED_DURING_CONNECT");
      }
      throwIfAborted(params2.signal);
    };
    try {
      const configStartedAt = performance.now();
      const realtimeConfigPromise = (params2.getIceConfig ? params2.getIceConfig(params2.signal) : getRealtimeIceConfig({ signal: params2.signal })).then((config) => {
        markDuration("config_fetch_ms", configStartedAt);
        timings.ice_server_count = config.ice_servers.length;
        timings.credential_ttl_seconds = config.credential_ttl_seconds;
        return config;
      });
      const mediaStartedAt = performance.now();
      const localStreamPromise = this.acquireLocalStream().then((stream) => {
        markDuration("media_acquire_ms", mediaStartedAt);
        return stream;
      });
      const [configResult, mediaResult] = await Promise.allSettled([
        realtimeConfigPromise,
        localStreamPromise
      ]);
      const stopResolvedLocalStream = () => {
        if (mediaResult.status === "fulfilled") {
          mediaResult.value.getTracks().forEach((track) => track.stop());
        }
      };
      if (this.disconnectGeneration !== generation || params2.signal?.aborted) {
        stopResolvedLocalStream();
      }
      bailIfStale();
      if (configResult.status === "rejected") {
        stopResolvedLocalStream();
        throw configResult.reason;
      }
      if (mediaResult.status === "rejected") {
        throw mediaResult.reason;
      }
      const realtimeConfig = configResult.value;
      this.localStream = mediaResult.value;
      this.localAudioTrack = this.localStream.getAudioTracks()[0] ?? null;
      this.setupMicAnalyser(this.localStream);
      bailIfStale();
      this.remoteStream = new MediaStream();
      this.pc = new RTCPeerConnection({
        iceServers: realtimeConfig.ice_servers
      });
      this.dc = this.pc.createDataChannel("realtime-control", { ordered: true });
      this.dc.onmessage = (event) => {
        try {
          const payload = JSON.parse(
            typeof event.data === "string" ? event.data : new TextDecoder().decode(event.data)
          );
          this.handleServerEvent(payload);
        } catch (error) {
          this.handlers.onError?.(
            error instanceof Error ? error : new Error("Invalid realtime event")
          );
        }
      };
      this.pc.ontrack = (event) => {
        if (!this.remoteStream) {
          this.remoteStream = new MediaStream();
        }
        if (event.track.kind === "audio") {
          this.remoteStream.addTrack(event.track);
        } else if (event.streams[0]) {
          event.streams[0].getAudioTracks().forEach((track) => {
            this.remoteStream?.addTrack(track);
          });
        }
        if (this.audioElement) {
          this.audioElement.srcObject = this.remoteStream;
          this.audioElement.muted = false;
          this.audioElement.volume = 1;
          this.audioElement.setAttribute("playsinline", "true");
          this.audioElement.autoplay = true;
          void this.audioElement.play().catch(() => {
          });
        }
      };
      this.pc.onconnectionstatechange = () => {
        const state = this.pc?.connectionState;
        if (state === "failed") {
          const shouldReport = Boolean(this.sessionId);
          void this.disconnect();
          if (shouldReport) {
            this.handlers.onError?.(new Error("\u5B9E\u65F6\u901A\u8BDD\u8FDE\u63A5\u5DF2\u4E2D\u65AD"));
          }
        }
      };
      const localStream = this.localStream;
      if (!localStream) {
        throw new Error("\u5B9E\u65F6\u901A\u8BDD\u9EA6\u514B\u98CE\u4E0D\u53EF\u7528");
      }
      localStream.getTracks().forEach((track) => {
        const sender = this.pc?.addTrack(track, localStream) ?? null;
        if (track.kind === "audio") {
          this.audioSender = sender;
        }
      });
      const offerStartedAt = performance.now();
      const offer = await this.pc.createOffer();
      markDuration("offer_create_ms", offerStartedAt);
      bailIfStale();
      const localDescriptionStartedAt = performance.now();
      await this.pc.setLocalDescription(offer);
      markDuration("local_description_ms", localDescriptionStartedAt);
      bailIfStale();
      const iceGathering = await this.waitForIceGatheringComplete(params2.signal);
      timings.ice_wait_ms = iceGathering.elapsedMs;
      timings.ice_gathering_timed_out = iceGathering.timedOut;
      timings.ice_gathering_state = iceGathering.state;
      bailIfStale();
      const sessionPostStartedAt = performance.now();
      const negotiated = await createRealtimeSession({
        chat_id: params2.chatId,
        character_id: params2.characterId,
        sdp: {
          type: this.pc.localDescription?.type || "offer",
          sdp: this.pc.localDescription?.sdp || ""
        }
      }, {
        signal: params2.signal
      });
      markDuration("session_post_ms", sessionPostStartedAt);
      bailIfStale();
      this.sessionId = negotiated.session_id;
      const remoteDescriptionStartedAt = performance.now();
      await this.pc.setRemoteDescription(
        new RTCSessionDescription({
          type: negotiated.sdp.type,
          sdp: negotiated.sdp.sdp
        })
      );
      markDuration("remote_description_ms", remoteDescriptionStartedAt);
      bailIfStale();
      this.sendEvent({
        type: "session.update",
        translation_enabled: params2.translationEnabled
      });
      logger.info(
        Module.REALTIME,
        RealtimeEvent.CONNECT_SIGNALLED,
        "Realtime connect advanced after SDP answer",
        {
          session_id: negotiated.session_id,
          source: "sdp_answer_applied",
          connect_total_ms: Math.round(performance.now() - connectStartedAt),
          ...timings
        }
      );
      this.handlers.onConnected?.(negotiated.session_id, "sdp_answer_applied");
    } catch (error) {
      if (error instanceof Error && error.message === "DISCONNECTED_DURING_CONNECT") {
        await this.disconnect({ emitDisconnected: false });
        return;
      }
      await this.disconnect();
      throw error;
    }
  }
  async acquireLocalStream() {
    try {
      return await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1
        }
      });
    } catch (micError) {
      throw new Error(mapMicStartError(micError));
    }
  }
  async disconnect(options) {
    const emitDisconnected = options?.emitDisconnected ?? true;
    this.disconnectGeneration += 1;
    const hadLocalStream = Boolean(this.localStream);
    const hadPeerConnection = Boolean(this.pc);
    const hadDataChannel = Boolean(this.dc);
    this._isMicCaptureEnabled = true;
    this.cleanupMicAnalyser();
    if (this.localStream) {
      this.localStream.getTracks().forEach((track) => track.stop());
      this.localStream = null;
    }
    this.localAudioTrack = null;
    if (this.dc) {
      this.dc.close();
      this.dc = null;
    }
    if (this.pc) {
      this.pc.close();
      this.pc = null;
    }
    this.audioSender = null;
    if (this.audioElement) {
      this.audioElement.srcObject = null;
    }
    this.remoteStream = null;
    const sid = this.sessionId;
    this.sessionId = null;
    if (sid) {
      try {
        await deleteRealtimeSession(sid);
      } catch {
      }
    }
    logger.info(
      Module.REALTIME,
      RealtimeEvent.DISCONNECTED,
      "Realtime client disconnected",
      {
        session_id: sid ?? null,
        emit_disconnected: emitDisconnected,
        had_local_stream: hadLocalStream,
        had_peer_connection: hadPeerConnection,
        had_data_channel: hadDataChannel
      }
    );
    if (emitDisconnected) {
      this.handlers.onDisconnected?.();
    }
  }
  updateSession(params2) {
    this.sendEvent({
      type: "session.update",
      translation_enabled: params2.translationEnabled
    });
  }
  async setMicCaptureEnabled(enabled) {
    logger.info(
      Module.REALTIME,
      RealtimeEvent.MIC_CAPTURE_REQUESTED,
      "Realtime mic capture toggle requested",
      {
        session_id: this.sessionId ?? null,
        enabled
      }
    );
    const audioTrack = this.localAudioTrack;
    if (!audioTrack) {
      logger.warn(
        Module.REALTIME,
        RealtimeEvent.MIC_CAPTURE_FAILED,
        "Realtime mic capture toggle skipped because local audio track is missing",
        {
          session_id: this.sessionId ?? null,
          enabled
        }
      );
      return;
    }
    try {
      this._isMicCaptureEnabled = enabled;
      audioTrack.enabled = enabled;
      if (!this.audioSender && this.pc) {
        this.audioSender = this.pc.getSenders().find((sender) => sender.track?.kind === "audio") ?? null;
      }
      if (this.audioSender) {
        if (enabled) {
          if (this.audioSender.track !== audioTrack) {
            await this.audioSender.replaceTrack(audioTrack);
          }
          this.sendEvent({ type: "input_audio_buffer.unmute" });
        } else {
          await this.audioSender.replaceTrack(null);
          this.sendEvent({ type: "input_audio_buffer.mute" });
        }
      }
      logger.info(
        Module.REALTIME,
        RealtimeEvent.MIC_CAPTURE_APPLIED,
        "Realtime mic capture toggle applied",
        {
          session_id: this.sessionId ?? null,
          enabled,
          sender_track_present: Boolean(this.audioSender?.track)
        }
      );
    } catch (error) {
      this._isMicCaptureEnabled = true;
      audioTrack.enabled = true;
      logger.fromError(Module.REALTIME, error, RealtimeEvent.MIC_CAPTURE_FAILED, {
        session_id: this.sessionId ?? null,
        enabled
      });
      throw error;
    }
  }
  cancelResponse() {
    this.sendEvent({
      type: "response.cancel"
    });
  }
  sendEvent(payload) {
    if (this.dc && this.dc.readyState === "open") {
      this.dc.send(JSON.stringify(payload));
    }
  }
  setupMicAnalyser(stream) {
    this.cleanupMicAnalyser();
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      const audioContext = new AudioCtx();
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 128;
      analyser.smoothingTimeConstant = 0.78;
      const source = audioContext.createMediaStreamSource(stream);
      source.connect(analyser);
      this.micAudioContext = audioContext;
      this.micAnalyser = analyser;
      this.micSource = source;
      if (audioContext.state === "suspended") {
        void audioContext.resume().catch(() => {
        });
      }
    } catch {
      this.cleanupMicAnalyser();
    }
  }
  cleanupMicAnalyser() {
    if (this.micSource) {
      try {
        this.micSource.disconnect();
      } catch {
      }
      this.micSource = null;
    }
    if (this.micAudioContext) {
      void this.micAudioContext.close().catch(() => {
      });
      this.micAudioContext = null;
    }
    this.micAnalyser = null;
  }
  handleServerEvent(event) {
    if (event.type === "session.created") {
      const sessionId = typeof event.session_id === "string" ? event.session_id : this.sessionId || "";
      logger.info(
        Module.REALTIME,
        RealtimeEvent.CONNECT_SIGNALLED,
        "Realtime connect advanced after session.created",
        {
          session_id: sessionId || null,
          source: "session_created_event"
        }
      );
      this.handlers.onConnected?.(sessionId, "session_created_event");
      return;
    }
    if (event.type === "input_audio_buffer.speech_started") {
      this.handlers.onUserSpeakingChange?.(true);
      return;
    }
    if (event.type === "input_audio_buffer.speech_stopped") {
      this.handlers.onUserSpeakingChange?.(false);
      return;
    }
    if (event.type === "response.started") {
      this.handlers.onBotSpeakingChange?.(true);
      return;
    }
    if (event.type === "response.interrupted" || event.type === "response.done") {
      this.handlers.onBotSpeakingChange?.(false);
      return;
    }
    if (event.type === "subtitle.delta" && typeof event.channel === "string" && typeof event.text === "string") {
      this.handlers.onSubtitleDelta?.(event.channel, event.text);
      return;
    }
    if (event.type === "subtitle.final" && typeof event.channel === "string" && typeof event.text === "string") {
      this.handlers.onSubtitleFinal?.(event.channel, event.text);
      return;
    }
    if (event.type === "conversation.turn.created" || event.type === "conversation.turn.updated" || event.type === "conversation.turn.failed") {
      this.handlers.onConversationEvent?.(event);
      return;
    }
    if (event.type === "error") {
      this.handlers.onError?.(
        new Error(typeof event.message === "string" ? event.message : "Realtime error")
      );
    }
  }
  async waitForIceGatheringComplete(signal) {
    const startedAt = performance.now();
    if (!this.pc || this.pc.iceGatheringState === "complete") {
      return {
        elapsedMs: 0,
        timedOut: false,
        state: this.pc?.iceGatheringState ?? "unknown"
      };
    }
    return new Promise((resolve) => {
      let timeout = null;
      const finish = (timedOut) => {
        cleanup();
        resolve({
          elapsedMs: Math.round(performance.now() - startedAt),
          timedOut,
          state: this.pc?.iceGatheringState ?? "unknown"
        });
      };
      const cleanup = () => {
        this.pc?.removeEventListener("icegatheringstatechange", handleStateChange);
        signal?.removeEventListener("abort", handleAbort);
        if (timeout !== null) {
          clearTimeout(timeout);
          timeout = null;
        }
      };
      const handleStateChange = () => {
        if (this.pc?.iceGatheringState === "complete") {
          finish(false);
        }
      };
      const handleAbort = () => {
        finish(false);
      };
      this.pc?.addEventListener("icegatheringstatechange", handleStateChange);
      signal?.addEventListener("abort", handleAbort, { once: true });
      timeout = setTimeout(() => {
        logger.warn(
          Module.REALTIME,
          RealtimeEvent.CONNECT_TIMEOUT,
          "ICE gathering timed out, proceeding with available candidates",
          {
            ice_gathering_state: this.pc?.iceGatheringState ?? "unknown",
            timeout_ms: ICE_GATHERING_TIMEOUT_MS
          }
        );
        finish(true);
      }, ICE_GATHERING_TIMEOUT_MS);
    });
  }
};
function throwIfAborted(signal) {
  if (signal?.aborted) {
    throw new DOMException("The operation was aborted.", "AbortError");
  }
}

// tools/voice-lab/harness/page.ts
var params = new URLSearchParams(location.search);
var RUN_ID = params.get("run") ?? "run";
var SCENARIO = params.get("scenario") ?? "s1";
var TOKEN = params.get("token") ?? "";
var CHAT_ID = params.get("chat") ?? "";
var CHARACTER_ID = params.get("character") ?? "";
var OBSERVATION_ID = params.get("obs") ?? "";
var AUDIO_BASE = params.get("audioBase") ?? "/audio";
var COLLECT_URL = "/collect";
var timeline = [];
var t0 = performance.now();
var elapsed = () => performance.now() - t0;
function mark(name, extra = {}) {
  const t = elapsed();
  timeline.push({ name, t: Math.round(t * 10) / 10, ...extra });
  return t;
}
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
var remoteCtx = null;
var remoteAnalyser = null;
var remoteSamples = [];
var remoteCapture = null;
var rmsTimer = null;
function setupRemoteCapture(stream) {
  if (remoteCapture) return;
  const ctx = new AudioContext({ sampleRate: 48e3, latencyHint: "interactive" });
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
  const recorder = ctx.createScriptProcessor(2048, 1, 1);
  const recorded = [];
  let recordedSamples = 0;
  const maxSamples = 48e3 * 120;
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
      }
      void ctx.close().catch(() => {
      });
      window.__remotePcm = recorded;
    }
  };
}
function installSrcObjectTap() {
  const proto = HTMLMediaElement.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(proto, "srcObject");
  if (!descriptor || !descriptor.set || !descriptor.get) return;
  Object.defineProperty(proto, "srcObject", {
    configurable: true,
    enumerable: descriptor.enumerable,
    get() {
      return descriptor.get.call(this);
    },
    set(value) {
      descriptor.set.call(this, value);
      if (value instanceof MediaStream) {
        mark("remote.srcObject_set");
        setupRemoteCapture(value);
      }
    }
  });
}
function encodeWav(chunks, sampleRate) {
  let total = 0;
  for (const chunk2 of chunks) total += chunk2.length;
  const pcm = new Int16Array(total);
  let offset = 0;
  for (const chunk2 of chunks) {
    for (let i = 0; i < chunk2.length; i += 1) {
      const s = Math.max(-1, Math.min(1, chunk2[i]));
      pcm[offset + i] = s < 0 ? s * 32768 : s * 32767;
    }
    offset += chunk2.length;
  }
  const buffer = new ArrayBuffer(44 + pcm.length * 2);
  const view = new DataView(buffer);
  const writeString = (at, text) => {
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
  const chunk = 32768;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}
var micCtx = new AudioContext({ sampleRate: 48e3, latencyHint: "interactive" });
var micDest = micCtx.createMediaStreamDestination();
{
  const silent = micCtx.createBufferSource();
  silent.buffer = micCtx.createBuffer(1, 4800, 48e3);
  silent.loop = true;
  silent.connect(micDest);
  silent.start();
}
function nowAudio() {
  return micCtx.currentTime;
}
function audioTimeToPerf(ctxTime) {
  return performance.now() + (ctxTime - micCtx.currentTime) * 1e3;
}
async function loadSpeech(name) {
  const response = await fetch(`${AUDIO_BASE}/${name}.wav`);
  if (!response.ok) throw new Error(`missing speech asset: ${name}`);
  const bytes = await response.arrayBuffer();
  return micCtx.decodeAudioData(bytes);
}
function speak(buffer, leadMs = 30) {
  const source = micCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(micDest);
  const startAt = nowAudio() + leadMs / 1e3;
  source.start(startAt);
  let stopT = null;
  const startT = audioTimeToPerf(startAt);
  const naturalEndT = startT + buffer.duration * 1e3;
  let resolveEnded = () => {
  };
  const ended = new Promise((resolve) => {
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
      }
      const t = performance.now() - t0;
      if (stopT === null) stopT = t;
      resolveEnded(t);
      mark("mic.speech_stop_injected");
      return t;
    },
    ended
  };
}
navigator.mediaDevices.getUserMedia = async () => new MediaStream([micDest.stream.getAudioTracks()[0]]);
var serverEvents = [];
var lastSpeechStartT = null;
var lastSpeechStopT = null;
var lastResponseStartedT = null;
var lastResponseDoneT = null;
var audioStoppedT = null;
function tapServerEvent(raw) {
  let payload;
  try {
    payload = typeof raw === "string" ? JSON.parse(raw) : JSON.parse(new TextDecoder().decode(raw));
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
function installDataChannelTap() {
  const proto = RTCDataChannel.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(proto, "onmessage");
  if (!descriptor || !descriptor.set || !descriptor.get) return;
  Object.defineProperty(proto, "onmessage", {
    configurable: true,
    enumerable: descriptor.enumerable,
    get() {
      return descriptor.get.call(this);
    },
    set(handler) {
      if (typeof handler !== "function") {
        descriptor.set.call(this, handler);
        return;
      }
      descriptor.set.call(this, (event) => {
        tapServerEvent(event.data);
        handler.call(this, event);
      });
    }
  });
}
function installSdpTap() {
  const proto = RTCPeerConnection.prototype;
  const setRemote = proto.setRemoteDescription;
  proto.setRemoteDescription = async function patched(description) {
    mark("webrtc.remote_description_applied");
    return setRemote.call(this, description);
  };
  const setLocal = proto.setLocalDescription;
  proto.setLocalDescription = async function patched(description) {
    const result = await setLocal.call(this, description);
    mark("webrtc.local_description_applied");
    return result;
  };
}
var AUDIBLE_RMS = 6e-3;
var SILENCE_RMS = 4e-3;
function firstAudibleAfter(t, sustainMs = 40) {
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
function silenceAfter(t, sustainMs = 120) {
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
function speechSegments(threshold = AUDIBLE_RMS, minGapMs = 150) {
  const segments = [];
  let start = null;
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
function pausedSegments() {
  const segments = speechSegments();
  const pauses = [];
  for (let i = 1; i < segments.length; i += 1) {
    pauses.push(Math.round((segments[i].start - segments[i - 1].end) * 10) / 10);
  }
  return pauses;
}
function remoteEnergyAfter(t, windowMs = 200) {
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
var turnResults = [];
var client = null;
var connected = false;
var botSpeaking = false;
var subtitleLog = [];
var lastSubtitle = { user: "", assistant: "" };
function makeClient() {
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
    }
  });
  instance.attachAudioElement(audio);
  return instance;
}
async function remoteAudioCtxResume() {
  if (remoteCtx && remoteCtx.state === "suspended") {
    await remoteCtx.resume().catch(() => {
    });
  }
}
async function waitFor(predicate, timeoutMs, label) {
  const started = performance.now();
  while (performance.now() - started < timeoutMs) {
    if (predicate()) return true;
    await sleep(20);
  }
  mark("probe.timeout", { label, timeout_ms: timeoutMs });
  return false;
}
function buildTurnMetrics(turn, speech) {
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
    speech_stopped_to_response_started_ms: lastResponseStartedT !== null && speechStopped !== null ? Math.round((lastResponseStartedT - speechStopped) * 10) / 10 : null,
    speech_stopped_to_first_audible_ms: ttfa !== null && speechStopped !== null ? Math.round((ttfa - speechStopped) * 10) / 10 : null,
    response_started_to_first_audible_ms: ttfa !== null && lastResponseStartedT !== null ? Math.round((ttfa - lastResponseStartedT) * 10) / 10 : null,
    bot_speech_pauses_ms: pauses,
    bot_speech_pause_count: pauses.length,
    bot_max_pause_ms: pauses.length ? Math.max(...pauses) : 0,
    user_subtitle_chars: lastSubtitle.user.length,
    assistant_subtitle_chars: lastSubtitle.assistant.length
  };
}
function resetTurnMarkers() {
  lastSpeechStartT = null;
  lastSpeechStopT = null;
  lastResponseStartedT = null;
  lastResponseDoneT = null;
  audioStoppedT = null;
  lastSubtitle = { user: "", assistant: "" };
}
async function runTurn(turn, asset, options = {}) {
  resetTurnMarkers();
  const before = remoteSamples.length;
  const speech = await loadSpeech(asset);
  const handle = speak(speech);
  mark("turn.started", { turn, asset });
  await handle.ended;
  mark("turn.user_audio_ended", { turn });
  const firstAudible = await waitFor(
    () => firstAudibleAfter(handle.endT) !== null,
    options.waitDone === false ? 12e3 : 2e4,
    `ttfa.turn${turn}`
  );
  if (!firstAudible) {
    return { turn, metrics: { turn, error: "no_bot_audio" } };
  }
  if (options.waitDone !== false) {
    await waitFor(
      () => lastResponseDoneT !== null || botSpeaking === false,
      3e4,
      `done.turn${turn}`
    );
    await sleep(300);
  }
  const metrics = buildTurnMetrics(turn, handle);
  metrics.remote_samples = remoteSamples.length - before;
  metrics.response_done_t = lastResponseDoneT;
  metrics.audio_stopped_t = audioStoppedT;
  turnResults.push({ turn, metrics });
  return { turn, metrics };
}
async function connect() {
  client = makeClient();
  const started = performance.now();
  await client.connect({
    chatId: CHAT_ID,
    characterId: CHARACTER_ID,
    translationEnabled: false,
    getIceConfig: async () => {
      const response = await fetch("/v1/realtime/config", {
        headers: { Authorization: `Bearer ${TOKEN}` }
      });
      const payload = await response.json();
      mark("config.fetched");
      return payload.data;
    }
  });
  mark("session.connect_total", { total_ms: Math.round(performance.now() - started) });
  await waitFor(() => connected, 15e3, "session.connected");
}
async function scenarioSingleTurn() {
  await runTurn(1, "q_single");
}
async function scenarioMultiTurn() {
  await runTurn(1, "q_multi1");
  await sleep(250);
  await runTurn(2, "q_multi2");
  await sleep(250);
  await runTurn(3, "q_multi3");
}
async function scenarioBargeIn() {
  resetTurnMarkers();
  const question = await loadSpeech("q_long");
  const first = speak(question);
  await first.ended;
  const audible = await waitFor(() => firstAudibleAfter(first.endT) !== null, 2e4, "barge.first_audible");
  if (!audible) throw new Error("barge_in: no bot audio");
  const audibleAt = firstAudibleAfter(first.endT);
  await sleep(1200);
  const beforeInterrupt = remoteEnergyAfter(elapsed(), 200);
  const barge = await loadSpeech("q_barge");
  const bargeHandle = speak(barge);
  const interruptStartT = bargeHandle.startT;
  mark("barge.speech_start", { before_rms: Math.round(beforeInterrupt * 1e5) / 1e5 });
  await waitFor(() => silenceAfter(interruptStartT, 120) !== null, 4e3, "barge.silence");
  await bargeHandle.ended;
  const silenceAt = silenceAfter(interruptStartT, 120);
  const stopLatency = silenceAt !== null ? silenceAt - interruptStartT : null;
  await waitFor(() => lastResponseDoneT !== null, 3e4, "barge.next_done");
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
      assistant_subtitle_chars: lastSubtitle.assistant.length
    }
  });
}
async function scenarioBackchannel() {
  resetTurnMarkers();
  const question = await loadSpeech("q_multi1");
  const first = speak(question);
  await first.ended;
  const audible = await waitFor(() => firstAudibleAfter(first.endT) !== null, 2e4, "backchannel.first_audible");
  if (!audible) throw new Error("backchannel: no bot audio");
  await sleep(700);
  const ack = await loadSpeech("q_backchannel");
  const ackHandle = speak(ack);
  await ackHandle.ended;
  const pausedAt = lastResponseStartedT;
  await waitFor(() => lastResponseDoneT !== null, 3e4, "backchannel.done");
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
      assistant_subtitle_chars: lastSubtitle.assistant.length
    }
  });
}
async function scenarioLongReply() {
  await runTurn(1, "q_long_detail");
}
async function scenarioSilence() {
  const before = serverEvents.length;
  await sleep(3e3);
  const created = serverEvents.slice(before).some((event) => String(event.type).startsWith("conversation.turn"));
  turnResults.push({
    turn: 0,
    metrics: { scenario: "silence_only", turn_created: created, duration_ms: 3e3 }
  });
}
async function scenarioRapid() {
  resetTurnMarkers();
  const q1 = await loadSpeech("q_multi2");
  const first = speak(q1);
  await first.ended;
  await waitFor(() => firstAudibleAfter(first.endT) !== null, 2e4, "rapid.first_audible");
  await sleep(400);
  const q2 = await loadSpeech("q_multi3");
  const second = speak(q2);
  await second.ended;
  await waitFor(() => lastResponseDoneT !== null, 3e4, "rapid.done");
  await sleep(400);
  turnResults.push({
    turn: 1,
    metrics: {
      scenario: "rapid_second_question",
      ttfa_ms: firstAudibleAfter(second.endT) !== null ? Math.round((firstAudibleAfter(second.endT) - second.endT) * 10) / 10 : null,
      assistant_subtitle_chars: lastSubtitle.assistant.length
    }
  });
}
var SCENARIOS = {
  s1: scenarioSingleTurn,
  s2: scenarioMultiTurn,
  s3: scenarioBargeIn,
  s4: scenarioBackchannel,
  s5: scenarioLongReply,
  s6: scenarioSilence,
  s7: scenarioRapid
};
async function main() {
  installSrcObjectTap();
  installDataChannelTap();
  installSdpTap();
  mark("probe.ready", { scenario: SCENARIO, run: RUN_ID });
  const runner = SCENARIOS[SCENARIO];
  if (!runner) throw new Error(`unknown scenario ${SCENARIO}`);
  try {
    await connect();
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
    }
    await postResults();
  }
}
async function postResults() {
  const pcm = window.__remotePcm ?? [];
  const wav = pcm.length ? encodeWav(pcm, 48e3) : "";
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
    probe: { version: 1, audible_rms: AUDIBLE_RMS, silence_rms: SILENCE_RMS }
  };
  await fetch(COLLECT_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  mark("probe.posted");
  document.title = `DONE ${RUN_ID} ${SCENARIO}`;
  const status = document.createElement("pre");
  status.id = "status";
  status.textContent = "DONE";
  document.body.appendChild(status);
}
void main();
