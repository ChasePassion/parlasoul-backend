/**
 * Injected into the real chat page before any app code runs.
 *
 * - replaces getUserMedia with a synthetic AudioContext-backed track
 * - taps the remote <audio> stream for RMS/pause analysis and WAV capture
 * - taps DataChannel JSON events as they arrive from the server
 * - attaches the telemetry observation id to POST /v1/realtime/session
 *
 * Everything is measured on the page's performance.now() clock.
 */
(() => {
  const T0 = performance.now();
  const now = () => Math.round((performance.now() - T0) * 10) / 10;
  const AUDIO_BASE =
    window.__VOICE_LAB_AUDIO_BASE__ || "http://127.0.0.1:8787/audio";

  const probe = {
    timeline: [],
    serverEvents: [],
    rms: [],
    errors: [],
    remoteWav: null,
    ready: false,
  };
  window.__PROBE__ = probe;

  function mark(name, extra) {
    probe.timeline.push({ name, t: now(), ...(extra || {}) });
    return now();
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  // ---- synthetic microphone ------------------------------------------------
  const micCtx = new AudioContext({ sampleRate: 48000, latencyHint: "interactive" });
  const micDest = micCtx.createMediaStreamDestination();
  {
    const silent = micCtx.createBufferSource();
    silent.buffer = micCtx.createBuffer(1, 4800, 48000);
    silent.loop = true;
    silent.connect(micDest);
    silent.start();
  }
  const decoded = new Map();
  async function loadSpeech(name) {
    if (decoded.has(name)) return decoded.get(name);
    const response = await fetch(`${AUDIO_BASE}/${name}.wav`, { mode: "cors" });
    if (!response.ok) throw new Error(`missing asset ${name}`);
    const buffer = await micCtx.decodeAudioData(await response.arrayBuffer());
    decoded.set(name, buffer);
    return buffer;
  }
  function audioToPerf(ctxTime) {
    return performance.now() + (ctxTime - micCtx.currentTime) * 1000;
  }
  let activeSource = null;
  probe.speak = async (name) => {
    const buffer = await loadSpeech(name);
    await micCtx.resume().catch(() => {});
    const source = micCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(micDest);
    const startAt = micCtx.currentTime + 0.03;
    source.start(startAt);
    activeSource = source;
    const startT = Math.round((audioToPerf(startAt) - T0) * 10) / 10;
    const endT = Math.round((startT + buffer.duration * 1000) * 10) / 10;
    let stoppedT = null;
    const ended = new Promise((resolve) => {
      source.onended = () => {
        if (stoppedT === null) stoppedT = now();
        resolve(stoppedT);
      };
    });
    // Leading silence in the synthesized WAV: measurements that care about the
    // perceived start of speech must not count it.
    let leadMs = 0;
    try {
      const data = buffer.getChannelData(0);
      for (let i = 0; i < data.length; i += 1) {
        if (Math.abs(data[i]) > 0.02) {
          leadMs = Math.round((i / buffer.sampleRate) * 1000);
          break;
        }
      }
    } catch {
      /* ignore */
    }
    mark("mic.speech_start", { asset: name, duration_ms: Math.round(buffer.duration * 1000) });
    return {
      startT,
      endT,
      durationMs: Math.round(buffer.duration * 1000),
      leadMs,
      ended,
    };
  };
  probe.stopSpeech = () => {
    if (!activeSource) return null;
    try {
      activeSource.stop();
    } catch {
      /* already stopped */
    }
    const t = mark("mic.speech_stop_injected");
    activeSource = null;
    return t;
  };

  navigator.mediaDevices.getUserMedia = async () =>
    new MediaStream([micDest.stream.getAudioTracks()[0]]);

  // ---- remote audio tap ----------------------------------------------------
  let remoteCtx = null;
  let analyser = null;
  const recorded = [];

  function attachRemote(stream) {
    if (remoteCtx) return;
    try {
      const ctx = new AudioContext({ sampleRate: 48000, latencyHint: "interactive" });
      remoteCtx = ctx;
      const source = ctx.createMediaStreamSource(stream);
      analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0;
      source.connect(analyser);

      const mute = ctx.createGain();
      mute.gain.value = 0;
      source.connect(mute).connect(ctx.destination);

      const recorder = ctx.createScriptProcessor(2048, 1, 1);
      recorder.onaudioprocess = (event) => {
        if (recorded.length > 2400) return; // ~2 min ceiling
        recorded.push(new Float32Array(event.inputBuffer.getChannelData(0)));
      };
      source.connect(recorder);
      const sink = ctx.createGain();
      sink.gain.value = 0;
      recorder.connect(sink).connect(ctx.destination);

      const buffer = new Float32Array(analyser.fftSize);
      const timer = setInterval(() => {
        if (!analyser) return;
        analyser.getFloatTimeDomainData(buffer);
        let sum = 0;
        for (let i = 0; i < buffer.length; i += 1) sum += buffer[i] * buffer[i];
        // The remote analyser taps the MediaStream, which is upstream of the
        // <audio> element. Fold in element volume/mute so energy numbers model
        // what the user actually hears (local barge-in ducks the element).
        const el = document.querySelector("audio");
        const gain = el ? (el.muted ? 0 : el.volume) : 1;
        const rms = Math.sqrt(sum / buffer.length) * gain;
        probe.rms.push([now(), Math.round(rms * 1e6) / 1e6]);
      }, 10);
      probe._rmsTimer = timer;
      ctx.resume().catch(() => {});
      mark("remote.captured");
    } catch (error) {
      probe.errors.push(`remote capture: ${String(error)}`);
    }
  }

  const srcObject = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "srcObject");
  if (srcObject && srcObject.set && srcObject.get) {
    Object.defineProperty(HTMLMediaElement.prototype, "srcObject", {
      configurable: true,
      enumerable: srcObject.enumerable,
      get() {
        return srcObject.get.call(this);
      },
      set(value) {
        srcObject.set.call(this, value);
        if (value instanceof MediaStream) {
          mark("remote.srcObject_set");
          attachRemote(value);
        }
      },
    });
  }

  // ---- data channel tap ---------------------------------------------------
  const onmessage = Object.getOwnPropertyDescriptor(RTCDataChannel.prototype, "onmessage");
  if (onmessage && onmessage.set && onmessage.get) {
    Object.defineProperty(RTCDataChannel.prototype, "onmessage", {
      configurable: true,
      enumerable: onmessage.enumerable,
      get() {
        return onmessage.get.call(this);
      },
      set(handler) {
        if (typeof handler !== "function") {
          onmessage.set.call(this, handler);
          return;
        }
        onmessage.set.call(this, (event) => {
          try {
            const payload =
              typeof event.data === "string" ? JSON.parse(event.data) : null;
            if (payload) {
              probe.serverEvents.push({
                type: payload.type,
                t: now(),
                generation_id: payload.generation_id ?? null,
              });
              if (payload.type === "error") probe.errors.push(String(payload.code || "error"));
            }
          } catch {
            /* ignore non-JSON */
          }
          handler.call(this, event);
        });
      },
    });
  }

  // ---- session create -> attach observation ------------------------------
  const rawFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    try {
      const url = typeof input === "string" ? input : input.url;
      if (
        init &&
        init.method === "POST" &&
        typeof url === "string" &&
        url.includes("/v1/realtime/session") &&
        window.__PROBE_OBS__ &&
        typeof init.body === "string"
      ) {
        const body = JSON.parse(init.body);
        body.observation_id = window.__PROBE_OBS__;
        init = { ...init, body: JSON.stringify(body) };
        mark("session.observation_attached");
      }
    } catch {
      /* fall through with original request */
    }
    return rawFetch(input, init);
  };

  // ---- readout ------------------------------------------------------------
  probe.state = () => ({
    timeline: probe.timeline,
    serverEvents: probe.serverEvents,
    rms: probe.rms,
    errors: probe.errors,
  });

  probe.firstAudibleAfter = (t, sustainMs) => {
    const threshold = 0.006;
    let streak = 0;
    let start = 0;
    for (const [rt, rms] of probe.rms) {
      if (rt < t) continue;
      if (rms >= threshold) {
        if (streak === 0) start = rt;
        streak += 1;
        if ((streak - 1) * 10 >= (sustainMs ?? 40)) return start;
      } else {
        streak = 0;
      }
    }
    return null;
  };

  probe.silenceAfter = (t, sustainMs) => {
    const threshold = 0.004;
    let streak = 0;
    let start = 0;
    for (const [rt, rms] of probe.rms) {
      if (rt < t) continue;
      if (rms < threshold) {
        if (streak === 0) start = rt;
        streak += 1;
        if ((streak - 1) * 10 >= (sustainMs ?? 120)) return start;
      } else {
        streak = 0;
      }
    }
    return null;
  };

  probe.energyBetween = (from, to) => {
    let sum = 0;
    let count = 0;
    for (const [rt, rms] of probe.rms) {
      if (rt >= from && rt <= to) {
        sum += rms;
        count += 1;
      }
    }
    return count ? sum / count : 0;
  };

  probe.pauses = (minGapMs) => {
    const threshold = 0.006;
    const segs = [];
    let start = null;
    let last = 0;
    for (const [rt, rms] of probe.rms) {
      if (rms >= threshold) {
        if (start === null) start = rt;
        last = rt;
      } else if (start !== null && rt - last >= (minGapMs ?? 150)) {
        segs.push([start, last]);
        start = null;
      }
    }
    if (start !== null) segs.push([start, last]);
    const pauses = [];
    for (let i = 1; i < segs.length; i += 1) {
      pauses.push(Math.round((segs[i][0] - segs[i - 1][1]) * 10) / 10);
    }
    return { segments: segs, pauses };
  };

  probe.finishWav = () => {
    if (!recorded.length) return null;
    let total = 0;
    for (const chunk of recorded) total += chunk.length;
    const pcm = new Int16Array(total);
    let offset = 0;
    for (const chunk of recorded) {
      for (let i = 0; i < chunk.length; i += 1) {
        const s = Math.max(-1, Math.min(1, chunk[i]));
        pcm[offset + i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      offset += chunk.length;
    }
    const buffer = new ArrayBuffer(44 + pcm.length * 2);
    const view = new DataView(buffer);
    const write = (at, text) => {
      for (let i = 0; i < text.length; i += 1) view.setUint8(at + i, text.charCodeAt(i));
    };
    write(0, "RIFF");
    view.setUint32(4, 36 + pcm.length * 2, true);
    write(8, "WAVE");
    write(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, 48000, true);
    view.setUint32(28, 96000, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    write(36, "data");
    view.setUint32(40, pcm.length * 2, true);
    new Int16Array(buffer, 44).set(pcm);
    const bytes = new Uint8Array(buffer);
    let binary = "";
    const step = 0x8000;
    for (let i = 0; i < bytes.length; i += step) {
      binary += String.fromCharCode(...bytes.subarray(i, i + step));
    }
    return btoa(binary);
  };

  probe.sleep = sleep;
  probe.mark = mark;
  probe.ready = true;
  mark("probe.ready");
})();
