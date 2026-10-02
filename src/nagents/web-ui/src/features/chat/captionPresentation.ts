import { groupLiveCaptions } from "./transcript.js";
import type { Entry } from "./transcript.js";

export function conversationEntries(entries: readonly Entry[]): Entry[] {
  const spoken = new Set(entries.flatMap(entry => entry.liveCaption?.speaker === "user" ? [entry.liveCaption.sessionId] : []));
  // The assistant's admitted voice request carries the same verified call ID.
  // Its model context stays intact; show the caller's actual captions once.
  return groupLiveCaptions(entries.filter(entry => !(entry.kind === "user" && entry.voice && entry.voiceSessionId && spoken.has(entry.voiceSessionId))));
}

export function captionTime(milliseconds: number): string {
  const seconds = Math.floor(milliseconds / 1000);
  return `${Math.floor(seconds / 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`;
}
