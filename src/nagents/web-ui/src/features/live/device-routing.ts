// An optional method also covers AudioContext.setSinkId, which is newer than
// the DOM declarations shipped by some TypeScript versions.
export interface AudioSink {
  setSinkId?(deviceId: string): Promise<void>;
}

export class DeviceChangeQueue {
  private readonly abort = new AbortController();
  private pending = Promise.resolve();
  get signal(): AbortSignal { return this.abort.signal; }
  run(change: (signal: AbortSignal) => Promise<void>): Promise<void> {
    const next = this.pending.then(() => { this.signal.throwIfAborted(); return change(this.signal); });
    this.pending = next.catch(() => {});
    return next;
  }
  stop() { this.abort.abort(); }
}

export async function duringDeviceChange<T>(operation: Promise<T>, signal: AbortSignal): Promise<T> {
  let cancel = () => {};
  const cancelled = new Promise<never>((_resolve, reject) => {
    cancel = () => reject(signal.reason);
    signal.addEventListener("abort", cancel, { once: true });
    if (signal.aborted) cancel();
  });
  try { return await Promise.race([operation, cancelled]); }
  finally { signal.removeEventListener("abort", cancel); }
}

export async function replaceMicrophone(inputId: string, signal: AbortSignal, apply: (stream: MediaStream) => Promise<void>): Promise<void> {
  signal.throwIfAborted();
  let candidate: MediaStream | undefined;
  // getUserMedia itself cannot be cancelled. Own a late permission result even
  // after the caller's promise has already rejected on stop/close.
  const incoming = captureMicrophone(inputId).then((stream) => {
    for (const track of stream.getAudioTracks()) track.enabled = false;
    if (signal.aborted) { stream.getTracks().forEach((track) => track.stop()); signal.throwIfAborted(); }
    candidate = stream;
    return stream;
  });
  try {
    const stream = await duringDeviceChange(incoming, signal);
    signal.throwIfAborted();
    if (!stream.getAudioTracks().length || stream.getAudioTracks().some((track) => track.readyState === "ended"))
      throw new Error("The selected microphone disconnected. Choose another microphone or System default.");
    await apply(stream);
    candidate = undefined;
  } finally { candidate?.getTracks().forEach((track) => track.stop()); }
}

export async function captureMicrophone(inputId: string): Promise<MediaStream> {
  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      ...(inputId ? { deviceId: { exact: inputId } } : {}),
    } });
  } catch (cause) {
    if (!inputId || cause instanceof Error && cause.name === "AbortError") throw cause;
    throw new Error("The selected microphone is unavailable or permission was denied. Choose another microphone or System default in Audio devices, then try again.");
  }
  const tracks = stream.getAudioTracks();
  if (!tracks.length || tracks.some(track => track.readyState === "ended")) {
    stream.getTracks().forEach(track => track.stop());
    throw new Error("The selected microphone did not provide live audio. Choose another microphone or System default in Audio devices, then try again.");
  }
  return stream;
}

export async function selectAudioOutput(target: AudioSink, outputId: string, signal: AbortSignal, resetDefault = false): Promise<void> {
  // A fresh player already follows the system default. Do not pin it to the
  // device that happens to be the default when the conversation starts.
  if (!outputId && !resetDefault) return;
  if (typeof target.setSinkId !== "function")
    throw new Error("This browser cannot use the selected speaker for this voice connection. Choose System default in Audio devices, then try again.");
  signal.throwIfAborted();
  try { await duringDeviceChange(target.setSinkId(outputId), signal); signal.throwIfAborted(); }
  catch {
    signal.throwIfAborted();
    throw new Error("The selected speaker is unavailable or permission was denied. Choose another speaker or System default in Audio devices, then try again.");
  }
}
