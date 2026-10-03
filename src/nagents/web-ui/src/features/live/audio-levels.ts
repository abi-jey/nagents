/** Ephemeral RMS readings from existing audio graphs; never retains audio frames. */
export interface AudioLevels {
  input(source?: AudioNode): void;
  output(source: AudioNode): () => void;
  sync(): void;
  close(): void;
}

function level(analyser: AnalyserNode, samples: Float32Array<ArrayBuffer>): number {
  try {
    analyser.getFloatTimeDomainData(samples);
    let energy = 0;
    for (const sample of samples) energy += sample * sample;
    const rms = Math.sqrt(energy / samples.length);
    // Keep device noise near silence while allowing ordinary speech to move
    // the meter without implying a calibrated sound-pressure measurement.
    return Number.isFinite(rms) && rms >= 0.003 ? Math.min(1, rms * 4) : 0;
  } catch { return 0; }
}

export function audioLevels(
  context: AudioContext,
  emit: ((input: number, output: number) => void) | undefined,
  enabled: () => readonly [boolean, boolean],
): AudioLevels | undefined {
  if (!emit) return undefined;
  const nodes: AudioNode[] = [];
  let input: AnalyserNode, output: AnalyserNode, silent: GainNode;
  try {
    input = context.createAnalyser(); nodes.push(input);
    output = context.createAnalyser(); nodes.push(output);
    silent = context.createGain(); nodes.push(silent); silent.gain.value = 0;
    input.fftSize = output.fftSize = 1024;
    input.connect(silent); output.connect(silent); silent.connect(context.destination);
  } catch {
    nodes.forEach((node) => { try { node.disconnect(); } catch { /* Metering is optional. */ } });
    return undefined;
  }
  const inputSamples = new Float32Array(input.fftSize), outputSamples = new Float32Array(output.fftSize);
  const sources = new Map<AudioNode, AnalyserNode>();
  let inputSource: AudioNode | undefined, stopped = false, previousInput = 0, previousOutput = 0;
  const publish = (mic: number, speaker: number) => {
    if (mic === previousInput && speaker === previousOutput) return;
    previousInput = mic; previousOutput = speaker; emit(mic, speaker);
  };
  const detach = (source: AudioNode) => {
    const analyser = sources.get(source);
    if (!analyser) return;
    sources.delete(source);
    try { source.disconnect(analyser); } catch { /* The transport may have already disconnected it. */ }
  };
  const attach = (source: AudioNode, analyser: AnalyserNode) => {
    if (stopped) return;
    try { source.connect(analyser); sources.set(source, analyser); } catch { /* Voice must survive unavailable metering. */ }
  };
  const sync = () => {
    const [mic, speaker] = enabled();
    publish(!stopped && context.state === "running" && mic ? previousInput : 0,
      !stopped && context.state === "running" && speaker ? previousOutput : 0);
  };
  const timer = setInterval(() => {
    if (stopped) return;
    const [mic, speaker] = enabled(), running = context.state === "running";
    publish(running && mic && inputSource && sources.has(inputSource) ? level(input, inputSamples) : 0,
      running && speaker && [...sources.values()].includes(output) ? level(output, outputSamples) : 0);
  }, 100);
  return {
    input(source) {
      if (inputSource) detach(inputSource);
      inputSource = source;
      if (source) attach(source, input);
      publish(0, previousOutput);
    },
    output(source) { attach(source, output); return () => detach(source); },
    sync,
    close() {
      if (stopped) return;
      stopped = true; clearInterval(timer);
      inputSamples.fill(0); outputSamples.fill(0);
      for (const source of sources.keys()) detach(source);
      for (const node of nodes) { try { node.disconnect(); } catch { /* A closed context has already released its graph. */ } }
      publish(0, 0);
    },
  };
}
