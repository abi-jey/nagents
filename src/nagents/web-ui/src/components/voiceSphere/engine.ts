import { densityProfiles, silentSignal } from "./types.js";
import type { AudioFrame, DelegationOutcome, SphereConfig, SphereEngine, SphereSnapshot, TaskPhase, TaskView, SignalFrame } from "./types.js";
import { ico, vec, scale, length, normalize, dot, mix, color, community, transitField, transitDisplacement } from "./geometry.js";
import type { Vec3, Edge, RGB, TransitField } from "./geometry.js";
type AudioKind = "input" | "output";
type AmbientKind = "idle" | "think" | "delegate";
type SignalKind = AudioKind | AmbientKind | "event";
interface Impact {
    kind: SignalKind;
    energy: number;
    impact?: number;
    fade?: number;
    tint?: "accent" | "violet" | "amber";
}
interface SignalBloom {
    node: number;
    age: number;
    strength: number;
    tint: "accent" | "violet" | "amber";
}
interface Packet extends Impact {
    kind: AudioKind | AmbientKind;
    ambient?: boolean;
    path: number[];
    lengths: number[];
    history: number[];
    segment: number;
    along: number;
    age: number;
    fade: number;
    speedMultiplier: number;
    speed: number;
    hops: number;
    maxHops: number;
    goal: number[] | null;
    goalReached: boolean;
    goalVisits: number;
    goalTail: number;
    previousNode?: number;
    targetCommunity?: number;
    retiring?: boolean;
}
interface Channel {
    previous: number;
    cooldown: number;
    rise: number;
    riseEnergy: number;
}
interface SignalState {
    graph: Vec3[];
    adjacency: number[][];
    packets: Packet[];
    position: Vec3[];
    velocity: Vec3[];
    glow: number[];
    clock: number;
    serial: number;
    inner: number[];
    outer: number[];
    inward: number[];
    outward: number[];
    communities: number[][];
    edgeUse: Map<string, {
        count: number;
        time: number;
    }>;
    originUse: Map<number, number>;
    input: Channel;
    output: Channel;
}
interface Task extends TaskView {
    path: readonly number[];
    returnPath?: readonly number[];
    age: number;
    duration: number;
    impactRoute: readonly number[];
    impactStep: number;
    sourceImpacted: boolean;
    resultQueued: boolean;
}
interface Point extends Vec3 {
    s: number;
}
interface Projected extends Point {
    id: number;
}
interface Pulse {
    start: number;
    graph: Vec3[];
    impacts: Set<number>;
    sourcePending: boolean;
}
interface DelegationState {
    graph: Vec3[];
    tasks: Task[];
    nextId: number;
    inner: number[];
    allowed: Set<number>;
    recentEdges: Map<number, number>;
    recentEndpoints: number[];
}
const palettes: Record<SphereConfig["color"], readonly [
    RGB,
    RGB
]> = { mint: [[145, 235, 217], [22, 125, 101]], ice: [[141, 202, 250], [28, 106, 166]], iris: [[194, 168, 239], [119, 77, 173]] };
const extraColors: readonly RGB[] = [[185, 163, 239], [230, 187, 135]];
const finite = (value: number, fallback: number, min: number, max: number): number => Number.isFinite(value) ? Math.max(min, Math.min(max, value)) : fallback;
const terminalTask = (phase: TaskPhase): boolean => phase === "complete" || phase === "failed" || phase === "cancelled";
function cleanConfig(config: SphereConfig): SphereConfig {
    const min = finite(config.thinkMin, .45, .1, 3), max = finite(config.thinkMax, 1.65, .1, 3);
    return { ...config, density: Math.round(finite(config.density, 3, 1, 7)), speed: finite(config.speed, 1, .2, 2.4), glow: finite(config.glow, .65, .1, 1), thinkMin: Math.min(min, max), thinkMax: Math.max(min, max) };
}
function cleanSignal(signal: SignalFrame): SignalFrame { return { active: Boolean(signal.active), rms: finite(signal.rms, 0, 0, 1), low: finite(signal.low, 0, 0, 1), mid: finite(signal.mid, 0, 0, 1), high: finite(signal.high, 0, 0, 1) }; }
/** Independent Canvas simulation. The host owns time, audio and all DOM events. */
export function createSphereEngine(config: SphereConfig): SphereEngine {
    const state = { ...cleanConfig(config), yaw: .3, pitch: -.1, zoom: 1, time: 1.4, hover: -1, origin: 0 };
    const reduced = { get matches(): boolean { return state.reducedMotion; } };
    let disposed = false, dark = state.dark, width = 0, height = 0, drag = false, velocity = 0;
    let nodes: Vec3[] = [], edges: Edge[] = [], adj: number[][] = [], parents: number[] = [], distance: number[] = [], maxDistance = 0;
    let pulse: Pulse | undefined;
    let projected: Projected[] = [];
    const signalBlooms: SignalBloom[] = [];
    let physicalCache: Vec3[] = [], physicalDirty = true, sharedShock = 0;
    let inputSignal = silentSignal(), outputSignal = silentSignal();
    const pointerLight = { x: 0, y: 0, targetX: 0, targetY: 0, alpha: 0, targetAlpha: 0 };
    const speech = { blend: 0, envelope: 0, inner: 0, outer: 0, shape: 0, shellShape: 0, low: 0, mid: 0, high: 0, clock: 0 };
    const listening = { blend: 1, energy: 0, shape: 0, low: 0, mid: 0, high: 0 };
    const breath = { outer: 0, inner: 0, outerVelocity: 0, innerVelocity: 0 };
    const voiceSignals: SignalState = { graph: [], adjacency: [], packets: [], position: [], velocity: [], glow: [], clock: 0, serial: 0, inner: [], outer: [], inward: [], outward: [], communities: [], edgeUse: new Map(), originUse: new Map(), input: { previous: 0, cooldown: 0, rise: 0, riseEnergy: 0 }, output: { previous: 0, cooldown: 0, rise: 0, riseEnergy: 0 } };
    const delegationDemo: DelegationState = { graph: [], tasks: [], nextId: 1, inner: [], allowed: new Set(), recentEdges: new Map(), recentEndpoints: [] };
    const seenIds = new Set<string>(), delegationNames = ['Agent A', 'Agent B', 'Agent C'];
    let delegationStatus = 'Send a task through the inner network, then return its result.';
    const rgb = (): RGB => palettes[state.color][dark ? 0 : 1];
    function build(): void {
        const profile = densityProfiles[state.density - 1], surface = ico(profile.surface), interior = ico(profile.core, .56);
        nodes = surface.nodes;
        edges = surface.edges;
        const offset = nodes.length;
        for (const p of interior.nodes) {
            const a = .41;
            nodes.push(vec(p.x * Math.cos(a) + p.z * Math.sin(a), p.y, -p.x * Math.sin(a) + p.z * Math.cos(a)));
        }
        edges.push(...interior.edges.map(([a, b]): Edge => [a + offset, b + offset]));
        for (let i = 0; i < interior.nodes.length; i++) {
            const p = nodes[i + offset], nearest = [-1, -1], scores = [-Infinity, -Infinity];
            for (let j = 0; j < offset; j++) {
                const score = dot(p, nodes[j]);
                if (score > scores[0]) {
                    scores[1] = scores[0];
                    nearest[1] = nearest[0];
                    scores[0] = score;
                    nearest[0] = j;
                }
                else if (score > scores[1]) {
                    scores[1] = score;
                    nearest[1] = j;
                }
            }
            edges.push([i + offset, nearest[0]], [i + offset, nearest[1]]);
        }
        adj = nodes.map(() => []);
        for (const [a, b] of edges) {
            adj[a].push(b);
            adj[b].push(a);
        }
        routeFrom(nodes.reduce((best, p, i) => p.z > nodes[best].z ? i : best, 0));
        state.hover = -1;
        projected = [];
        resetVoiceSignals();
        ensureDelegationGraph();
    }
    function routeFrom(origin: number): void {
        physicalDirty = true;
        pulse = undefined;
        state.origin = origin;
        parents = nodes.map(() => -1);
        distance = nodes.map(() => Infinity);
        distance[origin] = 0;
        const queue = [origin];
        for (let head = 0; head < queue.length; head++)
            for (const next of adj[queue[head]])
                if (!Number.isFinite(distance[next])) {
                    distance[next] = distance[queue[head]] + 1;
                    parents[next] = queue[head];
                    queue.push(next);
                }
        maxDistance = Math.max(...distance);
    }
    function rotate(p: Vec3): Vec3 { const cy = Math.cos(state.yaw), sy = Math.sin(state.yaw), cp = Math.cos(state.pitch), sp = Math.sin(state.pitch); const x = p.x * cy + p.z * sy, z = -p.x * sy + p.z * cy; return vec(x, p.y * cp - z * sp, p.y * sp + z * cp); }
    function soften(value: number, target: number, dt: number, attack = .065, release = .25) {
        const result = value + (target - value) * (1 - Math.exp(-dt / (target > value ? attack : release)));
        return Math.abs(result - target) < .0005 ? target : result;
    }
    function audioEnergy(rms: number, floor = .003) { return rms > floor ? Math.pow(Math.max(0, Math.min(1, (20 * Math.log10(rms) + 50) / 38)), 1.3) : 0; }
    function stepBreath(key: "outer" | "inner", target: number, dt: number) {
        const velocity: "outerVelocity" | "innerVelocity" = key === 'inner' ? 'innerVelocity' : 'outerVelocity', stiffness = key === 'inner' ? 2600 : 2000, damping = key === 'inner' ? 85 : 76;
        const count = Math.max(1, Math.ceil(dt * 120)), step = dt / count;
        for (let i = 0; i < count; i++) {
            breath[velocity] += (stiffness * (target - breath[key]) - damping * breath[velocity]) * step;
            breath[key] += breath[velocity] * step;
        }
        if (Math.abs(breath[key] - target) < .00008 && Math.abs(breath[velocity]) < .0003) {
            breath[key] = target;
            breath[velocity] = 0;
        }
    }
    function updateSpeaking(dt: number) {
        speech.clock += dt;
        const previous = [speech.blend, speech.inner, speech.outer, listening.energy, breath.outer, breath.inner];
        const speaking = outputSignal.active || state.mode === 'speak' ? 1 : 0, receiving = inputSignal.active ? 1 : 0;
        speech.blend = reduced.matches ? speaking : soften(speech.blend, speaking, dt, .2, .25);
        listening.blend = reduced.matches ? receiving : soften(listening.blend, receiving, dt, .2, .25);
        const rms = outputSignal.active ? outputSignal.rms : 0, inputRms = inputSignal.active ? inputSignal.rms : 0;
        speech.envelope = soften(speech.envelope, audioEnergy(rms), dt, .020, .085);
        speech.inner = speech.envelope * speech.blend;
        speech.outer = soften(speech.outer, speech.inner, dt, .025, .085);
        listening.energy = soften(listening.energy, audioEnergy(inputRms, .005) * listening.blend, dt, .022, .090);
        // Fast amplitude drives scale directly; the slower light envelope no longer
        // smooths away consonants, gaps, or the inward/outward change in direction.
        speech.shape = soften(speech.shape, Math.min(1, Math.max(0, rms - .003) * 5.3) * speaking, dt, .005, .028);
        speech.shellShape = soften(speech.shellShape, speech.shape, dt, .006, .032);
        listening.shape = soften(listening.shape, Math.min(1, Math.max(0, inputRms - .004) * 9) * receiving, dt, .006, .030);
        for (const band of ['low', 'mid', 'high'] as const) {
            speech[band] = soften(speech[band], outputSignal.active ? outputSignal[band] : 0, dt, .06, .22);
            listening[band] = soften(listening[band], inputSignal.active ? inputSignal[band] : 0, dt, .06, .22);
        }
        if (reduced.matches) {
            breath.outer = 0;
            breath.inner = 0;
            breath.outerVelocity = 0;
            breath.innerVelocity = 0;
        }
        else {
            stepBreath('outer', .065 * speech.shellShape - .030 * listening.shape, dt);
            stepBreath('inner', .095 * speech.shape - .050 * listening.shape, dt);
        }
        return previous.some((value, i) => value !== [speech.blend, speech.inner, speech.outer, listening.energy, breath.outer, breath.inner][i]);
    }
    function signalTransits(): TransitField[] {
        const fields: TransitField[] = [];
        for (const packet of voiceSignals.packets) {
            const a = packet.path[packet.segment], b = packet.path[packet.segment + 1];
            if (a === undefined || b === undefined) continue;
            fields.push(transitField(nodes[a], nodes[b], packet.along / packet.lengths[packet.segment], packet.energy, packet.impact ?? 1, packet.fade));
        }
        for (const task of delegationDemo.tasks) {
            if (task.phase !== "launch" && task.phase !== "return") continue;
            const head = delegationHead(task);
            fields.push(transitField(nodes[head.a], nodes[head.b], head.t, .55, .65));
        }
        return fields;
    }
    function refreshPhysical(): void {
        if (!physicalDirty) return;
        physicalDirty = false;
        sharedShock = 0;
        const fields = reduced.matches ? [] : signalTransits();
        physicalCache = nodes.map((p, index) => calculatePhysical(p, index, fields));
    }
    function physical(p: Vec3, index: number): Vec3 {
        refreshPhysical();
        return physicalCache[index] ?? p;
    }
    function calculatePhysical(p: Vec3, index: number, fields: readonly TransitField[]): Vec3 {
        const inner = length(p) < .7;
        let r = 1 + .022 * Math.sin(index * 7.13 + 1.9) * Math.cos(index * 3.17);
        if (!reduced.matches) {
            r += inner ? breath.inner : breath.outer;
            if (state.mode === 'delegate')
                r += .014 * Math.sin(state.time * 2.5 + community(p) * 2.1);
            if (pulse && index >= 0) {
                const age = state.time - pulse.start, wave = Math.exp(-Math.pow(distance[index] - .5 - age * 2.8, 2) * 1.15);
                r += wave * .013;
            }
        }
        const position = scale(p, r);
        if (!reduced.matches) {
            const t = state.time * .75, phase = p.x * 3.7 + p.y * 2.4 + p.z * 2.9;
            const strength = .006 * speech.inner + .009 * listening.energy;
            position.x += strength * Math.sin(t + phase);
            position.y += strength * Math.cos(t * .83 + phase * 1.1) * .6;
            position.z += strength * Math.sin(t * .67 + phase * .8) * .6;
            const eventOffset = transitDisplacement(p, fields), arrival = signalNodeOffset(index, p);
            const combined = vec(eventOffset.x + arrival.x, eventOffset.y + arrival.y, eventOffset.z + arrival.z), magnitude = length(combined), limit = magnitude > .045 ? .045 / magnitude : 1;
            sharedShock = Math.max(sharedShock, magnitude * limit);
            position.x += combined.x * limit;
            position.y += combined.y * limit;
            position.z += combined.z * limit;
        }
        return position;
    }
    function project(p: Vec3, w: number, h: number, small = false): Point { const q = rotate(p), s = Math.min(w * (small ? .38 : .36), h * (small ? .38 : .365)) * (small ? 1 : state.zoom); const perspective = 3.9 / (3.9 - q.z); return { x: w / 2 + q.x * s * perspective, y: h * (small ? .50 : .48) - q.y * s * perspective, z: q.z, s: perspective }; }
    function glowDot(context: CanvasRenderingContext2D, p: Point, r: number, c: RGB, alpha: number, glow: boolean, small = false) {
        const brightness = .40 + state.glow * .92;
        if (glow && !small) {
            const radius = r * (2.5 + state.glow * 5);
            const gradient = context.createRadialGradient(p.x, p.y, 0, p.x, p.y, radius);
            gradient.addColorStop(0, color(c, Math.min(1, alpha * state.glow * .72)));
            gradient.addColorStop(1, color(c, 0));
            context.fillStyle = gradient;
            context.beginPath();
            context.arc(p.x, p.y, radius, 0, Math.PI * 2);
            context.fill();
        }
        context.fillStyle = color(c, Math.min(1, alpha * brightness));
        context.beginPath();
        context.arc(p.x, p.y, r, 0, Math.PI * 2);
        context.fill();
    }
    function waveStrength(i: number) {
        let value = 0;
        if (pulse) {
            const age = state.time - pulse.start;
            if (age < maxDistance / 2.8 + 1.5)
                value = Math.max(value, .44 * Math.exp(-Math.pow(distance[i] - age * 2.8, 2) * 1.15));
        }
        return value;
    }
    function draw(context: CanvasRenderingContext2D, w: number, h: number, small = false) {
        context.clearRect(0, 0, w, h);
        if (!small) {
            context.fillStyle = dark ? '#0b1417' : '#f3f8f5';
            context.fillRect(0, 0, w, h);
        }
        const accent = rgb();
        if (!small) {
            const size = Math.min(w, h) * .43 * state.zoom;
            const halo = context.createRadialGradient(w / 2, h * .48, size * .05, w / 2, h * .48, size * 1.2);
            halo.addColorStop(0, color(accent, dark ? .045 : .035));
            halo.addColorStop(.66, color(accent, dark ? .025 : .025));
            halo.addColorStop(1, color(accent, 0));
            context.fillStyle = halo;
            context.fillRect(0, 0, w, h);
        }
        if (speech.blend > 0) {
            const center = project(vec(0, 0, 0), w, h, small), radius = Math.min(w, h) * (small ? .30 : .24) * (small ? 1 : state.zoom);
            const light = context.createRadialGradient(center.x, center.y, 0, center.x, center.y, radius);
            const violet: RGB = dark ? [190, 168, 243] : [119, 77, 173];
            light.addColorStop(0, color(violet, (dark ? .028 : .015) * speech.blend + speech.inner * (dark ? .050 : .026)));
            light.addColorStop(.6, color(violet, speech.inner * .018));
            light.addColorStop(1, color(violet, 0));
            context.fillStyle = light;
            context.fillRect(0, 0, w, h);
        }
        const points = nodes.map((p, i) => ({ ...project(physical(p, i), w, h, small), id: i }));
        if (!small)
            projected = points;
        const miniStride = small && nodes.length > 600 ? (nodes.length > 1200 ? 3 : 2) : 1;
        const miniInner = miniStride > 1 ? new Uint8Array(nodes.length) : null, miniKeep = miniStride > 1 ? new Uint8Array(nodes.length) : null;
        const miniWave = miniStride > 1 ? new Float32Array(nodes.length) : null, miniGlow = miniStride > 1 ? new Float32Array(nodes.length) : null, miniPaths = new Set<number>();
        const edgeKey = (a: number, b: number) => Math.min(a, b) * nodes.length + Math.max(a, b);
        if (miniStride > 1) {
            for (let i = 0; i < nodes.length; i++) {
                miniInner![i] = Number(length(nodes[i]) < .7);
                miniWave![i] = waveStrength(i);
                miniGlow![i] = signalNodeGlow(i);
            }
            function preservePath(path: readonly number[]) { for (let i = 0; i < path.length; i++) {
                miniKeep![path[i]] = 1;
                if (i)
                    miniPaths.add(edgeKey(path[i - 1], path[i]));
            } }
            if (voiceSignals.graph === nodes)
                for (const packet of voiceSignals.packets)
                    preservePath(packet.path);
            if (delegationDemo.graph === nodes)
                for (const task of delegationDemo.tasks)
                    if (task.phase !== 'complete' || task.age < 1) {
                        preservePath(task.path);
                        if (task.returnPath)
                            preservePath(task.returnPath);
                    }
        }
        const ordered = [];
        for (let i = 0; i < edges.length; i++) {
            const [a, b] = edges[i];
            if (miniStride > 1) {
                if (miniInner![a] !== miniInner![b]) {
                    miniKeep![a] = 1;
                    miniKeep![b] = 1;
                }
                const active = Math.max(miniWave![a], miniWave![b], miniGlow![a] * .38, miniGlow![b] * .38);
                if (!miniInner![a] && !miniInner![b] && i % miniStride !== 0 && active < .012 && a !== state.origin && b !== state.origin && a % 23 !== 0 && b % 23 !== 0 && !miniPaths.has(edgeKey(a, b)))
                    continue;
            }
            ordered.push({ a, b, depth: (points[a].z + points[b].z) * .5, id: i });
        }
        // At 80 px the depth-dependent opacity carries depth; avoid sorting thousands
        // of translucent subpixel edges after selecting the compact preview's detail.
        if (miniStride === 1)
            ordered.sort((a, b) => a.depth - b.depth);
        for (const edge of ordered) {
            const a = points[edge.a], b = points[edge.b], front = Math.max(0, Math.min(1, (edge.depth + 1) / 2));
            const aInner = miniInner ? Boolean(miniInner![edge.a]) : length(nodes[edge.a]) < .7, bInner = miniInner ? Boolean(miniInner![edge.b]) : length(nodes[edge.b]) < .7, inner = aInner && bInner, bridge = aInner !== bInner;
            let alpha = (dark ? .065 : .09) + Math.pow(front, 1.6) * (dark ? .30 : .30);
            if (state.mode !== 'delegate')
                alpha *= inner ? 1.45 : bridge ? .58 : .64;
            if (small)
                alpha *= .80;
            const active = miniWave ? Math.max(miniWave![edge.a], miniWave![edge.b], miniGlow![edge.a] * .38, miniGlow![edge.b] * .38) : Math.max(waveStrength(edge.a), waveStrength(edge.b), signalNodeGlow(edge.a) * .38, signalNodeGlow(edge.b) * .38);
            const related = state.hover === edge.a || state.hover === edge.b;
            const hover = hoverStrength({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, z: edge.depth }, small);
            const speechLight = (inner ? speech.inner : bridge ? (speech.inner + speech.outer) * .32 : speech.outer) + listening.energy * (inner ? .35 : .6);
            let c: RGB = inner && state.mode !== 'delegate' ? (dark ? [190, 168, 243] : [119, 77, 173]) : accent;
            if (state.mode === 'delegate') {
                const group = community(nodes[edge.a]);
                if (group === 1)
                    c = dark ? extraColors[0] : [132, 91, 178];
                if (group === 2)
                    c = dark ? extraColors[1] : [167, 117, 44];
                alpha *= .85 + .15 * Math.sin(state.time * 2 + group * 2.1);
            }
            context.strokeStyle = color(c, related ? .82 : Math.min(1, alpha + active * .55 + hover * .23 + speechLight * (inner ? .17 : .09)));
            context.lineWidth = (small ? .45 : .58 + front * .26) + (active + Number(related)) * (small ? .25 : .58) + (inner ? (small ? .12 : .30) : 0) + hover * .25 + speechLight * (small ? .10 : .14);
            line(context, a, b, small);
        }
        const sorted = (miniStride > 1 ? points.filter(p => miniInner![p.id] || miniKeep![p.id] || p.id % miniStride === 0 || p.id % 23 === 0 || p.id === state.origin || Math.max(miniWave![p.id], miniGlow![p.id] * .55) >= .012) : points.slice()).sort((a, b) => a.z - b.z);
        for (const p of sorted) {
            const front = Math.max(0, Math.min(1, (p.z + 1) / 2));
            const active = miniWave ? Math.max(miniWave![p.id], miniGlow![p.id] * .55) : Math.max(waveStrength(p.id), signalNodeGlow(p.id) * .55);
            const hub = p.id % 23 === 0;
            const selected = p.id === state.origin;
            const inner = miniInner ? Boolean(miniInner![p.id]) : length(nodes[p.id]) < .7;
            let c: RGB = inner && state.mode !== 'delegate' ? (dark ? [190, 168, 243] : [119, 77, 173]) : accent;
            const hover = hoverStrength(p, small), speechLight = (inner ? speech.inner : speech.outer) + listening.energy * (inner ? .3 : .65);
            if (state.mode === 'delegate') {
                const group = community(nodes[p.id]);
                if (group === 1)
                    c = dark ? extraColors[0] : [132, 91, 178];
                if (group === 2)
                    c = dark ? extraColors[1] : [167, 117, 44];
            }
            const radius = (small ? .32 : .72) + (small ? .30 : 1.0) * front + (hub ? (small ? .10 : .38) : 0) + active * (small ? .35 : 1.1) + (inner ? (small ? .16 : .50) : 0) + hover * .34 + speechLight * (small ? .10 : .22);
            const layerAlpha = state.mode !== 'delegate' ? (inner ? 1.2 : .78) : 1;
            const alpha = Math.min(1, ((dark ? .20 : .27) + front * .62 + active * .3) * layerAlpha + hover * .18 + speechLight * .11);
            glowDot(context, p, radius, c, alpha, !small && ((hub && front > .58) || active > .24), small);
            if (selected && !small && !(state.mode === 'speak')) {
                context.strokeStyle = color(accent, .4 * (1 - speech.blend));
                context.lineWidth = .6;
                context.beginPath();
                context.arc(p.x, p.y, 5 + Math.sin(state.time * 1.6) * .6, 0, Math.PI * 2);
                context.stroke();
            }
        }
        drawVoiceSignals(context, w, h, small);
        drawDelegationEffects(context, w, h, small);
        drawSignalBlooms(context, w, h, small);
        if (!small && state.hover >= 0) {
            const point = points[state.hover];
            context.strokeStyle = color(accent, .5);
            context.lineWidth = .7;
            context.beginPath();
            context.arc(point.x, point.y, 9, 0, Math.PI * 2);
            context.stroke();
        }
    }
    function delegationEdgeKey(a: number, b: number) { return Math.min(a, b) * nodes.length + Math.max(a, b); }
    function rememberDelegationPath(path: readonly number[]) {
        for (const [key, value] of delegationDemo.recentEdges) {
            const next = value * .62;
            if (next < .10)
                delegationDemo.recentEdges.delete(key);
            else
                delegationDemo.recentEdges.set(key, next);
        }
        for (let i = 1; i < path.length; i++) {
            const key = delegationEdgeKey(path[i - 1], path[i]);
            delegationDemo.recentEdges.set(key, Math.min(4, (delegationDemo.recentEdges.get(key) || 0) + 1));
        }
        delegationDemo.recentEndpoints.push(path[0], path[path.length - 1]);
        delegationDemo.recentEndpoints = delegationDemo.recentEndpoints.slice(-8);
    }
    function delegationRoute(origin: number, destination: number, avoid: ReadonlySet<number> = new Set<number>()): number[] {
        ensureDelegationGraph();
        const { inner, allowed } = delegationDemo;
        if (origin === destination || !allowed.has(origin) || !allowed.has(destination))
            return [];
        const limit = Math.min(25, inner.length);
        function search(strict: boolean, shortest: boolean): number[] {
            const costs = new Float64Array(nodes.length);
            costs.fill(Infinity);
            costs[origin] = 0;
            const previous = new Int32Array(nodes.length);
            previous.fill(-1);
            const visited = new Set<number>(), weights = new Map<number, number>();
            for (let step = 0; step < inner.length; step++) {
                let current = -1, best = Infinity;
                for (const id of inner)
                    if (!visited.has(id) && costs[id] < best) {
                        current = id;
                        best = costs[id];
                    }
                if (current < 0)
                    break;
                if (current === destination)
                    break;
                visited.add(current);
                for (const next of adj[current]) {
                    if (!allowed.has(next) || visited.has(next))
                        continue;
                    const key = delegationEdgeKey(current, next);
                    if (strict && avoid.has(key))
                        continue;
                    if (!weights.has(key)) {
                        const a = nodes[current], b = nodes[next], span = Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z), recent = delegationDemo.recentEdges.get(key) || 0;
                        weights.set(key, shortest ? 1 + Math.random() * .025 : span * (.35 + Math.random() * 2.3) * (1 + recent * .85) + (avoid.has(key) ? span * 9 : 0));
                    }
                    const candidate = best + weights.get(key)!;
                    if (candidate < costs[next]) {
                        costs[next] = candidate;
                        previous[next] = current;
                    }
                }
            }
            if (!Number.isFinite(costs[destination]))
                return [];
            const route = [destination];
            for (let id = destination; id !== origin;) {
                id = previous[id];
                if (id < 0 || route.length >= limit)
                    return [];
                route.push(id);
            }
            return route.reverse();
        }
        // Prefer a genuinely different return corridor. If excluding outbound edges
        // disconnects the core, a high-cost fallback still reaches the original source.
        if (avoid.size) {
            const separate = search(true, false);
            if (separate.length)
                return separate;
            const shortSeparate = search(true, true);
            if (shortSeparate.length)
                return shortSeparate;
        }
        const varied = search(false, false);
        return varied.length ? varied : search(false, true);
    }
    function delegationPath(): number[] {
        ensureDelegationGraph();
        const inner = delegationDemo.inner;
        if (inner.length < 2)
            return [];
        const busy = new Set(delegationDemo.tasks.filter(task => !terminalTask(task.phase)).flatMap(task => [task.path[0], task.path[task.path.length - 1]]));
        let origins = inner.filter(id => !busy.has(id) && !delegationDemo.recentEndpoints.includes(id));
        if (!origins.length)
            origins = inner.filter(id => id !== delegationDemo.recentEndpoints.at(-1));
        if (!origins.length)
            origins = inner;
        const origin = origins[Math.floor(Math.random() * origins.length)];
        let destinations = inner.filter(id => id !== origin && !adj[origin].includes(id) && !busy.has(id) && !delegationDemo.recentEndpoints.includes(id));
        if (!destinations.length)
            destinations = inner.filter(id => id !== origin && !adj[origin].includes(id));
        if (!destinations.length)
            destinations = inner.filter(id => id !== origin);
        const destination = destinations[Math.floor(Math.random() * destinations.length)];
        return delegationRoute(origin, destination);
    }
    function delegationTravelPath(task: Task): readonly number[] {
        return (task.phase === 'return' || task.phase === 'complete') && task.returnPath?.length ? task.returnPath : task.path;
    }
    function armDelegationImpacts(task: Task) {
        task.impactRoute = delegationTravelPath(task).slice();
        task.impactStep = 0;
        task.sourceImpacted = false;
        advanceDelegationImpacts(task);
    }
    function advanceDelegationImpacts(task: Task) {
        if (reduced.matches || !state.playing || !task.impactRoute)
            return;
        const route = task.impactRoute;
        if (!task.sourceImpacted)
            task.sourceImpacted = sharedSignalImpact('source', route[0], route[1], .55, .65, task.phase === 'return' ? 'amber' : 'violet');
        const reached = Math.min(route.length - 1, Math.floor(Math.min(1, task.age / task.duration) * (route.length - 1) + 1e-9));
        while (task.impactStep < reached) {
            task.impactStep++;
            sharedSignalImpact('arrival', route[task.impactStep], route[task.impactStep - 1], .55, .65, task.phase === 'return' ? 'amber' : 'violet');
        }
    }
    function delegationHead(task: Task) {
        const path = delegationTravelPath(task), progress = Math.max(0, Math.min(1, task.age / task.duration)), distance = progress * (path.length - 1);
        const segment = Math.min(path.length - 2, Math.floor(distance));
        return { a: path[segment], b: path[segment + 1], t: distance - segment, progress, path, distance };
    }

    function drawDelegationEffects(context: CanvasRenderingContext2D, w: number, h: number, small = false) {
        if (reduced.matches || delegationDemo.graph !== nodes)
            return;
        for (const task of delegationDemo.tasks) {
            const route = delegationTravelPath(task);
            if (route.length < 2 || route.some(i => i < 0 || i >= nodes.length))
                continue;
            const returning = task.phase === 'return' || task.phase === 'complete';
            const c: RGB = returning ? (dark ? [233, 189, 127] : [163, 110, 34]) : (dark ? [194, 166, 245] : [125, 78, 171]);
            if (task.phase === 'launch' || task.phase === 'return') {
                const head = delegationHead(task), distance = head.distance;
                const opacity = .24 + .72 * Math.sin(Math.PI * head.progress);
                const start = Math.max(0, distance - .72);
                context.beginPath();
                for (let step = 0; step <= 10; step++) {
                    const along = start + (distance - start) * step / 10, segment = Math.min(route.length - 2, Math.floor(along)), point = project(pathPoint(route[segment], route[segment + 1], along - segment), w, h, small);
                    if (step === 0)
                        context.moveTo(point.x, point.y);
                    else
                        context.lineTo(point.x, point.y);
                }
                context.strokeStyle = color(c, opacity * (small ? .75 : .7));
                context.lineWidth = small ? .8 : 1.6;
                context.stroke();
                const tip = project(pathPoint(head.a, head.b, head.t), w, h, small);
                glowDot(context, tip, small ? 1.0 : 2.2, c, opacity, !small, small);
            }
            else if (task.phase === 'pending') {
                const anchor = task.path[task.path.length - 1], point = project(physical(nodes[anchor], anchor), w, h, small);
                glowDot(context, point, small ? .8 : 1.8, c, .65, false, small);
            }

        }
    }
    function resetVoiceSignals() {
        physicalDirty = true;
        physicalCache = [];
        signalBlooms.length = 0;
        voiceSignals.graph = nodes;
        voiceSignals.adjacency = adj;
        voiceSignals.packets = [];
        voiceSignals.edgeUse.clear();
        voiceSignals.originUse.clear();
        voiceSignals.position = nodes.map(() => vec(0, 0, 0));
        voiceSignals.velocity = nodes.map(() => vec(0, 0, 0));
        voiceSignals.glow = nodes.map(() => 0);
        for (const channel of [voiceSignals.input, voiceSignals.output]) {
            channel.previous = 0;
            channel.cooldown = 0;
            channel.rise = 0;
            channel.riseEnergy = 0;
        }
        voiceSignals.inner = [];
        voiceSignals.outer = [];
        nodes.forEach((p, i) => (length(p) < .7 ? voiceSignals.inner : voiceSignals.outer).push(i));
        voiceSignals.inward = signalGoalDistances(voiceSignals.inner);
        voiceSignals.outward = signalGoalDistances(voiceSignals.outer);
        voiceSignals.communities = [0, 1, 2].map(group => signalGoalDistances(nodes.map((p, i) => community(p) === group ? i : -1).filter(i => i >= 0)));
    }
    function signalGoalDistances(goals: readonly number[]): number[] {
        const distances = nodes.map(() => Infinity), queue = goals.slice();
        for (const id of goals)
            distances[id] = 0;
        for (let head = 0; head < queue.length; head++)
            for (const next of adj[queue[head]])
                if (!Number.isFinite(distances[next])) {
                    distances[next] = distances[queue[head]] + 1;
                    queue.push(next);
                }
        return distances;
    }
    function sampleSignalNode(candidates: readonly number[], weight: (id: number) => number): number {
        const weights = candidates.map(weight), total = weights.reduce((sum, value) => sum + value, 0);
        let choice = Math.random() * total;
        for (let i = 0; i < candidates.length; i++) {
            choice -= weights[i];
            if (choice <= 0)
                return candidates[i];
        }
        return candidates[candidates.length - 1];
    }
    function freshSignalOrigin(kind: SignalKind): number {
        let pool = kind === 'input' ? voiceSignals.outer : kind === 'output' ? voiceSignals.inner : Math.random() < .28 ? voiceSignals.inner : voiceSignals.outer;
        if (!pool.length)
            pool = nodes.map((_, i) => i);
        const id = sampleSignalNode(pool, index => { const used = voiceSignals.originUse.get(index); return used === undefined ? 1 : 1 / (1 + 12 * Math.exp(-(voiceSignals.clock - used) / 8)); });
        voiceSignals.originUse.delete(id);
        voiceSignals.originUse.set(id, voiceSignals.clock);
        while (voiceSignals.originUse.size > 32)
            voiceSignals.originUse.delete(voiceSignals.originUse.keys().next().value!);
        return id;
    }
    function signalEdgeKey(a: number, b: number) { return Math.min(a, b) + ':' + Math.max(a, b); }
    function appendSignalHop(packet: Packet) {
        const current = packet.path[packet.path.length - 1], previous = packet.path.length > 1 ? packet.path[packet.path.length - 2] : packet.previousNode, remaining = packet.maxHops - packet.hops;
        let choices = adj[current].slice();
        if (packet.goal) {
            if (packet.goalReached)
                choices = choices.filter(id => packet.goal![id] === 0);
            else if (remaining <= packet.goal[current] + packet.goalTail)
                choices = choices.filter(id => packet.goal![id] < packet.goal![current]);
        }
        if (!choices.length)
            return false;
        const forward = choices.filter(id => id !== previous);
        if (forward.length)
            choices = forward;
        const unvisited = choices.filter(id => !packet.history.includes(id));
        if (unvisited.length)
            choices = unvisited;
        const next = sampleSignalNode(choices, id => {
            const use = voiceSignals.edgeUse.get(signalEdgeKey(current, id)), recent = use ? use.count * Math.exp(-(voiceSignals.clock - use.time) / 5) : 0;
            let weight = 1 / (1 + recent * 3.5);
            if (packet.history.includes(id))
                weight *= .12;
            if (packet.goal && !packet.goalReached) {
                const change = packet.goal[current] - packet.goal![id];
                weight *= change > 0 ? 2.7 : change === 0 ? 1 : .28;
            }
            else if (!packet.goal && (length(nodes[current]) < .7) !== (length(nodes[id]) < .7))
                weight *= 1.45;
            return weight;
        });
        const key = signalEdgeKey(current, next), lastUse = voiceSignals.edgeUse.get(key), count = lastUse ? lastUse.count * Math.exp(-(voiceSignals.clock - lastUse.time) / 5) + 1 : 1;
        voiceSignals.edgeUse.delete(key);
        voiceSignals.edgeUse.set(key, { count, time: voiceSignals.clock });
        while (voiceSignals.edgeUse.size > 512)
            voiceSignals.edgeUse.delete(voiceSignals.edgeUse.keys().next().value!);
        packet.path.push(next);
        packet.lengths.push(Math.max(.001, length(vec(nodes[next].x - nodes[current].x, nodes[next].y - nodes[current].y, nodes[next].z - nodes[current].z))));
        return true;
    }
    function beginProgressiveSignal(packet: Packet, origin: number, history: readonly number[] = []) {
        packet.path = [origin];
        packet.lengths = [];
        packet.history = history.slice(-6);
        if (packet.history[packet.history.length - 1] !== origin)
            packet.history.push(origin);
        packet.history = packet.history.slice(-6);
        packet.previousNode = packet.history[packet.history.length - 2];
        packet.hops = 0;
        packet.goal = null;
        packet.goalReached = false;
        packet.goalVisits = 0;
        packet.goalTail = 2 + Math.floor(Math.random() * 2);
        packet.maxHops = packet.kind === 'idle' ? 7 + Math.floor(Math.random() * 4) : 8 + Math.floor(Math.random() * 5);
        if (packet.kind === 'input')
            packet.goal = voiceSignals.inward;
        else if (packet.kind === 'output')
            packet.goal = voiceSignals.outward;
        else if (packet.kind === 'delegate') {
            const group = community(nodes[origin]);
            packet.targetCommunity = (group + 1 + Math.floor(Math.random() * 2)) % 3;
            packet.goal = voiceSignals.communities[packet.targetCommunity];
        }
        if (packet.goal) {
            packet.maxHops = Math.min(12, Math.max(packet.maxHops, packet.goal[origin] + packet.goalTail + 2));
            packet.goalReached = packet.goal[origin] === 0;
        }
        if (!appendSignalHop(packet))
            return false;
        voiceSignals.packets.push(packet);
        voiceSignals.serial++;
        fireSignalSource(packet, origin, packet.path[1]);
        return true;
    }
    function voiceSignalImpulse(index: number, previous: number, energy: number, weight = 1) {
        const p = nodes[index], from = nodes[previous] || p;
        const delta = vec(p.x - from.x, p.y - from.y, p.z - from.z), distance = length(delta);
        const direction = distance > .0001 ? scale(delta, 1 / distance) : scale(p, 1 / Math.max(.001, length(p)));
        const amount = (.095 + .075 * energy) * 1.75 * weight, velocity = voiceSignals.velocity[index];
        velocity.x += direction.x * amount;
        velocity.y += direction.y * amount;
        velocity.z += direction.z * amount;
        const magnitude = length(velocity);
        if (magnitude > .56) {
            velocity.x *= .56 / magnitude;
            velocity.y *= .56 / magnitude;
            velocity.z *= .56 / magnitude;
        }
        voiceSignals.glow[index] = Math.min(1, Math.max(voiceSignals.glow[index], (.45 + .5 * energy) * weight));
    }
    function arriveVoiceSignal(packet: Impact, index: number, previous: number) {
        const impact = (packet.impact ?? (packet.kind === 'think' ? .55 : 1)) * (packet.fade ?? 1);
        voiceSignalImpulse(index, previous, packet.energy, impact);
        for (const neighbor of adj[index])
            voiceSignalImpulse(neighbor, index, packet.energy, .20 * impact);
        const strength = (.65 + .35 * packet.energy) * Math.min(1.2, impact / .65);
        const tint = packet.tint ?? (packet.kind === "output" || packet.kind === "event" ? "violet" : "accent");
        const existing = signalBlooms.find(bloom => bloom.node === index && bloom.age < .09);
        if (existing) { existing.strength = Math.max(existing.strength, strength); existing.age = 0; existing.tint = tint; }
        else signalBlooms.push({node:index,age:0,strength,tint});
        if (signalBlooms.length > 24) signalBlooms.splice(0, signalBlooms.length - 24);
        physicalDirty = true;
    }
    function fireSignalSource(packet: Impact, index: number, next: number) {
        arriveVoiceSignal({ ...packet, impact: (packet.impact ?? 1) * .4 }, index, next);
    }
    function advanceSignalBlooms(dt: number): void {
        if (reduced.matches) { signalBlooms.length = 0; return; }
        if (!state.playing) return;
        for (const bloom of signalBlooms) bloom.age += dt;
        for (let index = signalBlooms.length - 1; index >= 0; index--) if (signalBlooms[index].age >= .60) signalBlooms.splice(index, 1);
    }
    function drawSignalBlooms(context: CanvasRenderingContext2D, w: number, h: number, small: boolean): void {
        if (reduced.matches) return;
        for (const bloom of signalBlooms) {
            if (!nodes[bloom.node]) continue;
            const point = project(physical(nodes[bloom.node], bloom.node), w, h, small);
            const age = bloom.age / .60, strength = Math.pow(1 - age, 2) * bloom.strength;
            const c: RGB = bloom.tint === "amber" ? (dark ? [233,189,127] : [163,110,34]) : bloom.tint === "violet" ? (dark ? [194,166,245] : [125,78,171]) : rgb();
            if (!small) {
                const radius = 12 + age * 17, gradient = context.createRadialGradient(point.x, point.y, 0, point.x, point.y, radius);
                gradient.addColorStop(0, color(c, strength * .22)); gradient.addColorStop(1, color(c, 0));
                context.fillStyle = gradient; context.beginPath(); context.arc(point.x, point.y, radius, 0, Math.PI * 2); context.fill();
            }
            glowDot(context, point, (small ? 1 : 2.4) * strength + .3, c, strength * .8, false, small);
        }
    }
    function launchVoiceSignal(kind: AudioKind, energy: number) {
        if (!voiceSignals.inner.length || !voiceSignals.outer.length || voiceSignals.packets.length >= 8)
            return false;
        const sharesWithAmbient = ambientSignalKind() !== '' || voiceSignals.packets.some(packet => packet.ambient);
        if (voiceSignals.packets.filter(packet => packet.kind === kind).length >= (sharesWithAmbient ? 2 : 4))
            return false;
        const speedMultiplier = .85 + Math.random() * .30;
        const packet = newPacket(kind, Math.max(.15, Math.min(1, energy)), speedMultiplier, 1.04 * speedMultiplier);
        return beginProgressiveSignal(packet, freshSignalOrigin(kind));
    }
    function ambientSignalKind(): AmbientKind | "" { return state.mode === 'listen' ? 'idle' : state.mode === 'think' || state.mode === 'delegate' ? state.mode : inputSignal.active ? 'idle' : ''; }
    function ambientSignalBudget() {
        const kind = ambientSignalKind();
        if (!kind)
            return 0;
        const inputActive = typeof inputSignal !== 'undefined' && inputSignal.active, outputActive = outputSignal.active;
        const inputCount = voiceSignals.packets.filter(packet => packet.kind === 'input').length, outputCount = voiceSignals.packets.filter(packet => packet.kind === 'output').length;
        const inputReserve = Math.max(inputCount, inputActive ? 2 : 0), outputReserve = Math.max(outputCount, outputActive ? 2 : 0);
        const busy = inputActive || outputActive || inputCount || outputCount, desired = kind === 'idle' ? 3 : kind === 'delegate' ? (busy ? 3 : 4) : (busy ? 4 : 5);
        return Math.max(0, Math.min(desired, 8 - inputReserve - outputReserve));
    }
    function thinkingSignalBudget() { return state.mode === 'think' ? ambientSignalBudget() : 0; }
    function launchAmbientSignal(start = -1, kind: AmbientKind | "" = ambientSignalKind(), history: readonly number[] = []) {
        if (!kind || kind !== ambientSignalKind() || voiceSignals.packets.length >= 8 || nodes.length < 2 || voiceSignals.packets.filter(packet => packet.ambient && packet.kind === kind && !packet.retiring).length >= ambientSignalBudget())
            return false;
        const low = kind === 'idle' ? .45 : kind === 'delegate' ? .7 : Number.isFinite(state.thinkMin) ? state.thinkMin : .45;
        const high = kind === 'idle' ? .95 : kind === 'delegate' ? 1.3 : Number.isFinite(state.thinkMax) ? state.thinkMax : 1.65;
        const minimum = Math.max(.10, Math.min(3, Math.min(low, high))), maximum = Math.max(minimum, Math.min(3, Math.max(low, high)));
        const speedMultiplier = minimum + Math.random() * (maximum - minimum);
        const origin = start >= 0 && start < nodes.length ? start : freshSignalOrigin(kind);
        const energy = kind === 'idle' ? .18 + Math.random() * .14 : kind === 'delegate' ? .28 + Math.random() * .18 : .25 + Math.random() * .20;
        const impact = .65;
        const packet = newPacket(kind, energy, speedMultiplier, (kind === 'idle' ? .52 : .76) * speedMultiplier, true, impact);
        return beginProgressiveSignal(packet, origin, history);
    }
    function launchThinkingSignal(start = -1) { return launchAmbientSignal(start, 'think'); }
    function updateVoiceSignalChannel(kind: AudioKind, active: boolean, rms: number, energy: number, dt: number) {
        const channel = voiceSignals[kind], raw = active && Number.isFinite(rms) ? Math.max(0, rms) : 0;
        channel.cooldown = Math.max(0, channel.cooldown - dt);
        channel.rise = Math.max(0, channel.rise - dt);
        // A short hold lets the smoothed envelope catch up to the measured syllable onset.
        if (raw > .004 && raw - channel.previous > Math.max(.0025, channel.previous * .12)) {
            channel.rise = .32;
            channel.riseEnergy = 0;
        }
        if (!active) {
            channel.rise = 0;
            channel.riseEnergy = 0;
        }
        if (channel.rise > 0 && Number.isFinite(energy))
            channel.riseEnergy = Math.max(channel.riseEnergy, energy);
        channel.previous = raw;
        if (active && channel.riseEnergy >= .15 && channel.rise > 0 && channel.cooldown === 0) {
            if (launchVoiceSignal(kind, channel.riseEnergy)) {
                channel.cooldown = kind === 'input' ? .34 : .26;
                channel.rise = 0;
                channel.riseEnergy = 0;
            }
        }
    }
    function updateVoiceSignals(dt: number) {
        if (voiceSignals.graph !== nodes || voiceSignals.adjacency !== adj)
            resetVoiceSignals();
        if (reduced.matches) {
            const changed = voiceSignals.packets.length > 0 || voiceSignals.glow.some(value => value > 0);
            if (changed)
                resetVoiceSignals();
            return changed;
        }
        if (!state.playing || !Number.isFinite(dt) || dt <= 0)
            return false;
        const elapsed = Math.min(.05, dt);
        voiceSignals.clock += elapsed;
        const inputActive = typeof inputSignal !== 'undefined' && inputSignal.active;
        const inputEnergy = typeof listening !== 'undefined' ? listening.energy : 0;
        updateVoiceSignalChannel('input', inputActive, inputActive ? inputSignal.rms : 0, inputEnergy, elapsed);
        updateVoiceSignalChannel('output', outputSignal.active, outputSignal.rms, speech.inner, elapsed);
        const ambientKind = ambientSignalKind(), ambientBudget = ambientSignalBudget(), ambient = voiceSignals.packets.filter(packet => packet.ambient && packet.kind === ambientKind && !packet.retiring);
        for (const packet of ambient.slice(ambientBudget))
            packet.retiring = true;
        while (ambientKind && voiceSignals.packets.filter(packet => packet.ambient && packet.kind === ambientKind && !packet.retiring).length < ambientBudget) {
            if (!launchAmbientSignal())
                break;
        }
        let changed = voiceSignals.packets.length > 0;
        const continuations = [], masterSpeed = Number.isFinite(state.speed) ? Math.max(.2, Math.min(2.4, state.speed)) : 1;
        for (const packet of voiceSignals.packets) {
            if (packet.ambient)
                packet.fade = packet.kind === ambientKind && !packet.retiring ? Math.min(1, packet.fade + elapsed / .25) : Math.max(0, packet.fade - elapsed / (packet.retiring ? .18 : .38));
            packet.age += elapsed;
            packet.along += elapsed * packet.speed * masterSpeed;
            while (packet.segment < packet.lengths.length && packet.along >= packet.lengths[packet.segment]) {
                packet.along -= packet.lengths[packet.segment];
                const previous = packet.path[packet.segment], arrival = packet.path[packet.segment + 1];
                arriveVoiceSignal(packet, arrival, previous);
                packet.segment++;
                packet.hops++;
                packet.history.push(arrival);
                if (packet.history.length > 6)
                    packet.history.shift();
                if (packet.goal && packet.goal[arrival] === 0) {
                    if (packet.goalReached)
                        packet.goalVisits++;
                    packet.goalReached = true;
                }
                if (packet.hops < packet.maxHops && (!packet.goalReached || packet.goalVisits < packet.goalTail))
                    appendSignalHop(packet);
            }
            if (packet.ambient && packet.kind === ambientKind && !packet.retiring && packet.segment >= packet.lengths.length)
                continuations.push({ origin: packet.path[packet.path.length - 1], history: packet.history.slice() });
        }
        voiceSignals.packets = voiceSignals.packets.filter(packet => packet.segment < packet.lengths.length && packet.fade > 0);
        for (const continuation of continuations)
            launchAmbientSignal(continuation.origin, ambientKind, continuation.history);
        const steps = Math.max(1, Math.ceil(elapsed / .012)), h = elapsed / steps;
        for (let i = 0; i < nodes.length; i++) {
            const p = voiceSignals.position[i], v = voiceSignals.velocity[i];
            if (length(p) < .00002 && length(v) < .0001 && voiceSignals.glow[i] < .0005) {
                p.x = p.y = p.z = v.x = v.y = v.z = 0;
                voiceSignals.glow[i] = 0;
                continue;
            }
            changed = true;
            for (let j = 0; j < steps; j++) {
                v.x += (-132 * p.x - 16 * v.x) * h;
                v.y += (-132 * p.y - 16 * v.y) * h;
                v.z += (-132 * p.z - 16 * v.z) * h;
                p.x += v.x * h;
                p.y += v.y * h;
                p.z += v.z * h;
                const displacement = length(p);
                if (displacement > .035) {
                    const ratio = .035 / displacement;
                    p.x *= ratio;
                    p.y *= ratio;
                    p.z *= ratio;
                }
            }
            voiceSignals.glow[i] *= Math.exp(-elapsed * 7.4);
        }
        return changed;
    }
    function signalNodeOffset(index: number, p: Vec3): Vec3 {
        if (reduced.matches || voiceSignals.graph !== nodes || !voiceSignals.position[index])
            return vec(0, 0, 0);
        const offset = voiceSignals.position[index];
        return vec(offset.x, offset.y, offset.z);
    }
    function signalNodeGlow(index: number) { return reduced.matches || voiceSignals.graph !== nodes ? 0 : voiceSignals.glow[index] || 0; }
    function drawVoiceSignals(context: CanvasRenderingContext2D, w: number, h: number, small = false) {
        if (reduced.matches || voiceSignals.graph !== nodes)
            return;
        for (const packet of voiceSignals.packets) {
            const a = packet.path[packet.segment], b = packet.path[packet.segment + 1];
            if (a === undefined || b === undefined)
                continue;
            const fraction = packet.along / packet.lengths[packet.segment], point = project(pathPoint(a, b, fraction), w, h, small);
            const tail = project(pathPoint(a, b, Math.max(0, fraction - .085 / packet.lengths[packet.segment])), w, h, small);
            let c: RGB = packet.kind === 'output' ? (dark ? [210, 185, 251] : [121, 76, 171]) : rgb();
            if (packet.kind === 'delegate') {
                const group = community(nodes[packet.path[0]]);
                if (group === 1)
                    c = dark ? [185, 163, 239] : [132, 91, 178];
                if (group === 2)
                    c = dark ? [230, 187, 135] : [167, 117, 44];
            }
            const depth = Math.max(0, Math.min(1, (point.z + 1) / 2)), alpha = (.38 + .55 * depth) * Math.min(1, packet.age * 8) * packet.fade;
            context.strokeStyle = color(c, alpha * .7);
            context.lineWidth = small ? .7 : 1.15;
            context.beginPath();
            context.moveTo(tail.x, tail.y);
            context.lineTo(point.x, point.y);
            context.stroke();
            glowDot(context, point, (small ? .52 : 1.1) + (small ? .20 : .55) * packet.energy, c, alpha, !small, small);
        }
    }
    function updatePointerLight(dt: number) {
        if (drag)
            return false;
        const positionBlend = reduced.matches ? 1 : 1 - Math.exp(-dt * 15), fadeBlend = reduced.matches ? 1 : 1 - Math.exp(-dt * 11);
        const oldX = pointerLight.x, oldY = pointerLight.y, oldAlpha = pointerLight.alpha;
        pointerLight.x = Math.abs(pointerLight.targetX - oldX) < .05 ? pointerLight.targetX : oldX + (pointerLight.targetX - oldX) * positionBlend;
        pointerLight.y = Math.abs(pointerLight.targetY - oldY) < .05 ? pointerLight.targetY : oldY + (pointerLight.targetY - oldY) * positionBlend;
        pointerLight.alpha = Math.abs(pointerLight.targetAlpha - oldAlpha) < .001 ? pointerLight.targetAlpha : oldAlpha + (pointerLight.targetAlpha - oldAlpha) * fadeBlend;
        return pointerLight.x !== oldX || pointerLight.y !== oldY || pointerLight.alpha !== oldAlpha;
    }
    function hoverStrength(point: Vec3, small = false) {
        if (small || pointerLight.alpha <= 0)
            return 0;
        const front = Math.max(0, Math.min(1, (point.z + .12) / .75)), radius = Math.max(52, Math.min(88, Math.min(width, height) * .20));
        const dx = point.x - pointerLight.x, dy = point.y - pointerLight.y;
        return pointerLight.alpha * front * front * (3 - 2 * front) * Math.exp(-(dx * dx + dy * dy) / (radius * radius * .72));
    }
    function pathPoint(a: number, b: number, t: number): Vec3 { return mix(physical(nodes[a], a), physical(nodes[b], b), t); }
    function line(context: CanvasRenderingContext2D, a: Point, b: Point, _small: boolean): void { context.beginPath(); context.moveTo(a.x, a.y); context.lineTo(b.x, b.y); context.stroke(); }
    function newPacket(kind: AudioKind | AmbientKind, energy: number, speedMultiplier: number, speed: number, ambient = false, impact = 1): Packet {
        return { kind, energy, speedMultiplier, speed, ambient, impact, path: [], lengths: [], history: [], segment: 0, along: 0, age: 0, fade: 1, hops: 0, maxHops: 12, goal: null, goalReached: false, goalVisits: 0, goalTail: 2 };
    }
    function syncDelegationDemo(message = ''): void { if (message)
        delegationStatus = message; }
    function ensureDelegationGraph(): void {
        if (delegationDemo.graph === nodes)
            return;
        const previous = delegationDemo.tasks;
        delegationDemo.graph = nodes;
        delegationDemo.tasks = [];
        delegationDemo.inner = nodes.map((p, i) => length(p) < .7 ? i : -1).filter(i => i >= 0);
        delegationDemo.allowed = new Set(delegationDemo.inner);
        delegationDemo.recentEdges.clear();
        delegationDemo.recentEndpoints = [];
        for (const old of previous) {
            const path = delegationPath();
            if (path.length < 2)
                continue;
            const task: Task = { ...old, path: Object.freeze(path), returnPath: undefined, age: terminalTask(old.phase) ? 1 : 0, impactRoute: [], impactStep: 0, sourceImpacted: false };
            if (task.phase === 'return') {
                const avoid = new Set(path.slice(1).map((id, i) => delegationEdgeKey(path[i], id))), route = delegationRoute(path[path.length - 1], path[0], avoid);
                task.returnPath = Object.freeze(route.length > 1 ? route : path.slice().reverse());
            }
            delegationDemo.tasks.push(task);
            rememberDelegationPath(path);
            if (task.phase === 'launch' || task.phase === 'return')
                armDelegationImpacts(task);
        }
        if (previous.length)
            syncDelegationDemo('Network detail changed. Active tasks continue on the new network.');
    }
    function sharedSignalImpact(phase: 'source' | 'arrival', index: number, other: number, energy: number, impact = 1, tint: Impact['tint'] = impact <= .2 ? 'accent' : 'violet'): boolean {
        if (reduced.matches || !state.playing || !nodes[index] || !nodes[other] || !adj[index]?.includes(other))
            return false;
        if (voiceSignals.graph !== nodes || voiceSignals.adjacency !== adj)
            resetVoiceSignals();
        const packet: Impact = { kind: 'event', energy, impact, fade: 1, tint };
        if (phase === 'source')
            fireSignalSource(packet, index, other);
        else
            arriveVoiceSignal(packet, index, other);
        return true;
    }
    function fireDelegation(id?: string, label?: string): boolean {
        if (disposed)
            return false;
        ensureDelegationGraph();
        if (id !== undefined && (id.trim() === '' || seenIds.has(id)))
            return false;
        const occupied = new Set(delegationDemo.tasks.filter(task => !terminalTask(task.phase)).map(task => task.slot)), slot = [0, 1, 2].find(value => !occupied.has(value));
        if (slot === undefined)
            return false;
        const path = delegationPath();
        if (path.length < 2)
            return false;
        let taskId = id;
        while (taskId === undefined) {
            const proposed = 'demo-' + delegationDemo.nextId++;
            if (!seenIds.has(proposed))
                taskId = proposed;
        }
        const task: Task = { id: taskId, label: label?.trim() || delegationNames[slot], slot, path: Object.freeze(path), phase: reduced.matches ? 'pending' : 'launch', age: 0, duration: 1.45 + slot * .18, impactRoute: [], impactStep: 0, sourceImpacted: false, resultQueued: false };
        seenIds.add(taskId);
        delegationDemo.tasks = delegationDemo.tasks.filter(item => item.slot !== slot);
        rememberDelegationPath(path);
        delegationDemo.tasks.push(task);
        armDelegationImpacts(task);
        syncDelegationDemo('Task sent to ' + task.label + '.');
        return true;
    }
    function returnDelegation(id?: string): boolean {
        if (disposed)
            return false;
        ensureDelegationGraph();
        const task = id === undefined ? delegationDemo.tasks.find(item => item.phase === 'pending') : delegationDemo.tasks.find(item => item.id === id);
        if (!task || task.resultQueued || task.phase === 'return' || terminalTask(task.phase))
            return false;
        if (task.phase === 'launch') {
            task.resultQueued = true;
            syncDelegationDemo('Result queued for ' + task.label + '.');
            return true;
        }
        const avoid = new Set(task.path.slice(1).map((node, i) => delegationEdgeKey(task.path[i], node))), route = delegationRoute(task.path[task.path.length - 1], task.path[0], avoid);
        if (route.length < 2)
            return false;
        task.returnPath = Object.freeze(route);
        rememberDelegationPath(route);
        task.phase = reduced.matches ? 'complete' : 'return';
        task.age = 0;
        task.duration = 1.2;
        armDelegationImpacts(task);
        syncDelegationDemo(reduced.matches ? 'Result received from ' + task.label + '.' : 'Result returning from ' + task.label + '.');
        return true;
    }
    function finishDelegation(id: string, outcome: DelegationOutcome): boolean {
        if (disposed || (outcome !== "failed" && outcome !== "cancelled") || !id.trim()) return false;
        ensureDelegationGraph();
        const task = delegationDemo.tasks.find(item => item.id === id);
        if (!task || terminalTask(task.phase)) return false;
        task.phase = outcome;
        task.resultQueued = false;
        task.returnPath = undefined;
        task.impactRoute = [];
        task.age = 0;
        physicalDirty = true;
        syncDelegationDemo(task.label + (outcome === "failed" ? ' failed.' : ' was cancelled.'));
        return true;
    }
    function resetDelegationDemo(): void { if (disposed)
        return; ensureDelegationGraph(); delegationDemo.tasks = []; seenIds.clear(); physicalDirty = true; syncDelegationDemo('Delegations reset.'); }
    function updateDelegationEffects(dt: number): void {
        ensureDelegationGraph();
        for (const task of delegationDemo.tasks) {
            if (task.phase === 'pending' || terminalTask(task.phase))
                continue;
            if (reduced.matches) {
                task.phase = task.phase === 'launch' ? 'pending' : 'complete';
                task.age = 1;
            }
            else if (state.playing) {
                task.age += Math.min(.08, Math.max(0, dt));
                if (task.phase === 'launch' || task.phase === 'return')
                    advanceDelegationImpacts(task);
                if (task.phase === 'launch' && task.age >= task.duration) {
                    task.phase = 'pending';
                    task.age = 0;
                    syncDelegationDemo(task.label + ' is working on the task.');
                }
                else if (task.phase === 'return' && task.age >= task.duration) {
                    task.phase = 'complete';
                    task.age = 0;
                    syncDelegationDemo('Result received from ' + task.label + '.');
                }
            }
            if (task.phase === 'pending' && task.resultQueued) {
                task.resultQueued = false;
                returnDelegation(task.id);
            }
        }
    }
    function sendPulse(node?: number): void {
        if (disposed)
            return;
        if (node !== undefined && Number.isInteger(node) && node >= 0 && node < nodes.length)
            routeFrom(node);
        pulse = { start: state.time, graph: nodes, impacts: new Set([state.origin]), sourcePending: !reduced.matches };
        if (sharedSignalImpact('source', state.origin, adj[state.origin][0], .20, .20)) pulse.sourcePending = false;
        physicalDirty = true;
    }
    function updatePulseImpacts(): void {
        if (!pulse || reduced.matches || !state.playing)
            return;
        if (pulse.graph !== nodes) {
            pulse = undefined;
            return;
        }
        if (pulse.sourcePending) pulse.sourcePending = !sharedSignalImpact('source', state.origin, adj[state.origin][0], .20, .20);
        const reach = (state.time - pulse.start) * 2.8;
        for (let i = 0; i < nodes.length; i++)
            if (!pulse.impacts.has(i) && Number.isFinite(distance[i]) && distance[i] <= reach) {
                pulse.impacts.add(i);
                sharedSignalImpact('arrival', i, parents[i], .20, .18);
            }
        if (reach > maxDistance + 4.2)
            pulse = undefined;
    }
    function closest(point: {
        x: number;
        y: number;
    }): number { let best = -1, score = Infinity; projected.forEach((p, i) => { if (p.z < -.35)
        return; const d = Math.hypot(p.x - point.x, p.y - point.y); if (d < score) {
        score = d;
        best = i;
    } }); return score < 40 ? best : -1; }
    function snapshot(): SphereSnapshot {
        refreshPhysical();
        return { nodes: nodes.length, edges: edges.length, origin: state.origin, zoom: state.zoom, tasks: delegationDemo.tasks.map(({ id, label, slot, phase, resultQueued }) => ({ id, label, slot, phase, resultQueued })), delegationStatus, inputLevel: listening.energy, outputLevel: speech.inner, outerScale: 1 + breath.outer, innerScale: 1 + breath.inner, ambientPackets: voiceSignals.packets.filter(p => p.ambient).length, voicePackets: voiceSignals.packets.filter(p => !p.ambient).length, shock: sharedShock };
    }
    build();
    return {
        configure(next) { if (disposed)
            return; const before = state.density; Object.assign(state, cleanConfig(next)); dark = state.dark; physicalDirty = true; if (before !== state.density)
            build(); },
        tick(seconds, audio) { if (disposed || !Number.isFinite(seconds) || seconds <= 0)
            return; const dt = Math.min(.05, seconds); inputSignal = cleanSignal(audio.input); outputSignal = cleanSignal(audio.output); updatePointerLight(dt); updateSpeaking(dt); advanceSignalBlooms(dt); updatePulseImpacts(); updateDelegationEffects(dt); updateVoiceSignals(dt); physicalDirty = true; if (state.playing) {
            state.time += dt;
            if (!drag && !reduced.matches)
                state.yaw += dt * .075;
        } if (!drag && Math.abs(velocity) > .0001 && !reduced.matches) {
            state.yaw += velocity;
            velocity *= Math.pow(.91, dt * 60);
        } },
        render(context, w, h, compact = false) { if (disposed || !Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0)
            return; if (!compact) {
            width = w;
            height = h;
        } draw(context, w, h, compact); },
        snapshot,
        rotate(yawDelta, pitchDelta = 0) { if (disposed)
            return; const yaw = finite(yawDelta, 0, -Math.PI, Math.PI), pitch = finite(pitchDelta, 0, -Math.PI, Math.PI); state.yaw += yaw; state.pitch = Math.max(-1.3, Math.min(1.3, state.pitch + pitch)); if (drag)
            velocity = yaw * .5555555556; },
        setDragging(value) { if (disposed)
            return; drag = value; if (value) {
            velocity = 0;
            pointerLight.targetAlpha = 0;
        } },
        pointer(x, y) { if (disposed || drag || !Number.isFinite(x) || !Number.isFinite(y))
            return; pointerLight.targetX = x; pointerLight.targetY = y; pointerLight.targetAlpha = 1; state.hover = -1; if (pointerLight.alpha < .001 || reduced.matches) {
            pointerLight.x = x;
            pointerLight.y = y;
        } if (reduced.matches)
            pointerLight.alpha = 1; },
        clearPointer() { pointerLight.targetAlpha = 0; if (reduced.matches)
            pointerLight.alpha = 0; state.hover = -1; },
        selectAt(x, y) { if (disposed || !Number.isFinite(x) || !Number.isFinite(y))
            return false; const nearest = closest({ x, y }); if (nearest < 0)
            return false; routeFrom(nearest); sendPulse(); return true; },
        nextNode() { if (disposed)
            return; const visible = projected.filter(p => p.z > -.1).sort((a, b) => a.id - b.id), next = visible.find(p => p.id > state.origin) || visible[0]; if (next)
            routeFrom(next.id); },
        pulse: sendPulse,
        zoomBy(delta) { if (!disposed)
            state.zoom = finite(state.zoom + finite(delta, 0, -1, 1), state.zoom, .65, 1.25); },
        resetView() { if (disposed)
            return; state.yaw = .3; state.pitch = -.1; state.zoom = 1; state.hover = -1; velocity = 0; },
        startDelegation: fireDelegation, deliverResult: returnDelegation, finishDelegation, resetDelegations: resetDelegationDemo,
        dispose() { if (disposed)
            return; disposed = true; voiceSignals.packets = []; delegationDemo.tasks = []; seenIds.clear(); pulse = undefined; projected = []; }
    };
}
