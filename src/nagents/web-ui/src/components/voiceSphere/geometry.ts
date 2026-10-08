export interface Vec3 {
    x: number;
    y: number;
    z: number;
}
export type Edge = [
    number,
    number
];
export type Face = [
    number,
    number,
    number
];
export type RGB = readonly [
    number,
    number,
    number
];
export interface Graph {
    nodes: Vec3[];
    edges: Edge[];
    faces: Face[];
}
export interface TransitField {
    head: Vec3;
    direction: Vec3;
    amplitude: number;
}
/** Every signal uses this same travelling deformation, regardless of its source. */
export function transitField(from: Vec3, to: Vec3, fraction: number, energy: number, impact = .65, fade = 1): TransitField {
    const t = Math.max(0, Math.min(1, fraction));
    const gain = (.7 + .3 * Math.max(0, Math.min(1, energy))) * Math.min(1.2, Math.max(0, impact) / .65);
    return {
        head: mix(from, to, t),
        direction: normalize(vec(to.x - from.x, to.y - from.y, to.z - from.z)),
        amplitude: .030 * gain * Math.max(0, Math.min(1, fade)) * (.30 + .70 * Math.sin(Math.PI * t)),
    };
}
export function transitDisplacement(point: Vec3, fields: readonly TransitField[]): Vec3 {
    const offset = vec(0, 0, 0);
    for (const field of fields) {
        const dx = point.x - field.head.x, dy = point.y - field.head.y, dz = point.z - field.head.z;
        const squared = dx * dx + dy * dy + dz * dz;
        if (squared > .30) continue;
        const amount = field.amplitude * Math.exp(-squared / .045);
        offset.x += field.direction.x * amount;
        offset.y += field.direction.y * amount;
        offset.z += field.direction.z * amount;
    }
    return offset;
}
export const vec = (x: number, y: number, z: number): Vec3 => ({ x, y, z });
export const scale = (v: Vec3, s: number): Vec3 => vec(v.x * s, v.y * s, v.z * s);
export const length = (v: Vec3): number => Math.hypot(v.x, v.y, v.z);
export const normalize = (v: Vec3): Vec3 => scale(v, 1 / Math.max(Number.EPSILON, length(v)));
export const dot = (a: Vec3, b: Vec3): number => a.x * b.x + a.y * b.y + a.z * b.z;
export const mix = (a: Vec3, b: Vec3, t: number): Vec3 => vec(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + (b.z - a.z) * t);
export const color = (rgb: RGB, alpha = 1): string => `rgba(${rgb.map(Math.round).join(",")},${Math.max(0, Math.min(1, alpha))})`;
export const community = (p: Vec3): number => Math.floor(((Math.atan2(p.z, p.x) + Math.PI) / (2 * Math.PI)) * 3) % 3;
/** Shared-edge refinement supports intermediate densities without cracks. */
export function ico(level: number, radius = 1): Graph {
    const phi = (1 + Math.sqrt(5)) / 2;
    const points = [vec(-1, phi, 0), vec(1, phi, 0), vec(-1, -phi, 0), vec(1, -phi, 0), vec(0, -1, phi), vec(0, 1, phi), vec(0, -1, -phi), vec(0, 1, -phi), vec(phi, 0, -1), vec(phi, 0, 1), vec(-phi, 0, -1), vec(-phi, 0, 1)].map(normalize);
    let faces: Face[] = [[0, 11, 5], [0, 5, 1], [0, 1, 7], [0, 7, 10], [0, 10, 11], [1, 5, 9], [5, 11, 4], [11, 10, 2], [10, 7, 6], [7, 1, 8], [3, 9, 4], [3, 4, 2], [3, 2, 6], [3, 6, 8], [3, 8, 9], [4, 9, 5], [2, 4, 11], [6, 2, 10], [8, 6, 7], [9, 8, 1]];
    const full = Math.floor(level), fraction = level - full;
    const keyOf = (a: number, b: number): string => `${Math.min(a, b)}:${Math.max(a, b)}`;
    for (let step = 0; step < full; step++) {
        const cache = new Map<string, number>();
        const midpoint = (a: number, b: number): number => {
            const key = keyOf(a, b), saved = cache.get(key);
            if (saved !== undefined)
                return saved;
            const i = points.length;
            points.push(normalize(mix(points[a], points[b], .5)));
            cache.set(key, i);
            return i;
        };
        const next: Face[] = [];
        for (const [a, b, c] of faces) {
            const ab = midpoint(a, b), bc = midpoint(b, c), ca = midpoint(c, a);
            next.push([a, ab, ca], [b, bc, ab], [c, ca, bc], [ab, bc, ca]);
        }
        faces = next;
    }
    if (fraction > 0) {
        const candidates = new Map<string, {
            a: number;
            b: number;
            rank: number;
        }>();
        for (const face of faces)
            for (let i = 0; i < 3; i++) {
                const a = Math.min(face[i], face[(i + 1) % 3]), b = Math.max(face[i], face[(i + 1) % 3]);
                candidates.set(keyOf(a, b), { a, b, rank: (Math.imul(a + 1, 73856093) ^ Math.imul(b + 1, 19349663)) >>> 0 });
            }
        const ranked = [...candidates.values()].sort((a, b) => a.rank - b.rank || a.a - b.a || a.b - b.b), midpoints = new Map<string, number>();
        for (const { a, b } of ranked.slice(0, Math.round(ranked.length * fraction))) {
            midpoints.set(keyOf(a, b), points.length);
            points.push(normalize(mix(points[a], points[b], .5)));
        }
        const next: Face[] = [];
        const squared = (a: number, b: number): number => { const p = points[a], q = points[b]; return (p.x - q.x) ** 2 + (p.y - q.y) ** 2 + (p.z - q.z) ** 2; };
        const splitTwo = (a: number, b: number, c: number, ab: number, bc: number): void => {
            next.push([b, bc, ab]);
            if (squared(a, bc) <= squared(ab, c))
                next.push([a, ab, bc], [a, bc, c]);
            else
                next.push([a, ab, c], [ab, bc, c]);
        };
        for (const [a, b, c] of faces) {
            const ab = midpoints.get(keyOf(a, b)), bc = midpoints.get(keyOf(b, c)), ca = midpoints.get(keyOf(c, a));
            if (ab !== undefined && bc !== undefined && ca !== undefined)
                next.push([a, ab, ca], [b, bc, ab], [c, ca, bc], [ab, bc, ca]);
            else if (ab !== undefined && bc !== undefined)
                splitTwo(a, b, c, ab, bc);
            else if (bc !== undefined && ca !== undefined)
                splitTwo(b, c, a, bc, ca);
            else if (ca !== undefined && ab !== undefined)
                splitTwo(c, a, b, ca, ab);
            else if (ab !== undefined)
                next.push([a, ab, c], [ab, b, c]);
            else if (bc !== undefined)
                next.push([b, bc, a], [bc, c, a]);
            else if (ca !== undefined)
                next.push([c, ca, b], [ca, a, b]);
            else
                next.push([a, b, c]);
        }
        faces = next;
    }
    const unique = new Map<string, Edge>();
    for (const face of faces)
        for (let i = 0; i < 3; i++) {
            const a = face[i], b = face[(i + 1) % 3];
            unique.set(keyOf(a, b), [a, b]);
        }
    return { nodes: points.map(p => scale(p, radius)), edges: [...unique.values()], faces };
}
