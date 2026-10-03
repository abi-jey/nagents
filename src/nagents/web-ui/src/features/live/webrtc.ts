import type { AudioDeviceSelection, LiveMedia, MediaHandlers } from "./types.js";
import { DEFAULT_AUDIO_DEVICES } from "./devices.js";
import { captureMicrophone, DeviceChangeQueue, duringDeviceChange, replaceMicrophone, selectAudioOutput } from "./device-routing.js";
import { audioLevels, type AudioLevels } from "./audio-levels.js";

// Login voice carries media directly to GPT-Live. Provisioning, credentials,
// delegation and transcript ownership stay with ngn serve's sideband.
export function webrtcMedia(handlers: MediaHandlers, devices: AudioDeviceSelection = DEFAULT_AUDIO_DEVICES): LiveMedia {
  let stream: MediaStream | undefined, peer: RTCPeerConnection | undefined, channel: RTCDataChannel | undefined;
  let audio: HTMLAudioElement | undefined, abortSignal: AbortSignal | undefined;
  let sender: RTCRtpSender | undefined;
  let meterContext: AudioContext | undefined, meter: AudioLevels | undefined, releaseOutputMeter: (() => void) | undefined;
  let playbackActive = false;
  let closed = false, stopped = false, ready = false, connected = false, inputMuted = false, outputMuted = false, sdp = "";
  let switchingInput = false, outputId = devices.outputId;
  const inputs = new DeviceChangeQueue(), outputs = new DeviceChangeQueue();
  let disconnectTimer: ReturnType<typeof setTimeout> | undefined;

  const stop = () => {
    if (stopped) return;
    stopped = true; clearTimeout(disconnectTimer);
    meter?.close();
    if (meterContext) void meterContext.close().catch(() => {});
    inputs.stop(); outputs.stop();
    stream?.getTracks().forEach((track) => { track.enabled = false; track.stop(); });
    if (audio) { audio.muted = true; audio.pause(); audio.srcObject = null; }
  };
  const observeMicrophone = (source: MediaStream) => {
    for (const track of source.getAudioTracks()) track.addEventListener("ended", () => {
      if (!stopped && !switchingInput && stream === source) handlers.failed("Your microphone was disconnected. Check it, then reconnect.");
    });
  };
  const close = () => {
    if (closed) return;
    closed = true; stop();
    abortSignal?.removeEventListener("abort", close);
    channel?.close(); peer?.close();
  };
  const play = async () => {
    if (stopped || !audio?.srcObject) return;
    if (meterContext) void meterContext.resume().catch(() => {});
    try { await audio.play(); if (!stopped) handlers.playbackBlocked(false); }
    catch { playbackActive = false; meter?.sync(); if (!stopped) handlers.playbackBlocked(true); }
  };
  const meterInput = (source: MediaStream) => {
    if (!meter || !meterContext) return;
    try { meter.input(meterContext.createMediaStreamSource(source)); }
    catch { meter.input(); }
  };
  const observeConnection = () => {
    if (stopped || !peer) return;
    if (peer.connectionState === "connected") {
      clearTimeout(disconnectTimer); disconnectTimer = undefined;
      if (ready && !connected) {
        connected = true;
        stream?.getAudioTracks().forEach((track) => { track.enabled = !inputMuted; });
        handlers.connected();
      }
    } else if (peer.connectionState === "failed") handlers.failed("The GPT-Live audio connection failed. Check your network, then reconnect.");
    else if (peer.connectionState === "closed") handlers.ended();
    else if (peer.connectionState === "disconnected" && !disconnectTimer) {
      // ICE may briefly disconnect during a network change. Keep a bounded
      // recovery window while the server heartbeat continues independently.
      disconnectTimer = setTimeout(() => {
        disconnectTimer = undefined;
        if (!stopped && peer?.connectionState === "disconnected") handlers.failed("The GPT-Live audio connection was interrupted. Reconnect to try again.");
      }, 5_000);
    }
    meter?.sync();
  };

  return {
    async prepare(signal) {
      signal.throwIfAborted(); abortSignal = signal;
      signal.addEventListener("abort", close, { once: true });
      try {
        if (handlers.levels && typeof globalThis.AudioContext === "function") {
          try {
            const context = new AudioContext(); meterContext = context;
            meter = audioLevels(context, handlers.levels, () => [
              !stopped && connected && peer?.connectionState === "connected" && !inputMuted,
              !stopped && connected && playbackActive && !outputMuted,
            ]);
            if (meter) void context.resume().catch(() => {});
            else { void context.close().catch(() => {}); meterContext = undefined; }
          } catch { /* Audio playback does not depend on optional level analysis. */ }
        }
        audio = new Audio(); audio.autoplay = true;
        audio.onplaying = () => { if (!stopped) { playbackActive = true; handlers.playbackBlocked(false); } };
        audio.onpause = () => { playbackActive = false; meter?.sync(); if (!stopped && audio?.srcObject && !outputMuted) handlers.playbackBlocked(true); };
        audio.onerror = () => { if (!stopped) handlers.failed("GPT-Live audio could not be played. Reconnect to try again."); };
        stream = await captureMicrophone(devices.inputId);
        if (closed || signal.aborted) { stream.getTracks().forEach((track) => track.stop()); throw new DOMException("Cancelled", "AbortError"); }
        // Never send speech while provider creation and sideband attachment are
        // pending, including the time between applying the SDP and ICE startup.
        for (const track of stream.getAudioTracks()) {
          track.enabled = false;
        }
        observeMicrophone(stream);
        meterInput(stream);
        await selectAudioOutput(audio, devices.outputId, signal);
        if (closed || signal.aborted) throw new DOMException("Cancelled", "AbortError");
        const connection = new RTCPeerConnection(); peer = connection;
        channel = connection.createDataChannel("oai-events");
        channel.onclose = () => { if (!stopped) handlers.ended(); };
        channel.onerror = () => { if (!stopped) handlers.failed("The GPT-Live control connection failed. Reconnect to try again."); };
        // Polling the authenticated server remains the single transcript and
        // delegation source. The browser never turns data-channel events into tasks.
        connection.onconnectionstatechange = observeConnection;
        connection.ontrack = (event) => {
          if (stopped || !audio) return;
          playbackActive = false; meter?.sync();
          const remote = event.streams[0] || new MediaStream([event.track]);
          audio.srcObject = remote;
          releaseOutputMeter?.();
          if (meter && meterContext) {
            try { releaseOutputMeter = meter.output(meterContext.createMediaStreamSource(remote)); }
            catch { releaseOutputMeter = undefined; }
          }
          void play();
        };
        const track = stream.getAudioTracks()[0];
        if (!track) throw new Error("The selected microphone did not provide audio. Choose another microphone or System default.");
        sender = connection.addTrack(track, stream);
        const offer = await connection.createOffer();
        if (closed || signal.aborted) throw new DOMException("Cancelled", "AbortError");
        if (!offer.sdp) throw new Error("The browser could not prepare a GPT-Live audio connection.");
        await connection.setLocalDescription(offer);
        if (closed || signal.aborted) throw new DOMException("Cancelled", "AbortError");
        // GPT-Live accepts the immediate SDP offer; ICE proceeds after its answer.
        sdp = offer.sdp;
      } catch (cause) { close(); throw cause; }
    },
    offer() { return sdp; },
    async connect(_sessionId, _token, answer) {
      if (stopped || !peer) throw new DOMException("Cancelled", "AbortError");
      if (!answer) throw new Error("GPT-Live did not return an audio connection. Reconnect to try again.");
      await peer.setRemoteDescription({ type: "answer", sdp: answer });
      if (stopped) throw new DOMException("Cancelled", "AbortError");
      ready = true; observeConnection();
    },
    muteInput(muted) {
      inputMuted = muted;
      stream?.getAudioTracks().forEach((track) => { track.enabled = !stopped && connected && !muted; });
      meter?.sync();
    },
    muteOutput(muted) {
      outputMuted = muted;
      if (audio) audio.muted = stopped || muted;
      meter?.sync();
      if (!muted) void play();
    },
    setInputDevice(deviceId) {
      return inputs.run(async (signal) => {
        if (stopped || !sender || !stream) throw new DOMException("Voice is not active", "AbortError");
        const currentSender = sender;
        switchingInput = true;
        try {
          await replaceMicrophone(deviceId, signal, async (replacement) => {
            const previous = stream!, track = replacement.getAudioTracks()[0];
            try { await duringDeviceChange(currentSender.replaceTrack(track), signal); }
            catch {
              signal.throwIfAborted();
              throw new Error("Could not switch microphones. Your previous microphone is still selected. Try another microphone or System default.");
            }
            signal.throwIfAborted();
            if (track.readyState === "ended") {
              try { await duringDeviceChange(currentSender.replaceTrack(previous.getAudioTracks()[0]), signal); }
              catch {
                signal.throwIfAborted();
                handlers.failed("The microphone connection could not be restored. Reconnect to try again.");
              }
              throw new Error("The selected microphone disconnected. Choose another microphone or System default.");
            }
            stream = replacement; observeMicrophone(replacement);
            meterInput(replacement);
            track.enabled = connected && !inputMuted;
            previous.getTracks().forEach((old) => old.stop());
          });
        } finally {
          switchingInput = false;
          if (!stopped && stream?.getAudioTracks().every((track) => track.readyState === "ended"))
            handlers.failed("Your microphone was disconnected. Check it, then reconnect.");
        }
      });
    },
    setOutputDevice(deviceId) {
      return outputs.run(async (signal) => {
        if (stopped || !audio) throw new DOMException("Voice is not active", "AbortError");
        if (deviceId === outputId) return;
        await selectAudioOutput(audio, deviceId, signal, true);
        signal.throwIfAborted(); outputId = deviceId;
      });
    },
    play, stop, close,
  };
}
