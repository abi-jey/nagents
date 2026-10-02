// An optional method also covers AudioContext.setSinkId, which is newer than
// the DOM declarations shipped by some TypeScript versions.
export interface AudioSink {
  setSinkId?(deviceId: string): Promise<void>;
}

export async function captureMicrophone(inputId: string): Promise<MediaStream> {
  try {
    return await navigator.mediaDevices.getUserMedia({ audio: {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      ...(inputId ? { deviceId: { exact: inputId } } : {}),
    } });
  } catch (cause) {
    if (!inputId || cause instanceof Error && cause.name === "AbortError") throw cause;
    throw new Error("The selected microphone is unavailable or permission was denied. Choose another microphone or System default in Audio devices, then try again.");
  }
}

export async function selectAudioOutput(target: AudioSink, outputId: string, signal: AbortSignal): Promise<void> {
  // A fresh player already follows the system default. Do not pin it to the
  // device that happens to be the default when the conversation starts.
  if (!outputId) return;
  if (typeof target.setSinkId !== "function")
    throw new Error("This browser cannot use the selected speaker for this voice connection. Choose System default in Audio devices, then try again.");
  let cancel = () => {};
  const cancelled = new Promise<never>((_resolve, reject) => {
    cancel = () => reject(signal.reason);
    signal.addEventListener("abort", cancel, { once: true });
    if (signal.aborted) cancel();
  });
  try { await Promise.race([target.setSinkId(outputId), cancelled]); signal.throwIfAborted(); }
  catch {
    signal.throwIfAborted();
    throw new Error("The selected speaker is unavailable or permission was denied. Choose another speaker or System default in Audio devices, then try again.");
  } finally { signal.removeEventListener("abort", cancel); }
}
