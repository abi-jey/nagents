import type { LivePhase } from "./types.js";

export interface SphereScene {
  phase: LivePhase;
  muted: boolean;
  busy: boolean;
  disabled: boolean;
  input: number;
  output: number;
  x: number;
  y: number;
  hover: boolean;
  pressed: boolean;
}

export interface SphereFrame {
  time: number;
  motion: number;
  input: number;
  output: number;
  x: number;
  y: number;
  hover: number;
  pressed: number;
}

export interface SpherePainter {
  paint(scene: SphereScene, frame: SphereFrame): void;
  dispose(): void;
}

const vertexSource = `
attribute vec2 position;
void main() { gl_Position = vec4(position, 0.0, 1.0); }
`;

// A real curved surface, not a texture spinning on a flat circle. The shape is
// softly displaced while a broad, flowing pigment field lives in object space.
const fragmentSource = `
precision highp float;
uniform vec2 resolution;
uniform float clock;
uniform vec2 levels;
uniform vec3 pointer;
uniform float press;
uniform float motion;
uniform float mode;
uniform float muted;
uniform float busy;

float hash(vec3 p) {
  p = fract(p * 0.3183099 + vec3(0.11, 0.17, 0.23));
  p *= 17.0;
  return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
}
float noise(vec3 p) {
  vec3 i = floor(p), f = fract(p);
  f = f*f*(3.0-2.0*f);
  return mix(mix(mix(hash(i),hash(i+vec3(1,0,0)),f.x),mix(hash(i+vec3(0,1,0)),hash(i+vec3(1,1,0)),f.x),f.y),
             mix(mix(hash(i+vec3(0,0,1)),hash(i+vec3(1,0,1)),f.x),mix(hash(i+vec3(0,1,1)),hash(i+vec3(1,1,1)),f.x),f.y),f.z);
}
float field(vec3 p) { return noise(p)*0.68 + noise(p*2.03+5.7)*0.23 + noise(p*4.11+9.2)*0.09; }
mat2 rotate(float a) { float c=cos(a),s=sin(a); return mat2(c,-s,s,c); }
float surface(vec3 p) {
  p.xy -= pointer.xy * vec2(0.046,-0.038) * motion;
  p.x /= 1.0 + press*0.12*motion;
  p.y /= 1.0 - press*0.10*motion;
  float energy = max(levels.x,levels.y)*motion;
  float t = clock * 0.62;
  float wave = sin(p.x*3.8+t)*cos(p.y*3.0-t*0.73)*sin(p.z*3.2+t*0.51);
  float ripple = sin(p.x*7.0-t*1.6)*sin(p.y*5.0+t*0.9)*cos(p.z*5.0-t);
  vec3 attract = normalize(vec3(pointer.x*0.8,-pointer.y*0.8,0.8));
  float reach = pow(max(0.0,dot(normalize(p+0.00001),attract)),7.0);
  float radius = 0.805 + wave*(0.026+energy*0.055) + ripple*energy*0.018;
  radius += reach*pointer.z*0.036*motion;
  return length(p)-radius;
}
vec3 normalAt(vec3 p) {
  vec2 e=vec2(0.003,-0.003);
  return normalize(e.xyy*surface(p+e.xyy)+e.yyx*surface(p+e.yyx)+e.yxy*surface(p+e.yxy)+e.xxx*surface(p+e.xxx));
}
void main() {
  vec2 uv = (gl_FragCoord.xy*2.0-resolution.xy)/min(resolution.x,resolution.y);
  vec3 origin=vec3(uv,2.6), ray=vec3(0,0,-1);
  float distanceAlong=0.0, nearest=10.0;
  vec3 point=origin, closest=origin;
  for(int i=0;i<40;i++) {
    point=origin+ray*distanceAlong;
    float d=surface(point);
    if(d<nearest) { nearest=d; closest=point; }
    if(d<0.0012 || distanceAlong>4.0) break;
    distanceAlong += max(d*0.85,0.001);
  }
  float pixel=2.0/min(resolution.x,resolution.y);
  float coverage=1.0-smoothstep(0.001,0.001+pixel*1.2,nearest);
  float halo=exp(-pow(length(uv)/0.87,4.0))*0.045;
  vec3 color=vec3(0.37,0.68,0.64);
  float alpha=halo;
  if(coverage>0.001) {
    point=closest;
    vec3 n=normalAt(point);
    vec3 p=point;
    p.xz=rotate(clock*0.12+pointer.x*0.12*motion)*p.xz;
    p.yz=rotate(-0.4+pointer.y*0.10*motion)*p.yz;
    vec3 drift=vec3(clock*0.11,-clock*0.09,clock*0.07);
    vec3 warp=vec3(field(p*1.8+drift),field(p*1.8+drift+13.0),field(p*1.8+drift+29.0));
    float cloud=field(p*1.35+warp*0.85+drift);
    float bloom=field(p*0.9-warp*0.55-drift*0.55+7.0);
    vec3 sea=vec3(0.36,0.69,0.65), pearl=vec3(0.92,0.91,0.79);
    vec3 violet=vec3(0.58,0.55,0.78), dusk=vec3(0.24,0.38,0.53);
    float sweep=0.5+0.33*n.y-0.19*n.x+(cloud-0.5)*0.30;
    vec3 pigment=mix(dusk,sea,smoothstep(0.08,0.83,sweep));
    pigment=mix(pigment,violet,smoothstep(-0.35,1.0,n.x+(bloom-0.5)*0.35)*0.59);
    pigment=mix(pigment,pearl,pow(max(0.0,dot(n,normalize(vec3(-0.8,0.7,0.4)))),3.0)*0.28);
    pigment+=(cloud-0.5)*vec3(0.06,0.09,0.10);
    vec3 light=normalize(vec3(-0.65,0.88,1.15));
    float diffuse=max(0.0,dot(n,light));
    float wrap=max(0.0,dot(n,normalize(vec3(0.85,-0.24,0.6))));
    float facing=max(0.0,n.z), fresnel=pow(1.0-facing,2.4);
    vec3 halfLight=normalize(light+vec3(0,0,1));
    float gloss=pow(max(0.0,dot(n,halfLight)),25.0);
    float softLight=pow(max(0.0,dot(n,halfLight)),5.0);
    vec3 sheen=mix(vec3(0.55,0.77,0.92),vec3(0.87,0.73,0.83),cloud);
    color=pigment*(0.47+diffuse*0.68)+vec3(0.27,0.42,0.54)*wrap*0.13;
    color+=vec3(0.88,1.0,0.93)*(gloss*0.20+softLight*0.07);
    color+=sheen*fresnel*(0.27+levels.y*0.08);
    color+=vec3(0.33,0.68,0.61)*pow(facing,2.0)*levels.y*0.08;
    float luminance=dot(color,vec3(0.2126,0.7152,0.0722));
    color=mix(color,vec3(luminance)*vec3(0.95,1.02,1.04),muted*0.46);
    color*=mix(0.87,1.0,mode);
    color=mix(color,color*vec3(1.15,0.86,0.93),step(1.5,mode)*0.5);
    color=pow(clamp(color,0.0,1.0),vec3(0.86));
    alpha=coverage+halo*(1.0-coverage);
  }
  gl_FragColor=vec4(color,alpha);
}
`;

function shader(gl: WebGLRenderingContext, kind: number, source: string): WebGLShader {
  const result = gl.createShader(kind);
  if (!result) throw new Error("Shader unavailable");
  gl.shaderSource(result, source); gl.compileShader(result);
  if (!gl.getShaderParameter(result, gl.COMPILE_STATUS)) { gl.deleteShader(result); throw new Error("Shader unsupported"); }
  return result;
}

export function createSpherePainter(canvas: HTMLCanvasElement): SpherePainter | undefined {
  const view = canvas.ownerDocument.defaultView;
  if (!view || typeof view.WebGLRenderingContext === "undefined") return undefined;
  const gl = canvas.getContext("webgl", { alpha: true, antialias: false, depth: false, stencil: false, premultipliedAlpha: false, powerPreference: "low-power" });
  if (!gl) return undefined;
  const program = gl.createProgram(), buffer = gl.createBuffer();
  let vertex: WebGLShader | undefined, fragment: WebGLShader | undefined;
  const dispose = () => { gl.deleteBuffer(buffer); gl.deleteProgram(program); };
  try {
    if (!program || !buffer) throw new Error("Renderer unavailable");
    vertex = shader(gl, gl.VERTEX_SHADER, vertexSource); fragment = shader(gl, gl.FRAGMENT_SHADER, fragmentSource);
    gl.attachShader(program, vertex); gl.attachShader(program, fragment); gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error("Renderer unsupported");
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(program, "position");
    const uniform: Record<string, WebGLUniformLocation | null> = Object.fromEntries(["resolution", "clock", "levels", "pointer", "press", "motion", "mode", "muted", "busy"]
      .map((name) => [name, gl.getUniformLocation(program, name)] as const));
    return {
      paint(scene, frame) {
        gl.viewport(0, 0, canvas.width, canvas.height); gl.useProgram(program);
        gl.bindBuffer(gl.ARRAY_BUFFER, buffer); gl.enableVertexAttribArray(position); gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
        gl.uniform2f(uniform.resolution, canvas.width, canvas.height);
        gl.uniform1f(uniform.clock, frame.time);
        gl.uniform2f(uniform.levels, frame.input, frame.output);
        gl.uniform3f(uniform.pointer, frame.x, frame.y, frame.hover);
        gl.uniform1f(uniform.press, frame.pressed); gl.uniform1f(uniform.motion, frame.motion);
        gl.uniform1f(uniform.mode, scene.phase === "error" ? 2 : scene.phase === "connected" ? 1 : 0.35);
        gl.uniform1f(uniform.muted, scene.muted ? 1 : 0); gl.uniform1f(uniform.busy, scene.busy ? 1 : 0);
        gl.drawArrays(gl.TRIANGLES, 0, 6);
      },
      dispose,
    };
  } catch { dispose(); return undefined; }
  finally { if (vertex) gl.deleteShader(vertex); if (fragment) gl.deleteShader(fragment); }
}

export const SPHERE_FRAME_MS = 1000 / 30;
export const SPHERE_MAX_PIXELS = 256;

export function mountSphereRenderer(canvas: HTMLCanvasElement, read: () => SphereScene,
  available: (ready: boolean) => void, makePainter = createSpherePainter): { refresh(): void; dispose(): void } {
  const view = canvas.ownerDocument.defaultView, document = canvas.ownerDocument;
  if (!view) return { refresh() {}, dispose() {} };
  let painter: SpherePainter | undefined;
  try { painter = makePainter(canvas); } catch { /* Keep the static, accessible fallback. */ }
  if (!painter) { available(false); return { refresh() {}, dispose() {} }; }
  const media = view.matchMedia?.("(prefers-reduced-motion: reduce)");
  let reduced = media?.matches || false, visible = true, disposed = false, lost = false;
  let frameId: number | undefined, last = -Infinity, time = 1.7, staticKey = "", reported = false;
  const current: SphereFrame = { time, motion: reduced ? 0 : 1, input: 0, output: 0, x: 0, y: 0, hover: 0, pressed: 0 };

  function size() {
    const bounds = canvas.getBoundingClientRect(), ratio = Math.min(2, view!.devicePixelRatio || 1);
    const width = Math.max(1, Math.min(SPHERE_MAX_PIXELS, Math.round(bounds.width * ratio)));
    const height = Math.max(1, Math.min(SPHERE_MAX_PIXELS, Math.round(bounds.height * ratio)));
    if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; staticKey = ""; }
  }
  function render(now: number, force = false) {
    if (disposed || lost || !visible || document.hidden || !painter) return;
    const scene = read(), key = `${scene.phase}:${scene.muted}:${scene.busy}:${scene.disabled}`;
    if (reduced && key === staticKey && !force) return;
    const elapsed = Number.isFinite(last) ? Math.min(0.1, Math.max(0, (now - last) / 1000)) : 1 / 30;
    const blend = 1 - Math.exp(-elapsed * 10);
    if (!reduced) time += elapsed * (scene.busy ? 0.92 : 0.6);
    current.time = reduced ? 1.7 : time; current.motion = reduced ? 0 : 1;
    for (const field of ["input", "output", "x", "y"] as const) current[field] += ((reduced ? 0 : scene[field]) - current[field]) * blend;
    current.hover += ((reduced ? 0 : Number(scene.hover)) - current.hover) * blend;
    current.pressed += ((reduced ? 0 : Number(scene.pressed)) - current.pressed) * Math.min(1, blend * 1.8);
    painter.paint(scene, current); staticKey = key; last = now;
    if (!reported) { reported = true; available(true); }
  }
  function stop() { if (frameId !== undefined) view!.cancelAnimationFrame(frameId); frameId = undefined; }
  function tick(now: number) {
    frameId = undefined;
    if (disposed || lost || !visible || document.hidden || reduced) return;
    if (now - last >= SPHERE_FRAME_MS - 0.1) render(now);
    frameId = view!.requestAnimationFrame(tick);
  }
  function refresh() {
    stop();
    if (disposed || lost || !visible || document.hidden) return;
    size();
    const now = view!.performance.now();
    if (reduced || !staticKey || now - last >= SPHERE_FRAME_MS - 0.1) render(now);
    if (!reduced && typeof view!.requestAnimationFrame === "function") frameId = view!.requestAnimationFrame(tick);
  }
  function preference() { reduced = media?.matches || false; staticKey = ""; refresh(); }
  function contextLost(event: Event) {
    event.preventDefault(); lost = true; stop(); reported = false;
    // Objects from a lost context are invalid after restoration. Retire them
    // now, while WebGL safely ignores deletion, rather than reusing old handles.
    painter?.dispose(); painter = undefined; available(false);
  }
  function contextRestored() {
    painter?.dispose();
    try { painter = makePainter(canvas); } catch { painter = undefined; }
    lost = !painter; staticKey = ""; refresh();
  }
  const intersection = typeof view.IntersectionObserver === "function" ? new view.IntersectionObserver((entries) => {
    visible = entries.some((entry) => entry.target === canvas && entry.isIntersecting); refresh();
  }) : undefined;
  const resize = typeof view.ResizeObserver === "function" ? new view.ResizeObserver(refresh) : undefined;
  intersection?.observe(canvas); resize?.observe(canvas);
  document.addEventListener("visibilitychange", refresh); media?.addEventListener("change", preference);
  view.addEventListener("resize", refresh);
  canvas.addEventListener("webglcontextlost", contextLost); canvas.addEventListener("webglcontextrestored", contextRestored);
  refresh();
  return { refresh, dispose() {
    if (disposed) return;
    disposed = true; stop(); painter?.dispose(); painter = undefined;
    intersection?.disconnect(); resize?.disconnect();
    document.removeEventListener("visibilitychange", refresh); media?.removeEventListener("change", preference);
    view.removeEventListener("resize", refresh);
    canvas.removeEventListener("webglcontextlost", contextLost); canvas.removeEventListener("webglcontextrestored", contextRestored);
  } };
}
