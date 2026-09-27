import type { LiveMedia, MediaHandlers } from "./types.js";

export function liveSupport(): string {
  if (!globalThis.isSecureContext) return "Open ngn serve on localhost or HTTPS to use your microphone.";
  if (!globalThis.navigator?.mediaDevices?.getUserMedia || typeof globalThis.AudioContext !== "function" || typeof globalThis.WebSocket !== "function")
    return "This browser does not support live audio. Try a current version of Chrome, Safari, Edge, or Firefox.";
  return "";
}

export function browserMedia(handlers: MediaHandlers): LiveMedia {
  let stream: MediaStream | undefined, context: AudioContext | undefined, capture: AudioWorkletNode | undefined, socket: WebSocket | undefined;
  let closed = false, outputMuted = false, playbackTime = 0;
  const playing = new Set<AudioBufferSourceNode>();

  const close = () => {
    if (closed) return;
    closed = true;
    stream?.getTracks().forEach((track) => track.stop());
    capture?.disconnect(); capture?.port.close();
    socket?.close();
    for (const source of playing) source.stop();
    playing.clear();
    if (context) void context.close();
  };

  const play = async () => {
    if (!context) return;
    try { await context.resume(); if (!closed) handlers.playbackBlocked(false); }
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
      for (const source of playing) source.stop();
      playing.clear(); playbackTime = 0;
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
      signal.addEventListener("abort", close, { once: true });
      // Resume during the explicit Start click, before an asynchronous permission
      // prompt consumes browser user activation. A blocked resume stays retryable.
      const audio = new AudioContext(); context = audio;
      void play();
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
        if (closed || signal.aborted) { stream.getTracks().forEach((track) => track.stop()); throw new DOMException("Cancelled", "AbortError"); }
        for (const track of stream.getAudioTracks())
          track.addEventListener("ended", () => { if (!closed) handlers.failed("Your microphone was disconnected. Check it, then reconnect."); });
        await audio.audioWorklet.addModule("/assets/live-capture.js");
        if (closed || signal.aborted) throw new DOMException("Cancelled", "AbortError");
        const source = audio.createMediaStreamSource(stream);
        const processor = new AudioWorkletNode(audio, "ngn-live-capture"); capture = processor;
        processor.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
          if (closed || !socket || socket.readyState !== WebSocket.OPEN) return;
          if (socket.bufferedAmount > 192_000) { handlers.failed("The audio relay is falling behind. Reconnect to try again."); return; }
          socket.send(event.data);
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
      await new Promise<void>((resolve, reject) => {
        channel.onopen = () => {
          if (!closed) {
            handlers.connected();
            if (context?.state !== "running") handlers.playbackBlocked(true);
            void play(); resolve();
          }
        };
        channel.onerror = () => reject(new Error("Could not connect to the ngn serve audio relay."));
        channel.onclose = () => {
          if (!closed) handlers.failed("The audio relay disconnected. Start a new conversation to reconnect.");
          reject(new Error("The audio relay disconnected."));
        };
      });
      channel.onmessage = (event: MessageEvent<ArrayBuffer>) => {
        if (!(event.data instanceof ArrayBuffer)) { handlers.failed("The audio relay sent an invalid frame."); return; }
        receive(event.data);
      };
    },
    muteInput(muted) { stream?.getAudioTracks().forEach((track) => { track.enabled = !muted; }); },
    muteOutput(muted) {
      outputMuted = muted;
      if (muted) { for (const source of playing) source.stop(); playing.clear(); playbackTime = 0; }
    },
    play, close,
  };
}
