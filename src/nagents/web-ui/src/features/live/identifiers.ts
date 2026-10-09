/** Provider IDs are opaque; local call/model IDs use their own stricter validators. */
export function providerDelegationId(value: unknown): value is string {
  return typeof value === "string" && !!value.trim() && Array.from(value).length <= 256;
}
