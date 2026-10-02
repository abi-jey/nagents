import type { AudioDeviceSelection, LiveMedia, MediaHandlers } from "./types.js";
import { DEFAULT_AUDIO_DEVICES } from "./devices.js";
import { captureMicrophone, selectAudioOutput } from "./device-routing.js";

// Login voice carries media directly to GPT-Live. Provisioning, credentials,
// delegation and transcript ownership stay with ngn serve's sideband.
export function webrtcMedia(handlers: MediaHandlers, devices: AudioDeviceSelection = DEFAULT_AUDIO_DEVICES): LiveMedia {
  let stream: MediaStream | undefined, peer: RTCPeerConnection | undefined, channel: RTCDataChannel | undefined;
  let audio: HTMLAudioElement | undefined, abortSignal: AbortSignal | undefined;
  let closed = false, stopped = false, ready = false, connected = false, inputMuted = false, outputMuted = false, sdp = "";
  let disconnectTimer: ReturnType<typeof setTimeout> | undefined;

  const stop = () => {
    if (stopped) return;
    stopped = true; clearTimeout(disconnectTimer);
    stream?.getTracks().forEach((track) => { track.enabled = false; track.stop(); });
    if (audio) { audio.muted = true; audio.pause(); audio.srcObject = null; }
  };
  const close = () => {
    if (closed) return;
    closed = true; stop();
    abortSignal?.removeEventListener("abort", close);
    channel?.close(); peer?.close();
  };
  const play = async () => {
    if (stopped || !audio?.srcObject) return;
    try { await audio.play(); if (!stopped) handlers.playbackBlocked(false); }
    catch { if (!stopped) handlers.playbackBlocked(true); }
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
  };

  return {
    async prepare(signal) {
      signal.throwIfAborted(); abortSignal = signal;
      signal.addEventListener("abort", close, { once: true });
      try {
        audio = new Audio(); audio.autoplay = true;
        audio.onplaying = () => { if (!stopped) handlers.playbackBlocked(false); };
        audio.onpause = () => { if (!stopped && audio?.srcObject && !outputMuted) handlers.playbackBlocked(true); };
        audio.onerror = () => { if (!stopped) handlers.failed("GPT-Live audio could not be played. Reconnect to try again."); };
        stream = await captureMicrophone(devices.inputId);
        if (closed || signal.aborted) { stream.getTracks().forEach((track) => track.stop()); throw new DOMException("Cancelled", "AbortError"); }
        // Never send speech while provider creation and sideband attachment are
        // pending, including the time between applying the SDP and ICE startup.
        for (const track of stream.getAudioTracks()) {
          track.enabled = false;
          track.addEventListener("ended", () => { if (!stopped) handlers.failed("Your microphone was disconnected. Check it, then reconnect."); });
        }
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
          audio.srcObject = event.streams[0] || new MediaStream([event.track]);
          void play();
        };
        for (const track of stream.getAudioTracks()) connection.addTrack(track, stream);
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
    },
    muteOutput(muted) {
      outputMuted = muted;
      if (audio) audio.muted = stopped || muted;
      if (!muted) void play();
    },
    play, stop, close,
  };
}
