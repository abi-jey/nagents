import assert from "node:assert/strict";
import { test } from "node:test";
import { createSphereEngine } from "./engine";
import { ico, length, transitDisplacement, transitField, vec } from "./geometry";
import { densityProfiles, silentSignal } from "./types";
import type { AudioFrame, SphereConfig, SphereEngine } from "./types";
const config: SphereConfig = { mode: "listen", playing: true, density: 3, speed: 1, thinkMin: .45, thinkMax: 1.65, glow: .65, color: "mint", dark: true, reducedMotion: false };
const silent = (): AudioFrame => ({ input: silentSignal(), output: silentSignal() });
const voiced = (): AudioFrame => ({ input: { active: true, rms: .13, low: .04, mid: .03, high: .004 }, output: { active: true, rms: .2, low: .05, mid: .025, high: .003 } });
function advance(engine: SphereEngine, seconds: number, audio = silent()): void { for (let elapsed = 0; elapsed < seconds; elapsed += 1 / 60)
    engine.tick(1 / 60, audio); }
function surface(): {
    context: CanvasRenderingContext2D;
    count: () => number;
    radii: () => readonly number[];
    coordinates: () => readonly number[];
    reset: () => void;
} {
    let commands = 0;
    const radii: number[] = [], coordinates: number[] = [];
    const finite = (...values: number[]): void => { commands++; assert(values.every(Number.isFinite)); coordinates.push(...values); };
    const context: Partial<CanvasRenderingContext2D> = {
        fillStyle: "#000", strokeStyle: "#000", lineWidth: 1,
        clearRect: finite, fillRect: finite, moveTo: finite, lineTo: finite,
        beginPath() { commands++; }, stroke() { commands++; }, fill() { commands++; },
        arc(x, y, radius, start, end) { finite(x, y, radius, start, end); assert(radius >= 0); },
        createRadialGradient(x0, y0, r0, x1, y1, r1) {
            finite(x0, y0, r0, x1, y1, r1);
            radii.push(r1);
            assert(r0 >= 0 && r1 >= 0);
            return { addColorStop(offset, color) { assert(offset >= 0 && offset <= 1); assert(!color.includes("NaN")); } };
        },
    };
    return { context: context as CanvasRenderingContext2D, count: () => commands, radii: () => radii, coordinates: () => coordinates, reset() { commands = 0; radii.length = 0; coordinates.length = 0; } };
}
test("all seven densities form closed shells and render finite main and compact frames", () => {
    const counts = [54, 114, 204, 444, 804, 1284, 1764];
    const engine = createSphereEngine(config), drawing = surface();
    for (let density = 1; density <= 7; density++) {
        engine.configure({ ...config, density });
        engine.tick(1 / 60, silent());
        assert.equal(engine.snapshot().nodes, counts[density - 1]);
        engine.render(drawing.context, 720, 430);
        engine.render(drawing.context, 80, 80, true);
        const shell = ico(densityProfiles[density - 1].surface), incidence = new Map<string, number>();
        for (const face of shell.faces)
            for (let i = 0; i < 3; i++) {
                const a = face[i], b = face[(i + 1) % 3], key = `${Math.min(a, b)}:${Math.max(a, b)}`;
                incidence.set(key, (incidence.get(key) ?? 0) + 1);
            }
        assert([...incidence.values()].every(value => value === 2));
    }
    assert(drawing.count() > 1000);
    engine.dispose();
});
test("a fast host result is queued once and survives a density change", () => {
    const engine = createSphereEngine(config);
    assert(engine.startDelegation("research-42", "Research"));
    assert(engine.deliverResult("research-42"));
    assert.equal(engine.deliverResult("research-42"), false);
    engine.configure({ ...config, density: 6 });
    assert.deepEqual(engine.snapshot().tasks.map(({ id, label, phase }) => ({ id, label, phase })), [{ id: "research-42", label: "Research", phase: "launch" }]);
    advance(engine, 4);
    assert.equal(engine.snapshot().tasks[0].phase, "complete");
    assert.equal(engine.startDelegation("research-42"), false);
    assert.equal(engine.deliverResult("unknown"), false);
    assert.equal(engine.deliverResult("research-42"), false);
    engine.resetDelegations();
    assert(engine.startDelegation("research-42"));
    engine.dispose();
});
test("task capacity, pending and return state persist across scene reconfiguration", () => {
    const engine = createSphereEngine(config);
    assert(engine.startDelegation("a"));
    assert(engine.startDelegation("b"));
    assert(engine.startDelegation("c"));
    assert.equal(engine.startDelegation("d"), false);
    advance(engine, 2.2);
    assert(engine.snapshot().tasks.every(task => task.phase === "pending"));
    assert(engine.deliverResult("b"));
    engine.configure({ ...config, density: 1, dark: false });
    assert.deepEqual(engine.snapshot().tasks.map(task => task.phase), ["pending", "return", "pending"]);
    advance(engine, 1.4);
    assert.equal(engine.snapshot().tasks.find(task => task.id === "b")?.phase, "complete");
    assert(engine.startDelegation("d"));
    assert.equal(engine.startDelegation("b"), false);
    engine.dispose();
});
test("microphone input mixes with all activities without fabricating idle audio", () => {
    const engine = createSphereEngine(config);
    advance(engine, .5);
    assert.equal(engine.snapshot().ambientPackets, 3);
    assert.equal(engine.snapshot().voicePackets, 0);
    assert.equal(engine.snapshot().inputLevel, 0);
    for (const mode of ["listen", "think", "speak", "delegate"] as const) {
        engine.configure({ ...config, mode });
        for (let frame = 0; frame < 180; frame++)
            engine.tick(1 / 60, frame % 20 < 8 ? voiced() : { ...voiced(), input: { ...voiced().input, rms: .001 }, output: { ...voiced().output, rms: .001 } });
        const result = engine.snapshot();
        assert(result.inputLevel > 0);
        assert(result.ambientPackets + result.voicePackets <= 8);
        assert(result.shock <= .04500001);
        if (mode === "speak")
            assert(result.outputLevel > 0);
    }
    engine.dispose();
});
test("instances are isolated, disposed engines do not mutate, and snapshots are detached", () => {
    const first = createSphereEngine(config), second = createSphereEngine(config);
    first.startDelegation("one");
    first.zoomBy(.2);
    first.pulse(10);
    assert.equal(second.snapshot().tasks.length, 0);
    assert.equal(second.snapshot().zoom, 1);
    const detached = first.snapshot();
    detached.tasks[0].label = "mutated caller copy";
    assert.notEqual(first.snapshot().tasks[0].label, detached.tasks[0].label);
    first.dispose();
    const after = first.snapshot();
    first.tick(1, voiced());
    first.configure({ ...config, density: 7 });
    first.pulse();
    first.zoomBy(-.2);
    assert.deepEqual(first.snapshot(), after);
    assert.equal(first.startDelegation("after"), false);
    assert.equal(first.deliverResult("one"), false);
    second.dispose();
});
test("nonfinite input stays finite and reduced motion clears movement", () => {
    const engine = createSphereEngine({ ...config, density: Infinity, speed: NaN, glow: Infinity, thinkMin: NaN });
    const malformed = { active: true, rms: NaN, low: Infinity, mid: -Infinity, high: NaN };
    advance(engine, .5, { input: malformed, output: malformed });
    assert.equal(engine.snapshot().nodes, 204);
    assert.equal(engine.snapshot().inputLevel, 0);
    assert(Number.isFinite(engine.snapshot().outerScale));
    engine.configure({ ...config, reducedMotion: true });
    advance(engine, 1, voiced());
    assert.equal(engine.snapshot().shock, 0);
    assert.equal(engine.snapshot().ambientPackets, 0);
    assert.equal(engine.snapshot().voicePackets, 0);
    assert.equal(engine.snapshot().outerScale, 1);
    assert(engine.startDelegation("reduced"));
    assert.equal(engine.snapshot().tasks[0].phase, "pending");
    assert(engine.deliverResult("reduced"));
    assert.equal(engine.snapshot().tasks[0].phase, "complete");
    const drawing = surface();
    engine.render(drawing.context, 720, 430);
    engine.dispose();
});

test("actual ambient and explicit delegation launches use the same bloom family", () => {
    for (const [mode, expected] of [["listen", 3], ["think", 5], ["delegate", 4]] as const) {
        const engine = createSphereEngine({ ...config, mode }), drawing = surface();
        engine.tick(.000001, silent());
        engine.render(drawing.context, 720, 430);
        assert.equal(drawing.radii().filter(radius => Math.abs(radius - 12) < 1e-9).length, expected, `${mode} must visibly bloom at every source`);
        assert(engine.snapshot().shock > 0);
        engine.dispose();
    }
    const engine = createSphereEngine({ ...config, mode: "speak" }), drawing = surface();
    engine.startDelegation("same-family");
    engine.render(drawing.context, 720, 430);
    assert.equal(drawing.radii().filter(radius => Math.abs(radius - 12) < 1e-9).length, 1);
    assert(engine.snapshot().shock > 0);
    engine.dispose();
});

test("the common transit field is local, follows its edge and preserves equal-energy response", () => {
    const from = vec(-.1, 0, .56), to = vec(.1, 0, .56), center = vec(0, 0, .56);
    const outgoing = transitField(from, to, .5, .55, .65);
    const returning = transitField(to, from, .5, .55, .65);
    const forward = transitDisplacement(center, [outgoing]), backward = transitDisplacement(center, [returning]);
    assert(length(forward) > .02 && length(forward) < .03);
    assert(Math.abs(forward.x + backward.x) < 1e-12, "same energy on either signal direction has equal displacement");
    assert.equal(forward.y, 0); assert.equal(forward.z, 0);
    assert.equal(length(transitDisplacement(vec(0, 1, 0), [outgoing])), 0, "distant nodes must not join the impact");
    assert.equal(length(transitDisplacement(center, [transitField(from, to, .5, .55, .65, 0)])), 0);
});

test("shared transit and arrival movement stays bounded at every density", () => {
    for (let density = 1; density <= 7; density++) {
        const engine = createSphereEngine({ ...config, density, mode: "speak" }), drawing = surface();
        engine.startDelegation(`outbound-${density}`); engine.deliverResult(`outbound-${density}`);
        engine.startDelegation(`other-${density}`);
        for (let frame = 0; frame < 210; frame++) {
            engine.tick(1 / 60, voiced());
            const snapshot = engine.snapshot();
            assert(Number.isFinite(snapshot.shock) && snapshot.shock <= .04500001);
            assert(snapshot.ambientPackets + snapshot.voicePackets <= 8);
        }
        engine.render(drawing.context, 720, 430); engine.render(drawing.context, 80, 80, true);
        assert.equal(engine.snapshot().tasks[0].phase, "complete");
        engine.dispose();
    }
});

test("a pulse requested while paused does not resume simulation", () => {
    const engine = createSphereEngine({ ...config, playing: false }), drawing = surface();
    engine.pulse(10); engine.render(drawing.context, 720, 430);
    const before = [...drawing.coordinates()]; drawing.reset();
    advance(engine, .5); engine.render(drawing.context, 720, 430);
    assert.deepEqual(drawing.coordinates(), before, "paused pulse must not restart rotation or movement");
    assert.equal(engine.snapshot().ambientPackets, 0); assert.equal(engine.snapshot().voicePackets, 0);
    engine.configure(config); engine.tick(1 / 60, silent());
    assert.equal(engine.snapshot().ambientPackets, 3); assert(engine.snapshot().shock > 0);
    engine.dispose();
});
