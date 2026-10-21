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
  const received = [];
  const events = [];
  const health = [];
  const seenLiveEvents = new Set();
  const limit = Math.max(30, Math.min(config.maxSeconds || 120, 900));
  const PCM_RATE = 24000;
  // Raw network PCM is private user audio. Cap this new copy separately and
  // preserve the existing 256 MB exported-JSON bound in cdp_runner.py.
  const receivedPcmByteLimit = Math.min(30_000_000, PCM_RATE * 2 * limit);
  const eventLimit = Math.ceil(limit * 240 + 6000);
  const origin = performance.timeOrigin;
  const installed = performance.now();
  let microphone;
  let stopped = false;
  let stopPromise;
  let measurementStarted;
  let measurementStartedAudioTime;
  let receivedSamples = 0;
  let transportReceivedPackets = 0;
  let measurementReceivedSamples = 0;
  let receivedSamplesCaptured = 0;
  let receivedBytesCaptured = 0;
  let receivedPacketCount = 0;
  let receivedOmittedBytes = 0;
  let receivedOmittedPackets = 0;
  let receivedLimitReported = false;
  let captureOrder = 0;
  const receivedByBuffer = new WeakMap();
  const submittedSamplesByContext = new Map();
  let omitted = 0;
  let currentUIState = [];
  let selectedSessionId = "";
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
    const fields = ["seq", "type", "id", "name", "tool", "call_id", "task_id", "activation", "parent_task_id", "session_id", "speaker", "text", "status", "message", "delegation_id", "voice_session_id",
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
  const safeWireRecord = (source, frame) => {
    if (!source || typeof source !== "object") return;
    const outer = source;
    const nested = outer.event === "task_activity" && outer.payload && typeof outer.payload === "object" ? outer.payload : outer;
    const eventType = nested.event || nested.type;
    if (!new Set(["run_started", "run_finished", "tool_call", "tool_execution_started", "tool_result",
      "task_started", "task_completed", "task_activity", "approval_closed"]).has(eventType)) return;
    const allowed = ["id", "name", "tool", "call_id", "task_id", "activation", "parent_task_id", "run_id", "status"];
    const detail = Object.fromEntries(allowed.filter(key => ["string", "number", "boolean"].includes(typeof nested[key]))
      .map(key => [key, typeof nested[key] === "string" ? nested[key].slice(0, 300) : nested[key]]));
    for (const key of ["task_id", "activation", "parent_task_id", "run_id"])
      if (detail[key] === undefined && ["string", "number", "boolean"].includes(typeof outer[key])) detail[key] = outer[key];
    record("live.event", {
      ...detail,
      eventType,
      call_id: detail.call_id || (String(eventType).startsWith("tool_") ? detail.id : ""),
      session_id: frame.session_id,
      eventCursor: frame.cursor,
    });
  };
  const safeEventFrame = frame => {
    if (!frame || typeof frame !== "object" || frame.session_id !== selectedSessionId) return;
    if (frame.type === "event") safeWireRecord(frame.record, frame);
    if (frame.type === "snapshot" && frame.snapshot && typeof frame.snapshot === "object") {
      const run = frame.snapshot.active_run;
      if (run && typeof run === "object") {
        const items = Array.isArray(run.events) ? run.events : Array.isArray(run.records) ? run.records : [];
        for (const item of items) safeWireRecord(item, frame);
      }
    }
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
      const entry = { context, id: contexts.length, recorder: undefined, ready: undefined, frameOrigin: undefined,
        samplesCaptured: 0, blocksCaptured: 0, schema: 2, playbackEpoch: 0, playbackRealtime: false,
        armed: false, armPromise: undefined,
        acknowledgeStart: undefined, stopPromise: undefined, acknowledgeStop: undefined };
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
            const end = { contextId: entry.id, schema: data.schema, finalFrame: data.finalFrame,
              observedFinalFrame: data.observedFinalFrame, frameOrigin: data.frameOrigin,
              samplesCaptured: data.samplesCaptured, blocksCaptured: data.blocksCaptured,
              sampleRate: data.sampleRate, armed: data.armed };
            record("recorder.capture-stop", end);
            entry.acknowledgeStop?.(end);
            return;
          }
          if (data.type === "capture-start") {
            entry.armed = true;
            entry.frameOrigin = data.frameOrigin;
            const start = { contextId: entry.id, schema: data.schema, frame: data.frame,
              frameOrigin: data.frameOrigin, observedFrame: data.observedFrame,
              sampleOffset: data.sampleOffset, sequence: data.sequence, sampleRate: data.sampleRate };
            record("recorder.capture-start", start);
            entry.acknowledgeStart?.(start);
            return;
          }
          if (data.type === "clock-discontinuity") {
            record("recorder.clock-discontinuity", { contextId: entry.id, schema: data.schema,
              fromFrame: data.fromFrame, toFrame: data.toFrame, observedFrame: data.observedFrame,
              capturedSamples: data.capturedSamples, sampleOffset: data.sampleOffset,
              sequence: data.sequence, sampleRate: data.sampleRate, phase: data.phase,
              contextTimeAtDelivery: context.currentTime });
            return;
          }
          if (stopped) return;
          const bytes = pcm(data.samples);
          entry.samplesCaptured = data.sampleOffset + data.samples.length;
          entry.blocksCaptured = data.sequence + 1;
          capture.push({ schema: data.schema, contextId: entry.id, frame: data.frame,
            frameOrigin: entry.frameOrigin, observedFrame: data.observedFrame,
            sampleOffset: data.sampleOffset, sequence: data.sequence,
            audioTime: data.frame / data.sampleRate, sampleRate: data.sampleRate,
            contextTimeAtDelivery: context.currentTime, outputTimestamp: context.getOutputTimestamp?.(),
            ...stamp(), ...stats(bytes), pcm16: base64(bytes) });
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
        const entry = contexts.find(item => item.context === context);
        if (entry) {
          const postMessage = this.port.postMessage.bind(this.port);
          this.port.postMessage = (message, transfer) => {
            if (message?.type === "pcm" && message.buffer instanceof ArrayBuffer) {
              const samples = message.buffer.byteLength / 2;
              const submittedSamples = (submittedSamplesByContext.get(entry.id) || 0) + samples;
              submittedSamplesByContext.set(entry.id, submittedSamples);
              const epoch = Number.isSafeInteger(message.epoch) ? message.epoch : entry.playbackEpoch;
              const receivedMeta = receivedByBuffer.get(message.buffer);
              record("playback.submit", {
                contextId: entry.id, epoch,
                samples, bytes: message.buffer.byteLength, submittedSamples, audioTime: context.currentTime,
                captureOrder: captureOrder++, ...(receivedMeta || {}),
              });
            }
            if (message && typeof message === "object" && ["reset", "barrier", "mode"].includes(message.type)) {
              if ((message.type === "reset" || message.type === "barrier") && Number.isSafeInteger(message.epoch))
                entry.playbackEpoch = message.epoch;
              if (message.type === "mode" && typeof message.realtime === "boolean")
                entry.playbackRealtime = message.realtime;
              record("playback.control", { contextId: entry.id, controlType: message.type,
                epoch: Number.isSafeInteger(message.epoch) ? message.epoch : entry.playbackEpoch,
                realtime: entry.playbackRealtime, audioTime: context.currentTime, captureOrder: captureOrder++ });
            }
            return postMessage(message, transfer);
          };
        }
        this.port.addEventListener("message", ({ data }) => {
          if (!stopped && data?.type === "played" && data.buffer instanceof ArrayBuffer) {
            const samples = data.buffer.byteLength / 2;
            const view = new DataView(data.buffer);
            let firstActiveSample = -1;
            let lastActiveSample = -1;
            let activeSamples = 0;
            const activeSpans = [];
            let spanStart = -1;
            for (let offset = 0; offset < samples; offset++) {
              const active = Math.abs(view.getInt16(offset * 2, true)) > 327;
              if (active) {
                if (firstActiveSample < 0) firstActiveSample = offset;
                lastActiveSample = offset;
                activeSamples++;
                if (spanStart < 0) spanStart = offset;
              } else if (spanStart >= 0) {
                activeSpans.push([spanStart, offset]);
                spanStart = -1;
              }
            }
            if (spanStart >= 0) activeSpans.push([spanStart, samples]);
            record("playback.rendered", {
              contextId: contexts.find(item => item.context === context)?.id,
              epoch: data.epoch,
              sourceStart: data.sourceStart,
              samples,
              sampleRate: 24000,
              contextSampleRate: context.sampleRate,
              at: data.at,
              until: data.until,
              firstActiveSample,
              lastActiveSample,
              activeSamples,
              activeSpans,
              outputTimestamp: context.getOutputTimestamp?.(),
              contextTimeAtDelivery: context.currentTime,
              captureOrder: captureOrder++,
            });
          }
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
      const fixtures = Array.isArray(config.rounds) && config.rounds.length ? config.rounds : [config];
      const buffers = [];
      for (const fixture of fixtures) {
        const response = await nativeFetch(fixture.wavPath || config.wavPath);
        if (!response.ok) throw new Error(`Voice fixture download failed (${response.status})`);
        const bytes = await response.arrayBuffer();
        const digest = await crypto.subtle.digest("SHA-256", bytes);
        const hash = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
        if (hash !== (fixture.fixtureSha256 || config.fixtureSha256)) throw new Error("Voice fixture SHA-256 mismatch");
        buffers.push(await context.decodeAudioData(bytes));
      }
      const keepAlive = context.createConstantSource();
      keepAlive.offset.value = 0;
      nativeConnect.call(keepAlive, destination);
      keepAlive.start();
      microphone = { context, destination, buffers, fixtures, sources: [] };
      await resumed;
      record("microphone.ready", { sampleRate: context.sampleRate, fixtureCount: buffers.length,
        fixtureDurations: buffers.map(buffer => buffer.duration), sinkType: context.sinkId.type });
    }
    return microphone.destination.stream.clone();
  };
  globalThis.WebSocket = class extends NativeSocket {
    constructor(url, protocols) {
      super(url, protocols);
      const path = new URL(url, location.href).pathname;
      const liveSocket = /^\/api\/live\/sessions\/[^/]+\/audio$/.test(path);
      const eventSocket = path === "/api/events";
      if (!liveSocket && !eventSocket) return;
      record("socket.created", { path }); // Never retain token-bearing protocols.
      this.addEventListener("open", () => record("socket.open", { path }));
      this.addEventListener("close", event => record("socket.close", { path, code: event.code, clean: event.wasClean }));
      this.addEventListener("error", () => record("socket.error", { path }));
      this.addEventListener("message", ({ data }) => {
        if (liveSocket && data instanceof ArrayBuffer) {
          const bytes = new Uint8Array(data);
          receivedSamples += data.byteLength / 2;
          const transportSequence = transportReceivedPackets++;
          const arrival = stamp();
          const captureOrderAtArrival = captureOrder++;
          receivedByBuffer.set(data, {
            receivedSequence: transportSequence, transportReceivedSamples: receivedSamples,
            receiveMonotonicMs: arrival.monotonicMs, receiveWallMs: arrival.wallMs,
          });
          const source = contexts.find(entry => entry.context.state === "running") || contexts.at(-1);
          record("socket.receive", { path, bytes: data.byteLength, receivedSamples, ...stats(bytes),
            ...arrival, captureOrder: captureOrderAtArrival });
          if (measurementStarted !== undefined && !stopped) {
            const samples = Math.floor(data.byteLength / 2);
            const sequence = receivedPacketCount++;
            const sampleOffset = measurementReceivedSamples;
            measurementReceivedSamples += samples;
            receivedByBuffer.set(data, { ...receivedByBuffer.get(data), receivedSamples: measurementReceivedSamples });
            const validPcm = data.byteLength > 0 && data.byteLength <= 48000 && !(data.byteLength % 2);
            const overLimit = receivedBytesCaptured + data.byteLength > receivedPcmByteLimit;
            if (!validPcm || overLimit) {
              receivedOmittedPackets++;
              receivedOmittedBytes += data.byteLength;
              omitted++;
              if (!receivedLimitReported) {
                record("harness.received-pcm-omitted", {
                  reason: validPcm ? "capture_limit" : "invalid_pcm_frame",
                  byteLimit: receivedPcmByteLimit,
                });
                receivedLimitReported = true;
              }
            } else {
              // WebSocket ArrayBuffers are transferred to the playback worklet
              // by the application listener. Snapshot before that transfer, but
              // defer base64 encoding until export so capture adds little work
              // on the UI thread during playback.
              received.push({ schema: 1, sequence, transportSequence, captureOrder: captureOrderAtArrival,
                sampleOffset, receivedSamples: measurementReceivedSamples,
                transportReceivedSamples: receivedSamples,
                samples, bytes: data.byteLength, sampleRate: PCM_RATE,
                contextId: source?.id ?? null, epoch: source?.playbackEpoch ?? 0,
                realtime: source?.playbackRealtime ?? false, audioTime: source?.context.currentTime ?? null,
                ...arrival, pcm: data.slice(0) });
              receivedBytesCaptured += data.byteLength;
              receivedSamplesCaptured += samples;
            }
          }
        }
        else if (liveSocket && typeof data === "string") record("socket.control", { path, text: data.slice(0, 256) });
        else if (eventSocket && typeof data === "string") {
          try { safeEventFrame(JSON.parse(data)); }
          catch { record("harness.error", { message: "Malformed app event frame; no frame content retained" }); }
        }
      });
      const send = this.send.bind(this);
      this.send = data => {
        if (eventSocket && typeof data === "string") {
          try {
            const request = JSON.parse(data);
            if (request.type === "subscribe" && typeof request.session_id === "string") selectedSessionId = request.session_id;
          } catch { /* Do not retain app socket payloads. */ }
        }
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
            const session = body.session_id || path.match(/^\/api\/live\/sessions\/([^/]+)/)?.[1];
            if (!session || !Number.isSafeInteger(event.seq)) {
              record("harness.error", { message: "Live event lacks a session or sequence identity" });
              continue;
            }
            const identity = `${session}:${event.seq}`;
            if (seenLiveEvents.has(identity)) continue;
            if (seenLiveEvents.size >= eventLimit) { omitted++; continue; }
            seenLiveEvents.add(identity);
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
    currentUIState = state;
    const next = JSON.stringify(state);
    if (next !== lastUI) { lastUI = next; record("ui.voice", { state }); }
  };
  const observer = new MutationObserver(observeUI);
  observer.observe(document, { childList: true, subtree: true, characterData: true,
    attributes: true, attributeFilter: ["data-phase", "data-status"] });
  const api = {
    async play(delayMs = 250, roundNumber = 1) {
      if (!microphone || stopped) throw new Error("Start voice first; microphone must be ready and harness active");
      if (!Number.isInteger(roundNumber) || roundNumber < 1 || roundNumber > microphone.buffers.length)
        throw new Error("Unknown spoken fixture round");
      const fixture = microphone.fixtures[roundNumber - 1];
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
        const source = contexts.find(entry => entry.context.state === "running") || contexts.at(-1);
        measurementStartedAudioTime = source?.context.currentTime ?? null;
        record("measurement.start", { maxSeconds: limit, contextId: source?.id ?? null,
          audioTime: measurementStartedAudioTime });
      }
      const source = microphone.context.createBufferSource();
      source.buffer = microphone.buffers[roundNumber - 1];
      nativeConnect.call(source, microphone.destination);
      const audioTime = microphone.context.currentTime + delayMs / 1000;
      source.start(audioTime);
      microphone.sources.push(source);
      record("fixture.start", { round: roundNumber, audioTime, duration: source.buffer.duration, delayMs, outputStarts,
        fixtureText: fixture.fixtureText || config.fixtureText || "" });
      source.addEventListener("ended", () => record("fixture.ended", { round: roundNumber, audioTime: microphone.context.currentTime }));
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
      const liveEvents = events.filter(event => event.type === "live.event");
      const transcripts = liveEvents.filter(event => event.eventType === "transcript").map(event => ({
        speaker: event.speaker || "", text: event.text || "", monotonicMs: event.monotonicMs,
      }));
      const delegations = liveEvents.filter(event => ["delegation", "task_started", "task_completed"].includes(event.eventType)).map(event => ({
        delegationId: event.delegation_id || "", taskId: event.task_id || "", runId: event.run_id || "",
        eventType: event.eventType, status: event.status || "", monotonicMs: event.monotonicMs,
      }));
      const toolCalls = liveEvents.filter(event => ["tool_call", "tool_execution_started", "tool_result"].includes(event.eventType)).map(event => ({
        eventType: event.eventType, callId: event.call_id || event.id || "", name: event.name || event.tool || "",
        runId: event.run_id || "", monotonicMs: event.monotonicMs,
      }));
      const audible = capture.filter(block => block.rms > 0.01).at(-1);
      return { mode: config.mode, stopped, microphone: !!microphone,
        measurementElapsedMs: measurementStarted === undefined ? 0 : performance.now() - measurementStarted,
        maxSeconds: limit,
        voiceClosed: events.some(event => event.type === "live.status" && event.status === "closed"),
        providerFinalized: events.some(event => `${event.text || ""}${event.message || ""}`.includes("Finalization confirmed.")),
        outputArmed: contexts.length > 0 && contexts.every(entry => entry.armed), outputBlocks: capture.length,
        inputFrames: input.length, events: events.length, health: health.at(-1), omitted,
        receivedPacketCount, receivedSamples: measurementReceivedSamples,
        receivedSamplesCaptured, receivedBytesCaptured, receivedOmittedBytes, receivedOmittedPackets,
        transcripts, delegations, toolCalls, subscribedSessionId: selectedSessionId,
        ui: currentUIState, lastAudibleOutputMs: audible?.monotonicMs || 0 };
    },
    export() {
      return { schema: "ngn-browser-voice-e2e/v2", mode: config.mode,
        environment: { userAgent: navigator.userAgent.slice(0, 512), visibilityState: document.visibilityState,
          viewport: { width: innerWidth, height: innerHeight, devicePixelRatio },
          appRevision: "Not exposed by the public bootstrap or DOM; retain server revision separately.",
          appScriptPaths: [...document.scripts].map(script => script.src).filter(Boolean)
            .map(src => new URL(src, location.href)).filter(url => url.origin === location.origin)
            .map(url => url.pathname.slice(0, 256)).slice(0, 8) },
        fixture: { text: config.fixtureText, sha256: config.fixtureSha256 },
        clock: { performanceTimeOriginMs: origin, installedMonotonicMs: installed,
          measurementStartedMonotonicMs: measurementStarted,
          measurementStartedAudioTime, receivePcmRate: PCM_RATE, receivePcmByteLimit: receivedPcmByteLimit,
          note: "Audio frames are context-relative; callback wall/monotonic timestamps record delivery, not acoustic time." },
        contexts: contexts.map(({ context, id, frameOrigin, samplesCaptured, blocksCaptured, schema }) => ({
          id, schema, frameOrigin, samplesCaptured, blocksCaptured,
          sampleRate: context.sampleRate, sinkType: context.sinkId?.type,
          state: context.state, currentTime: context.currentTime, outputTimestamp: context.getOutputTimestamp?.() })),
        stopped, omitted, events, health, input,
        received: received.map(({ pcm: bytes, ...packet }) => ({ ...packet, pcm16: base64(new Uint8Array(bytes)) })),
        receivedPcm: { sampleRate: PCM_RATE, sampleFormat: "s16le", packetCount: receivedPacketCount,
          capturedPackets: received.length, samples: measurementReceivedSamples, capturedSamples: receivedSamplesCaptured,
          capturedBytes: receivedBytesCaptured, omittedBytes: receivedOmittedBytes,
          omittedPackets: receivedOmittedPackets, byteLimit: receivedPcmByteLimit },
        output: capture };
    },
  };
  globalThis.__voiceE2E = api;
  // Only isolated test contexts should execute this script.
  localStorage.removeItem("ngn.live.audio-devices.v1");
  record("harness.installed", { mode: config.mode, maxSeconds: limit });
})();
