export function TestPrompt({ value, target, canSend, change, send }: {
  value: string; target: string; canSend: boolean; change: (value: string) => void; send: () => void;
}) {
  return <textarea placeholder={`Message ${target}…`} value={value}
    aria-keyshortcuts="Control+Enter Meta+Enter" title="Ctrl+Enter or ⌘+Enter to send; Enter for a new line"
    onChange={(event) => change(event.target.value)}
    onKeyDown={(event) => {
      if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey) || event.shiftKey || event.altKey || event.nativeEvent.isComposing) return;
      event.preventDefault();
      if (!event.repeat && canSend && value.trim()) send();
    }} />;
}
