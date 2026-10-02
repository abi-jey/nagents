import type { AudioDeviceSelection, LiveMedia, LiveTransport, MediaHandlers } from "./types.js";
import { readAudioDevices } from "./devices.js";
import { captureMicrophone, selectAudioOutput, type AudioSink } from "./device-routing.js";
import { webrtcMedia } from "./webrtc.js";

export function liveSupport(transport: LiveTransport = "websocket"): string {
  if (!globalThis.isSecureContext) return "Open ngn serve on localhost or HTTPS to use your microphone.";
  const supported = transport === "webrtc" ? typeof globalThis.RTCPeerConnection === "function"
    : typeof globalThis.AudioContext === "function" && typeof globalThis.AudioWorkletNode === "function" && typeof globalThis.WebSocket === "function";
  if (!globalThis.navigator?.mediaDevices?.getUserMedia || !supported)
    return "This browser does not support live audio. Try a current version of Chrome, Safari, Edge, or Firefox.";
  return "";
}

export function browserMedia(handlers: MediaHandlers, transport: LiveTransport = "websocket", devices: AudioDeviceSelection = readAudioDevices()): LiveMedia {
  return transport === "webrtc" ? webrtcMedia(handlers, devices) : relayMedia(handlers, devices);
}

function relayMedia(handlers: MediaHandlers, devices: AudioDeviceSelection): LiveMedia {
  let stream: MediaStream | undefined, context: AudioContext | undefined, capture: AudioWorkletNode | undefined, socket: WebSocket | undefined;
  let closed = false, inputMuted = false, outputMuted = false, playbackTime = 0;
  let abortSignal: AbortSignal | undefined, rejectConnection: ((cause: Error) => void) | undefined;
  const playing = new Set<AudioBufferSourceNode>();

  const stopPlayback = () => {
    for (const source of playing) { source.stop(); source.disconnect(); }
    playing.clear(); playbackTime = 0;
  };

  const close = () => {
    if (closed) return;
    closed = true;
    abortSignal?.removeEventListener("abort", close);
    rejectConnection?.(new DOMException("Cancelled", "AbortError")); rejectConnection = undefined;
    stream?.getTracks().forEach((track) => track.stop());
    capture?.disconnect(); capture?.port.close();
    socket?.close();
    stopPlayback();
    if (context) { context.onstatechange = null; void context.close().catch(() => {}); }
  };

  const play = async () => {
    if (!context || closed) return;
    try { await context.resume(); if (!closed) handlers.playbackBlocked(context.state !== "running"); }
    catch { if (!closed) handlers.playbackBlocked(true); }
  };

  const receive = (data: ArrayBuffer) => {
    if (closed || !context || outputMuted) return;
    if (data.byteLength === 0 || data.byteLength > 48_000 || data.byteLength % 2) { handlers.failed("The audio relay sent an invalid frame."); return; }
    const samples = new DataView(data), buffer = context.createBuffer(1, data.byteLength / 2, 24_000);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < channel.length; i++) channel[i] = samples.getInt16(i * 2, true) / 32768;
    // Avoid building a long delayed playback queue if the browser falls behind.
    if (playbackTime > context.currentTime + 0.6) {
      stopPlayback();
    }
    const source = context.createBufferSource();
    source.buffer = buffer; source.connect(context.destination);
    source.onended = () => { playing.delete(source); source.disconnect(); };
    playing.add(source);
    const start = Math.max(context.currentTime + 0.02, playbackTime);
    source.start(start);
    playbackTime = start + buffer.duration;
    if (context.state !== "running") handlers.playbackBlocked(true);
  };

  return {
    async prepare(signal) {
      signal.throwIfAborted();
      abortSignal = signal;
      signal.addEventListener("abort", close, { once: true });
      // Resume during the explicit Start click, before an asynchronous permission
      // prompt consumes browser user activation. A blocked resume stays retryable.
      const audio = new AudioContext(); context = audio;
      audio.onstatechange = () => {
        if (closed) return;
        // A backgrounded tab or device change can suspend an already-running
        // context. Discard stale speech and expose the resume action again.
        if (audio.state !== "running") stopPlayback();
        handlers.playbackBlocked(audio.state !== "running");
      };
      void play();
      try {
        stream = await captureMicrophone(devices.inputId);
        if (closed || signal.aborted) { stream.getTracks().forEach((track) => track.stop()); throw new DOMException("Cancelled", "AbortError"); }
        for (const track of stream.getAudioTracks())
          track.addEventListener("ended", () => { if (!closed) handlers.failed("Your microphone was disconnected. Check it, then reconnect."); });
        await selectAudioOutput(audio as AudioContext & AudioSink, devices.outputId, signal);
        if (closed || signal.aborted) throw new DOMException("Cancelled", "AbortError");
        await audio.audioWorklet.addModule("/assets/live-capture.js");
        if (closed || signal.aborted) throw new DOMException("Cancelled", "AbortError");
        const source = audio.createMediaStreamSource(stream);
        const processor = new AudioWorkletNode(audio, "ngn-live-capture"); capture = processor;
        processor.onprocessorerror = () => { if (!closed) handlers.failed("Microphone audio processing stopped. Reconnect to try again."); };
        processor.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
          if (closed || !socket || socket.readyState !== WebSocket.OPEN) return;
          if (socket.bufferedAmount > 192_000) { handlers.failed("The audio relay is falling behind. Reconnect to try again."); return; }
          // A worklet frame queued before a mute click must not leak after it.
          socket.send(inputMuted ? new ArrayBuffer(event.data.byteLength) : event.data);
        };
        // Keep capture running without playing microphone audio locally.
        const silent = audio.createGain(); silent.gain.value = 0;
        source.connect(processor).connect(silent).connect(audio.destination);
      } catch (error) { close(); throw error; }
    },
    async connect(sessionId, token) {
      if (closed || !context) throw new DOMException("Cancelled", "AbortError");
      const scheme = location.protocol === "https:" ? "wss:" : "ws:";
      const channel = new WebSocket(`${scheme}//${location.host}/api/live/sessions/${encodeURIComponent(sessionId)}/audio`, ["ngn.live.v1", `ngn.token.${token}`]);
      socket = channel; channel.binaryType = "arraybuffer";
      channel.onmessage = (event: MessageEvent<ArrayBuffer>) => {
        if (closed) return;
        if (!(event.data instanceof ArrayBuffer)) { handlers.failed("The audio relay sent an invalid frame."); return; }
        receive(event.data);
      };
      await new Promise<void>((resolve, reject) => {
        let opened = false;
        rejectConnection = reject;
        channel.onopen = () => {
          if (!closed) {
            opened = true; rejectConnection = undefined;
            handlers.connected();
            if (context?.state !== "running") handlers.playbackBlocked(true);
            void play(); resolve();
          }
        };
        channel.onerror = () => {
          if (!closed && opened) handlers.failed("The audio relay connection failed. Reconnect to try again.");
          reject(new Error("Could not connect to the ngn serve audio relay."));
        };
        channel.onclose = (event) => {
          rejectConnection = undefined;
          if (!closed && opened) {
            // The server closes this socket after finalization, before the next
            // caption poll may arrive. Let its final snapshot decide the result.
            if (event.code === 1000) handlers.ended();
            else handlers.failed("The audio relay disconnected. Start a new conversation to reconnect.");
          }
          reject(new Error("The audio relay disconnected."));
        };
      });
    },
    muteInput(muted) { inputMuted = muted; stream?.getAudioTracks().forEach((track) => { track.enabled = !muted; }); },
    muteOutput(muted) {
      outputMuted = muted;
      if (muted) stopPlayback();
    },
    play, close,
  };
}
