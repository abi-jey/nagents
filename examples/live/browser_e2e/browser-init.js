/* Installed only by Chrome DevTools MCP navigate_page(initScript=...). */
(() => {
  "use strict";
  const config = globalThis.__voiceE2EConfig;
  if (!config || globalThis.__voiceE2E) return;
  const NativeContext = globalThis.AudioContext;
  const NativeWorklet = globalThis.AudioWorkletNode;
  const NativeSocket = globalThis.WebSocket;
  const nativeConnect = AudioNode.prototype.connect;
  const nativeFetch = globalThis.fetch.bind(globalThis);
  const contexts = [];
  const capture = [];
  const input = [];
  const events = [];
  const health = [];
  const limit = Math.max(30, Math.min(config.maxSeconds || 120, 900));
  const eventLimit = Math.ceil(limit * 160 + 6000);
  const origin = performance.timeOrigin;
  const installed = performance.now();
  let microphone;
  let stopped = false;
  let stopPromise;
  let measurementStarted;
  let receivedSamples = 0;
  let omitted = 0;
  const stamp = () => ({ monotonicMs: performance.now(), wallMs: Date.now() });
  const record = (type, detail = {}) => {
    if (events.length < eventLimit) events.push({ type, ...stamp(), ...detail });
    else omitted++;
  };
  const base64 = (bytes) => {
    let text = "";
    for (let i = 0; i < bytes.length; i += 8192)
      text += String.fromCharCode(...bytes.subarray(i, i + 8192));
    return btoa(text);
  };
  const pcm = (samples) => {
    const bytes = new Uint8Array(samples.length * 2), view = new DataView(bytes.buffer);
    samples.forEach((value, i) => view.setInt16(i * 2, Math.round(Math.max(-1, Math.min(1, value)) * 32767), true));
    return bytes;
  };
  const stats = (bytes) => {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    let square = 0, peak = 0;
    for (let i = 0; i + 1 < bytes.length; i += 2) {
      const value = view.getInt16(i, true) / 32768;
      square += value * value; peak = Math.max(peak, Math.abs(value));
    }
    return { samples: bytes.length / 2, rms: Math.sqrt(square / Math.max(1, bytes.length / 2)), peak };
  };
  const safeEvent = (value) => {
    if (!value || typeof value !== "object") return {};
    const fields = ["seq", "type", "speaker", "text", "status", "message", "delegation_id", "voice_session_id",
      "chat_session_id", "run_id", "provider", "model", "detail_type", "phase", "error", "code", "kind", "outcome", "wire_type",
      "start_ms", "end_ms", "offset_ms"];
    return Object.fromEntries(fields.filter(key => ["string", "number", "boolean"].includes(typeof value[key]))
      .map(key => [key, typeof value[key] === "string" ? value[key].slice(0, 4000) : value[key]]));
  };
  const verifySoftwareSink = (context, role) => {
    if (context.sinkId?.type !== "none") {
      record("harness.error", { message: "Browser did not honor the required software audio sink", role });
      void NativeContext.prototype.close.call(context);
      throw new Error("Voice E2E requires AudioContext sinkId: { type: 'none' }; no physical-device fallback");
    }
    record("audio.virtual-sink", { role, sinkType: context.sinkId.type });
  };
  const stopRecorder = async entry => {
    await entry.ready;
    if (!entry.stopPromise) entry.stopPromise = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        record("harness.error", { message: "Speaker recorder stop acknowledgement missing", contextId: entry.id });
        reject(new Error("Speaker recorder stop acknowledgement missing"));
      }, 3000);
      entry.acknowledgeStop = end => { clearTimeout(timeout); resolve(end); };
      entry.recorder.port.postMessage("stop");
    });
    return await entry.stopPromise;
  };
  globalThis.AudioContext = class extends NativeContext {
    constructor(options) {
      super({ ...options, sinkId: { type: "none" } });
      const context = this;
      verifySoftwareSink(context, "speaker");
      const entry = { context, id: contexts.length, recorder: undefined, ready: undefined,
        armed: false, armPromise: undefined, acknowledgeStart: undefined,
        stopPromise: undefined, acknowledgeStop: undefined };
      contexts.push(entry);
      const addModule = context.audioWorklet.addModule.bind(context.audioWorklet);
      entry.ready = addModule("/assets/voice-e2e-capture.js").then(() => {
        const node = new NativeWorklet(context, "ngn-voice-e2e-capture", {
          numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
        });
        entry.recorder = node;
        nativeConnect.call(node, context.destination);
        node.port.onmessage = ({ data }) => {
          if (data.type === "capture-stop") {
            const end = { contextId: entry.id, finalFrame: data.finalFrame, sampleRate: data.sampleRate, armed: data.armed };
            record("recorder.capture-stop", end);
            entry.acknowledgeStop?.(end);
            return;
          }
          if (data.type === "capture-start") {
            entry.armed = true;
            const start = { contextId: entry.id, frame: data.frame, sampleRate: data.sampleRate };
            record("recorder.capture-start", start);
            entry.acknowledgeStart?.(start);
            return;
          }
          if (data.type === "clock-discontinuity") {
            record("recorder.clock-discontinuity", { contextId: entry.id, fromFrame: data.fromFrame,
              toFrame: data.toFrame, sampleRate: data.sampleRate, phase: data.phase,
              contextTimeAtDelivery: context.currentTime });
            return;
          }
          if (stopped) return;
          const bytes = pcm(data.samples);
          capture.push({ contextId: entry.id, frame: data.frame, audioTime: data.frame / data.sampleRate,
            sampleRate: data.sampleRate, contextTimeAtDelivery: context.currentTime,
            outputTimestamp: context.getOutputTimestamp?.(), ...stamp(), ...stats(bytes), pcm16: base64(bytes) });
        };
        record("speaker.ready", { contextId: entry.id, sampleRate: context.sampleRate, sinkType: context.sinkId.type });
      }).catch(error => { record("harness.error", { message: String(error) }); throw error; });
      context.audioWorklet.addModule = async (...args) => {
        await entry.ready;
        return addModule(...args);
      };
      context.addEventListener("statechange", () => record("audio.state", {
        contextId: entry.id, state: context.state, audioTime: context.currentTime,
      }));
    }
    async setSinkId(sinkId) {
      if (sinkId?.type !== "none") {
        record("harness.error", { message: "Physical speaker selection is unavailable in a virtual audio test" });
        throw new Error("Voice E2E keeps its software speaker sink for the entire test");
      }
      await NativeContext.prototype.setSinkId.call(this, sinkId);
      verifySoftwareSink(this, "speaker");
    }
    async close() {
      const entry = contexts.find(item => item.context === this);
      try { if (entry) await stopRecorder(entry); }
      catch (error) {
        record("harness.error", { message: String(error), contextId: entry?.id });
        await super.close();
        throw error;
      }
      return await super.close();
    }
  };
  AudioNode.prototype.connect = function(destination, ...args) {
    const entry = contexts.find(item => item.context === this.context);
    if (entry && destination === this.context.destination) {
      if (!entry.recorder) throw new Error("Voice E2E recorder not ready before destination connection");
      // The real output graph feeds a virtual, silent speaker. Nothing is
      // scheduled or reconstructed from received network packets.
      nativeConnect.call(this, entry.recorder, ...args);
      return destination;
    }
    return nativeConnect.call(this, destination, ...args);
  };
  globalThis.AudioWorkletNode = class extends NativeWorklet {
    constructor(context, name, options) {
      super(context, name, options);
      record("worklet.created", { name, sampleRate: context.sampleRate });
      if (name === "ngn-live-playback") {
        this.port.addEventListener("message", ({ data }) => {
          if (!stopped && data?.type === "health" && health.length < limit * 4 + 1000) {
            const item = { ...stamp(), audioTime: context.currentTime,
              outputTimestamp: context.getOutputTimestamp?.(),
              contextId: contexts.find(entry => entry.context === context)?.id };
            for (const [key, value] of Object.entries(data))
              if (typeof value === "number" || key === "type") item[key] = value;
            health.push(item);
          }
        });
        this.port.start();
      }
      this.addEventListener("processorerror", () => record("worklet.error", { name }));
    }
  };
  navigator.mediaDevices.getUserMedia = async constraints => {
    if (!constraints?.audio || constraints.video) throw new Error("Voice E2E supports audio-only capture");
    if (!microphone) {
      const context = new NativeContext({ sampleRate: 24000, sinkId: { type: "none" } });
      verifySoftwareSink(context, "microphone");
      const resumed = context.resume();
      const destination = context.createMediaStreamDestination();
      const response = await nativeFetch(config.wavPath);
      if (!response.ok) throw new Error(`Voice fixture download failed (${response.status})`);
      const bytes = await response.arrayBuffer();
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      const hash = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
      if (hash !== config.fixtureSha256) throw new Error("Voice fixture SHA-256 mismatch");
      const buffer = await context.decodeAudioData(bytes);
      const keepAlive = context.createConstantSource();
      keepAlive.offset.value = 0;
      nativeConnect.call(keepAlive, destination);
      keepAlive.start();
      microphone = { context, destination, buffer, sources: [] };
      await resumed;
      record("microphone.ready", { sampleRate: context.sampleRate, fixtureDuration: buffer.duration,
        sinkType: context.sinkId.type,
        fixtureText: config.fixtureText, fixtureSha256: config.fixtureSha256 });
    }
    return microphone.destination.stream.clone();
  };
  globalThis.WebSocket = class extends NativeSocket {
    constructor(url, protocols) {
      super(url, protocols);
      const path = new URL(url, location.href).pathname;
      if (!/^\/api\/live\/sessions\/[^/]+\/audio$/.test(path)) return;
      record("socket.created", { path }); // Never retain token-bearing protocols.
      this.addEventListener("open", () => record("socket.open", { path }));
      this.addEventListener("close", event => record("socket.close", { path, code: event.code, clean: event.wasClean }));
      this.addEventListener("error", () => record("socket.error", { path }));
      this.addEventListener("message", ({ data }) => {
        if (data instanceof ArrayBuffer) {
          receivedSamples += data.byteLength / 2;
          record("socket.receive", { path, bytes: data.byteLength, receivedSamples, ...stats(new Uint8Array(data)) });
        }
        else if (typeof data === "string") record("socket.control", { path, text: data.slice(0, 256) });
      });
      const send = this.send.bind(this);
      this.send = data => {
        if (data instanceof ArrayBuffer && !stopped) {
          const bytes = new Uint8Array(data);
          const item = { ...stamp(), ...stats(bytes), bytes: bytes.length };
          record("socket.send", item);
          if (measurementStarted !== undefined) input.push({ ...item, pcm16: base64(bytes) });
        }
        send(data);
      };
    }
  };
  globalThis.fetch = async (...args) => {
    const raw = args[0] instanceof Request ? args[0].url : String(args[0]);
    const path = new URL(raw, location.href).pathname;
    const observed = path.startsWith("/api/live");
    const began = performance.now();
    try {
      const response = await nativeFetch(...args);
      if (observed) {
        record("http.response", { path, status: response.status, elapsedMs: performance.now() - began });
        void response.clone().json().then(body => {
          if (Array.isArray(body.events)) for (const event of body.events) {
            const detail = safeEvent(event);
            detail.eventType = detail.type;
            delete detail.type;
            record("live.event", detail);
          }
          if (typeof body.status === "string") record("live.status", { status: body.status, path });
          if (!response.ok) record("live.error", { path, status: response.status, detail: String(body.detail || "").slice(0, 1000) });
        }).catch(() => {});
      }
      return response;
    } catch (error) {
      if (observed) record("http.error", { path, message: String(error) });
      throw error;
    }
  };
  let previousTick = performance.now();
  const timer = setInterval(() => {
    const now = performance.now(), gap = now - previousTick;
    if (gap > 80) record("main-thread.gap", { gapMs: gap });
    previousTick = now;
    if (measurementStarted !== undefined && now - measurementStarted > limit * 1000 && !stopPromise) {
      record("measurement.limit", { elapsedMs: now - measurementStarted, maxSeconds: limit });
      void api.stop();
    }
  }, 20);
  addEventListener("error", event => record("window.error", { message: event.message }));
  addEventListener("unhandledrejection", event => record("window.rejection", { message: String(event.reason).slice(0, 1000) }));
  const visibilityChanged = () => record("page.visibility", { state: document.visibilityState });
  document.addEventListener("visibilitychange", visibilityChanged);
  visibilityChanged();
  let lastUI = "";
  const observeUI = () => {
    const selectors = [".voice-controls", ".voice-current-caption", ".voice-delegation", ".voice-feedback", ".voice-enable-audio"];
    const state = selectors.flatMap(selector => [...document.querySelectorAll(selector)].map(element => ({
      selector, text: element.textContent?.trim().slice(0, 4000) || "",
      phase: element.getAttribute("data-phase") || "", status: element.getAttribute("data-status") || "",
    })));
    const next = JSON.stringify(state);
    if (next !== lastUI) { lastUI = next; record("ui.voice", { state }); }
  };
  const observer = new MutationObserver(observeUI);
  observer.observe(document, { childList: true, subtree: true, characterData: true,
    attributes: true, attributeFilter: ["data-phase", "data-status"] });
  const api = {
    async play(delayMs = 250) {
      if (!microphone || stopped) throw new Error("Start voice first; microphone must be ready and harness active");
      await microphone.context.resume();
      const outputStarts = await Promise.all(contexts.map(async entry => {
        await entry.ready;
        if (!entry.armPromise) entry.armPromise = new Promise((resolve, reject) => {
          const timeout = setTimeout(() => reject(new Error("Speaker recorder did not acknowledge capture start")), 3000);
          entry.acknowledgeStart = start => { clearTimeout(timeout); resolve(start); };
          entry.recorder.port.postMessage("arm");
        });
        return await entry.armPromise;
      }));
      if (measurementStarted === undefined) {
        measurementStarted = performance.now();
        record("measurement.start", { maxSeconds: limit });
      }
      const source = microphone.context.createBufferSource();
      source.buffer = microphone.buffer;
      nativeConnect.call(source, microphone.destination);
      const audioTime = microphone.context.currentTime + delayMs / 1000;
      source.start(audioTime);
      microphone.sources.push(source);
      const round = microphone.sources.length;
      record("fixture.start", { round, audioTime, duration: source.buffer.duration, delayMs, outputStarts });
      source.addEventListener("ended", () => record("fixture.ended", { round, audioTime: microphone.context.currentTime }));
      return { audioTime, duration: source.buffer.duration, outputStarts };
    },
    mark(label) { record("operator.mark", { label: String(label).slice(0, 200) }); },
    stop() {
      if (stopPromise) return stopPromise;
      stopPromise = (async () => {
        await Promise.all(contexts.map(entry => stopRecorder(entry).catch(error => {
          record("harness.error", { message: String(error), contextId: entry.id });
        })));
        stopped = true;
        clearInterval(timer);
        observer.disconnect();
        document.removeEventListener("visibilitychange", visibilityChanged);
        for (const source of microphone?.sources || []) { try { source.stop(); } catch {} }
        await microphone?.context.close();
        record("harness.stopped");
      })();
      return stopPromise;
    },
    status() {
      return { mode: config.mode, stopped, microphone: !!microphone,
        measurementElapsedMs: measurementStarted === undefined ? 0 : performance.now() - measurementStarted,
        maxSeconds: limit,
        voiceClosed: events.some(event => event.type === "live.status" && event.status === "closed"),
        providerFinalized: events.some(event => `${event.text || ""}${event.message || ""}`.includes("Finalization confirmed.")),
        outputArmed: contexts.length > 0 && contexts.every(entry => entry.armed), outputBlocks: capture.length,
        inputFrames: input.length, events: events.length, health: health.at(-1), omitted };
    },
    export() {
      return { schema: "ngn-browser-voice-e2e/v1", mode: config.mode,
        environment: { userAgent: navigator.userAgent.slice(0, 512), visibilityState: document.visibilityState,
          viewport: { width: innerWidth, height: innerHeight, devicePixelRatio },
          appRevision: "Not exposed by the public bootstrap or DOM; retain server revision separately.",
          appScriptPaths: [...document.scripts].map(script => script.src).filter(Boolean)
            .map(src => new URL(src, location.href)).filter(url => url.origin === location.origin)
            .map(url => url.pathname.slice(0, 256)).slice(0, 8) },
        fixture: { text: config.fixtureText, sha256: config.fixtureSha256 },
        clock: { performanceTimeOriginMs: origin, installedMonotonicMs: installed,
          measurementStartedMonotonicMs: measurementStarted,
          note: "Audio frames are context-relative; callback wall/monotonic timestamps record delivery, not acoustic time." },
        contexts: contexts.map(({ context, id }) => ({ id, sampleRate: context.sampleRate, sinkType: context.sinkId?.type,
          state: context.state, currentTime: context.currentTime, outputTimestamp: context.getOutputTimestamp?.() })),
        stopped, omitted, events, health, input, output: capture };
    },
  };
  globalThis.__voiceE2E = api;
  // Only isolated test contexts should execute this script.
  localStorage.removeItem("ngn.live.audio-devices.v1");
  record("harness.installed", { mode: config.mode, maxSeconds: limit });
})();
