import type { ArtifactImageRef } from "../../types.js";

const MAX_BYTES = 8 * 1024 * 1024;
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
export type ArtifactState = { artifacts?: ArtifactImageRef[]; artifactError?: string };

/** Artifact references may only address immutable image parts on the ngn host. */
export function artifactImages(value: unknown): ArtifactImageRef[] {
  if (value === undefined) return [];
  if (!Array.isArray(value)) throw new Error("Image artifact metadata is invalid.");
  if (value.length > 3) throw new Error("Image artifacts exceed the limit of 3 images.");
  let bytes = 0;
  const sizes = new Map<string, number>();
  return value.map((raw: unknown) => {
    if (!object(raw) || raw.type !== "artifact_image" || typeof raw.artifact_id !== "string" || !raw.artifact_id ||
      typeof raw.filename !== "string" || !raw.filename || raw.media_type !== "image/png" && raw.media_type !== "image/jpeg" ||
      typeof raw.byte_length !== "number" || !Number.isSafeInteger(raw.byte_length) || raw.byte_length <= 0 || raw.byte_length > MAX_BYTES ||
      raw.artifact_part !== undefined && (typeof raw.artifact_part !== "number" || !Number.isInteger(raw.artifact_part) || raw.artifact_part < 0 || raw.artifact_part > 255) ||
      raw.caption !== undefined && typeof raw.caption !== "string") throw new Error("Image artifact type, size or metadata is invalid.");
    const route = typeof raw.path === "string" && /^\/api\/sessions\/[A-Za-z0-9_-]+\/artifacts\/[1-9]\d*\/(0|[1-9]\d{0,2})$/.exec(raw.path);
    if (!route || Number(route[1]) > 255) throw new Error("Image artifact must use a local ngn artifact route.");
    if (!sizes.has(raw.path as string)) bytes += raw.byte_length;
    sizes.set(raw.path as string, raw.byte_length);
    if (bytes > MAX_BYTES) throw new Error("Image artifacts exceed the total 8 MiB limit.");
    return { type: "artifact_image", artifact_id: raw.artifact_id, artifact_part: typeof raw.artifact_part === "number" ? raw.artifact_part : 0,
      filename: raw.filename, media_type: raw.media_type as ArtifactImageRef["media_type"], byte_length: raw.byte_length,
      path: raw.path as string, caption: typeof raw.caption === "string" ? raw.caption : "" };
  });
}
export function artifactParts(value: unknown): unknown[] {
  return Array.isArray(value) ? value.filter(part => object(part) && part.type === "artifact_image") : [];
}
/** Repeated streams and saved copies enrich one message, keyed by its part route. */
export function artifactState(value: unknown, previous?: ArtifactImageRef[]): ArtifactState {
  try {
    const incoming = artifactImages(value);
    const merged = new Map((previous || []).map(part => [part.path, part]));
    for (const part of incoming) merged.set(part.path, part);
    const artifacts = artifactImages([...merged.values()]);
    return { artifacts: !artifacts.length ? undefined : JSON.stringify(artifacts) === JSON.stringify(previous) ? previous : artifacts,
      artifactError: undefined };
  } catch (error) {
    return { artifacts: previous, artifactError: error instanceof Error ? error.message : "Image artifact metadata is invalid." };
  }
}
