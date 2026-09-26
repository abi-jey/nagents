import type { LiveMedia, MediaHandlers } from "./types.js";

export function liveSupport(): string {
  if (!globalThis.isSecureContext) return "Open ngn serve on localhost or HTTPS to use your microphone.";
  if (!globalThis.navigator?.mediaDevices?.getUserMedia || typeof globalThis.RTCPeerConnection !== "function")
    return "This browser does not support live audio. Try a current version of Chrome, Safari, Edge, or Firefox.";
  return "";
}

export function browserMedia(handlers: MediaHandlers): LiveMedia {
  let stream: MediaStream | undefined, peer: RTCPeerConnection | undefined, channel: RTCDataChannel | undefined;
  let closed = false;
  let disconnectTimer: ReturnType<typeof setTimeout> | undefined;
  const audio = new Audio();
  audio.autoplay = true;
  const play = async () => {
    try { await audio.play(); if (!closed) handlers.playbackBlocked(false); }
    catch { if (!closed) handlers.playbackBlocked(true); }
  };
  const close = () => {
    if (closed) return;
    closed = true; clearTimeout(disconnectTimer);
    stream?.getTracks().forEach((track) => track.stop());
    channel?.close(); peer?.close();
    audio.pause(); audio.srcObject = null;
  };
  return {
    async offer(signal) {
      signal.throwIfAborted();
      signal.addEventListener("abort", close, { once: true });
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      // Permission prompts cannot be cancelled. Stop a late-granted stream too.
      if (closed || signal.aborted) { stream.getTracks().forEach((track) => track.stop()); throw new DOMException("Cancelled", "AbortError"); }
      const connection = new RTCPeerConnection(); peer = connection;
      for (const track of stream.getTracks()) {
        connection.addTrack(track, stream);
        track.addEventListener("ended", () => { if (!closed) handlers.failed("Your microphone was disconnected. Check it, then reconnect."); });
      }
      connection.ontrack = (event) => {
        if (closed) return;
        audio.srcObject = new MediaStream([event.track]); void play();
      };
      connection.onconnectionstatechange = () => {
        if (closed) return;
        clearTimeout(disconnectTimer);
        if (connection.connectionState === "connected") handlers.connected();
        else if (connection.connectionState === "failed" || connection.connectionState === "closed") handlers.failed("The audio connection was lost. Start a new conversation to reconnect.");
        else if (connection.connectionState === "disconnected") disconnectTimer = setTimeout(() => {
          if (!closed) handlers.failed("The audio connection was interrupted. Check your network and reconnect.");
        }, 8000);
      };
      // The session already starts through the HTTP WebRTC handshake. This
      // channel is negotiated for Live events; never send session.start here.
      channel = connection.createDataChannel("oai-events");
      await connection.setLocalDescription(await connection.createOffer());
      if (connection.iceGatheringState !== "complete") await new Promise<void>((resolve, reject) => {
        const finish = (error?: Error) => {
          clearTimeout(timer); connection.removeEventListener("icegatheringstatechange", changed);
          signal.removeEventListener("abort", aborted);
          if (error) reject(error); else resolve();
        };
        const changed = () => { if (connection.iceGatheringState === "complete") finish(); };
        const aborted = () => finish(new DOMException("Cancelled", "AbortError"));
        const timer = setTimeout(() => finish(new Error("Could not prepare the audio connection. Check your network and try again.")), 10_000);
        connection.addEventListener("icegatheringstatechange", changed);
        signal.addEventListener("abort", aborted, { once: true });
        if (signal.aborted) aborted(); else changed();
      });
      signal.throwIfAborted();
      const sdp = connection.localDescription?.sdp;
      if (!sdp) throw new Error("The browser could not create an audio connection.");
      return sdp;
    },
    async answer(sdp) { if (!closed && peer) await peer.setRemoteDescription({ type: "answer", sdp }); },
    muteInput(muted) { stream?.getAudioTracks().forEach((track) => { track.enabled = !muted; }); },
    muteOutput(muted) { audio.muted = muted; },
    play,
    close,
  };
}
