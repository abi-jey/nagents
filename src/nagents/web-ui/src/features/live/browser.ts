import type { AudioDeviceSelection, LiveMedia, LiveTransport, MediaHandlers } from "./types.js";
import { readAudioDevices } from "./devices.js";
import { captureMicrophone, DeviceChangeQueue, replaceMicrophone, selectAudioOutput, type AudioSink } from "./device-routing.js";
import { pcmPlayback, type PcmPlayback } from "./playback.js";
import { audioLevels, type AudioLevels } from "./audio-levels.js";
import { serverSocketUrl } from "../../api/origin.js";
import { createPcmAudio, type PcmAudio } from "../../components/voiceSphere/audioStreams.js";
import { silentSignal, type AudioFrame } from "../../components/voiceSphere/types.js";

export function liveSupport(transport: LiveTransport = "websocket"): string {
  if (transport !== "websocket") return "Update ngn serve to use the server voice relay.";
  if (!globalThis.isSecureContext) return "Open ngn serve on localhost or HTTPS to use your microphone.";
  const supported = typeof globalThis.AudioContext === "function" && typeof globalThis.AudioWorkletNode === "function" && typeof globalThis.WebSocket === "function";
  if (!globalThis.navigator?.mediaDevices?.getUserMedia || !supported)
    return "This browser does not support live audio. Try a current version of Chrome, Safari, Edge, or Firefox.";
  return "";
}

export function browserMedia(handlers: MediaHandlers, transport: LiveTransport = "websocket", devices: AudioDeviceSelection = readAudioDevices()): LiveMedia {
  if (transport !== "websocket") throw new Error("Voice must connect through the ngn server relay.");
  return relayMedia(handlers, devices);
}

function relayMedia(handlers: MediaHandlers, devices: AudioDeviceSelection): LiveMedia {
  let stream: MediaStream | undefined, context: AudioContext | undefined, capture: AudioWorkletNode | undefined, socket: WebSocket | undefined;
  let captureSource: MediaStreamAudioSourceNode | undefined;
  let meter: AudioLevels | undefined;
  let playback: PcmPlayback | undefined;
  let pcm: PcmAudio | undefined;
  let closed = false, inputMuted = false, outputMuted = false;
  let socketOpened = false, readyReported = false;
  let hasOutput = false;
  let switchingInput = false, outputId = devices.outputId;
  const inputs = new DeviceChangeQueue(), outputs = new DeviceChangeQueue();
  let abortSignal: AbortSignal | undefined, rejectConnection: ((cause: Error) => void) | undefined;

  // Visualization is optional: a queue/analysis failure must never terminate
  // microphone capture, scheduled playback, or the server-owned connection.
  const releasePcm = () => {
    const previous = pcm; pcm = undefined;
    try { previous?.dispose(); } catch { /* The audio transport remains usable. */ }
  };
  const withPcm = (operation: (audio: PcmAudio) => void) => {
    if (!pcm) return;
    try { operation(pcm); } catch { releasePcm(); }
  };
  const resetInput = () => withPcm((audio) => audio.input.reset());
  const resetOutput = () => withPcm((audio) => audio.output.reset());
  const sampleAudio = (): AudioFrame => {
    const silent = (): AudioFrame => ({ input: silentSignal(), output: silentSignal() });
    if (closed || context?.state !== "running" || !pcm || socket?.readyState !== WebSocket.OPEN) return silent();
    try {
      const frame = pcm.sample();
      return { input: inputMuted || switchingInput ? silentSignal() : frame.input,
        output: outputMuted || !playback ? silentSignal() : frame.output };
    } catch { releasePcm(); return silent(); }
  };

  const stopPlayback = () => { hasOutput = false; resetOutput(); playback?.reset(); };

  const close = () => {
    if (closed) return;
    closed = true;
    meter?.close();
    releasePcm();
    inputs.stop(); outputs.stop();
    abortSignal?.removeEventListener("abort", close);
    rejectConnection?.(new DOMException("Cancelled", "AbortError")); rejectConnection = undefined;
    stream?.getTracks().forEach((track) => track.stop());
    captureSource?.disconnect();
    capture?.disconnect(); capture?.port.close();
    socket?.close();
    stopPlayback(); playback?.close(); playback = undefined;
    if (context) { context.onstatechange = null; void context.close().catch(() => {}); }
  };

  const reportReady = () => {
    if (closed || readyReported || !socketOpened || socket?.readyState !== WebSocket.OPEN ||
        context?.state !== "running" || !capture || !captureSource ||
        !stream?.getAudioTracks().some(track => track.readyState === "live" && (inputMuted || track.enabled && !track.muted))) return;
    readyReported = true;
    handlers.connected();
  };

  const observeMicrophone = (source: MediaStream) => {
    for (const track of source.getAudioTracks()) track.addEventListener("unmute", reportReady);
    for (const track of source.getAudioTracks()) track.addEventListener("ended", () => {
      if (!closed && !switchingInput && stream === source) handlers.failed("Your microphone was disconnected. Check it, then reconnect.");
    });
  };

  const play = async () => {
    if (!context || closed) return;
    try { await context.resume(); if (!closed) { handlers.playbackBlocked(context.state !== "running"); reportReady(); } }
    catch { if (!closed) handlers.playbackBlocked(true); }
  };

  const receive = (data: ArrayBuffer) => {
    if (closed || !context || outputMuted) return;
    if (data.byteLength === 0 || data.byteLength > 48_000 || data.byteLength % 2) { handlers.failed("The audio relay sent an invalid frame."); return; }
    if (context.state !== "running") { handlers.playbackBlocked(true); return; }
    hasOutput = true; playback?.write(data);
  };

  return {
    async prepare(signal) {
      signal.throwIfAborted();
      abortSignal = signal;
      signal.addEventListener("abort", close, { once: true });
      // Resume during the explicit Start click, before an asynchronous permission
      // prompt consumes browser user activation. A blocked resume stays retryable.
      const audio = new AudioContext(); context = audio;
      try {
        const format = { encoding: "s16le" as const, sampleRate: 24_000, channels: 1 as const };
        pcm = createPcmAudio({ inputFormat: format, outputFormat: format, clock: () => audio.currentTime, maxBufferedSeconds: 2 });
      } catch { releasePcm(); }
      meter = audioLevels(audio, handlers.levels, () => [
        !closed && !inputMuted && socket?.readyState === WebSocket.OPEN,
        !closed && !outputMuted && hasOutput,
      ]);
      audio.onstatechange = () => {
        if (closed) return;
        // A backgrounded tab or device change can suspend an already-running
        // context. Discard stale speech and expose the resume action again.
        if (audio.state !== "running") { stopPlayback(); resetInput(); }
        meter?.sync();
        handlers.playbackBlocked(audio.state !== "running");
        reportReady();
      };
      void play();
      try {
        stream = await captureMicrophone(devices.inputId);
        if (closed || signal.aborted) { stream.getTracks().forEach((track) => track.stop()); throw new DOMException("Cancelled", "AbortError"); }
        observeMicrophone(stream);
        await selectAudioOutput(audio as AudioContext & AudioSink, devices.outputId, signal);
        if (closed || signal.aborted) throw new DOMException("Cancelled", "AbortError");
        await Promise.all([audio.audioWorklet.addModule("/assets/live-capture.js"), audio.audioWorklet.addModule("/assets/live-playback.js")]);
        if (closed || signal.aborted) throw new DOMException("Cancelled", "AbortError");
        playback = pcmPlayback(audio, (bytes, at) => {
          if (!closed && !outputMuted) withPcm(queue => queue.output.write(bytes, { at }));
        }, () => { if (!closed) handlers.failed("Speaker audio processing stopped. Reconnect to try again."); });
        meter?.output(playback.node);
        const source = audio.createMediaStreamSource(stream); captureSource = source;
        const processor = new AudioWorkletNode(audio, "ngn-live-capture"); capture = processor;
        processor.onprocessorerror = () => { if (!closed) handlers.failed("Microphone audio processing stopped. Reconnect to try again."); };
        processor.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
          if (closed || !socket || socket.readyState !== WebSocket.OPEN) return;
          if (socket.bufferedAmount > 192_000) { handlers.failed("The audio relay is falling behind. Reconnect to try again."); return; }
          // A worklet frame queued before a mute click must not leak after it.
          socket.send(inputMuted ? new ArrayBuffer(event.data.byteLength) : event.data);
          if (!inputMuted && !switchingInput && audio.state === "running") {
            const at = audio.currentTime - event.data.byteLength / (24_000 * 2);
            withPcm((queue) => queue.input.write(new Uint8Array(event.data), { at }));
          }
        };
        // Keep capture running without playing microphone audio locally.
        const silent = audio.createGain(); silent.gain.value = 0;
        source.connect(processor).connect(silent).connect(audio.destination);
        meter?.input(source);
      } catch (error) { close(); throw error; }
    },
    async connect(sessionId, token) {
      if (closed || !context) throw new DOMException("Cancelled", "AbortError");
      resetInput(); stopPlayback();
      const channel = new WebSocket(serverSocketUrl(`/api/live/sessions/${encodeURIComponent(sessionId)}/audio`), ["ngn.live.v2", `ngn.token.${token}`]);
      socket = channel; channel.binaryType = "arraybuffer";
      channel.onmessage = (event: MessageEvent<ArrayBuffer | string>) => {
        if (closed) return;
        if (typeof event.data === "string" && event.data === '{"type":"interrupt"}') { stopPlayback(); return; }
        if (!(event.data instanceof ArrayBuffer)) { handlers.failed("The audio relay sent an invalid frame."); return; }
        receive(event.data);
      };
      await new Promise<void>((resolve, reject) => {
        let opened = false;
        rejectConnection = reject;
        channel.onopen = () => {
          if (!closed) {
            opened = true; rejectConnection = undefined;
            socketOpened = true;
            reportReady();
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
    muteInput(muted) { inputMuted = muted; resetInput(); stream?.getAudioTracks().forEach((track) => { track.enabled = !muted; }); meter?.sync(); reportReady(); },
    muteOutput(muted) {
      outputMuted = muted;
      playback?.mute(muted);
      if (muted) stopPlayback();
      else resetOutput();
      meter?.sync();
    },
    setInputDevice(deviceId) {
      return inputs.run(async (signal) => {
        if (closed || !context || !capture || !captureSource || !stream) throw new DOMException("Voice is not active", "AbortError");
        const audio = context, processor = capture;
        switchingInput = true; resetInput();
        try {
          await replaceMicrophone(deviceId, signal, async (replacement) => {
            signal.throwIfAborted();
            const previous = stream!, previousSource = captureSource!;
            let source: MediaStreamAudioSourceNode;
            try { source = audio.createMediaStreamSource(replacement); }
            catch { throw new Error("Could not switch microphones. Your previous microphone is still selected. Try another microphone or System default."); }
            try { source.connect(processor); previousSource.disconnect(); }
            catch { source.disconnect(); throw new Error("Could not switch microphones. Your previous microphone is still selected. Try another microphone or System default."); }
            captureSource = source; stream = replacement; observeMicrophone(replacement);
            resetInput();
            meter?.input(source);
            for (const track of replacement.getAudioTracks()) track.enabled = !inputMuted;
            previous.getTracks().forEach((track) => track.stop());
          });
        } finally {
          switchingInput = false;
          if (!closed && stream?.getAudioTracks().every((track) => track.readyState === "ended"))
            handlers.failed("Your microphone was disconnected. Check it, then reconnect.");
        }
      });
    },
    setOutputDevice(deviceId) {
      return outputs.run(async (signal) => {
        if (closed || !context) throw new DOMException("Voice is not active", "AbortError");
        if (deviceId === outputId) return;
        await selectAudioOutput(context as AudioContext & AudioSink, deviceId, signal, true);
        signal.throwIfAborted(); outputId = deviceId; playback?.fenceReports(); resetOutput();
      });
    },
    play, close, sampleAudio,
    audioHealth: () => playback?.health() || { receivedSamples: 0, playedSamples: 0, underruns: 0, droppedSamples: 0,
      bufferedMs: 0, peakBufferedMs: 0, targetMs: 40, sampleRate: context?.sampleRate || 0, correctionPpm: 0 },
  };
}
