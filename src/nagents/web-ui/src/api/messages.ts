import { request } from "./client.js";

export type QueuedMessage = { session_id: string; prompt: string; message_id: string };
export const MESSAGE_ID_RESERVE = "00000000-0000-4000-8000-000000000000";
export function queuedMessageFailure(prompt: string, sessionId: string): string {
  return new TextEncoder().encode(JSON.stringify({ session_id: sessionId, prompt, message_id: MESSAGE_ID_RESERVE })).byteLength > 65536
    ? "The queued message exceeds the 64 KiB request limit including its message ID. Shorten the draft; it is kept."
    : "";
}
export async function queueMessage(token: string, message: QueuedMessage): Promise<void> {
  // Deliberately independent of the subscription's AbortSignal: closing or changing
  // the viewed root must not abandon accepted server-owned work.
  const response = await request("messages", token, message);
  const reply: unknown = await response.json();
  if (!reply || typeof reply !== "object" || !("status" in reply) || reply.status !== "queued" ||
      !("session_id" in reply) || reply.session_id !== message.session_id ||
      !("message_id" in reply) || reply.message_id !== message.message_id)
    throw new Error("Message acknowledgement was not confirmed. Retry the same prompt to check its queued identity.");
}

export class MessageQueue {
  private uncertain = new Map<string, QueuedMessage>();
  prepare(sessionId: string, prompt: string, uuid: () => string = () => crypto.randomUUID()): QueuedMessage {
    const key = JSON.stringify([sessionId, prompt]);
    const message = this.uncertain.get(key) || { session_id: sessionId, prompt, message_id: uuid() };
    this.uncertain.set(key, message);
    return message;
  }
  confirmed(message: QueuedMessage) {
    this.uncertain.delete(JSON.stringify([message.session_id, message.prompt]));
  }
}
