type IconName = "plus" | "chat" | "trash" | "settings" | "channels" | "folder" | "close" | "menu" | "chevron" | "more" | "tools" | "wave" | "mic" | "mic-off" | "volume" | "volume-off" | "stop" | "paperclip" | "search" | "edit" | "branch";

const paths: Record<IconName, string> = {
  branch: "M5 3v10a3 3 0 0 0 3 3h7M5 8h5a5 5 0 0 0 5-5M3 3h4M13 3h4M15 14l2 2-2 2",
  plus: "M10 4v12M4 10h12",
  chat: "M4 3h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H8l-5 3V5a2 2 0 0 1 1-2Z",
  trash: "M3 5h14M7 5V3h6v2M5 5l1 12h8l1-12M8 8v6M12 8v6",
  settings: "M3 5h14M3 10h14M3 15h14M7 3v4M13 8v4M8 13v4",
  channels: "M3 7h4v6H3zM13 2h4v5h-4zM13 13h4v5h-4zM7 10h3V5h3M10 10v5h3",
  folder: "M2 5a1 1 0 0 1 1-1h5l2 2h7a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1Z",
  close: "m5 5 10 10M15 5 5 15",
  menu: "M3 5h14M3 10h14M3 15h14",
  chevron: "m7 4 6 6-6 6",
  more: "M4 10h.01M10 10h.01M16 10h.01",
  tools: "M12 3a5 5 0 0 0-6 6L2.5 13.5a2 2 0 0 0 4 4L11 13a5 5 0 0 0 6-6l-3 3-4-4Z",
  wave: "M2 8v4M6 5v10M10 2v16M14 5v10M18 8v4",
  mic: "M7 5a3 3 0 0 1 6 0v5a3 3 0 0 1-6 0ZM4 9v1a6 6 0 0 0 12 0V9M10 16v3M7 19h6",
  "mic-off": "m2 2 16 16M7 7v3a3 3 0 0 0 5 2M8 2.7A3 3 0 0 1 13 5v3M4 9v1a6 6 0 0 0 10 4M16 9v1M10 16v3M7 19h6",
  volume: "M3 7h3l4-4v14l-4-4H3ZM13 6a6 6 0 0 1 0 8M15 3a10 10 0 0 1 0 14",
  "volume-off": "M3 7h3l4-4v14l-4-4H3Zm10 0 5 6M18 7l-5 6",
  stop: "M5 5h10v10H5Z",
  paperclip: "m7 10 5.5-5.5a2.5 2.5 0 0 1 3.5 3.5l-7 7a4 4 0 0 1-5.7-5.7l7-7M6.2 11.8l5.7-5.7",
  search: "m14 14 4 4M16 9a7 7 0 1 1-14 0 7 7 0 0 1 14 0Z",
  edit: "m12 3 5 5M3 17l4-1L17 6a2 2 0 0 0-4-4L3 12Z",
};

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 20 20" fill="none" stroke="currentColor"
    strokeWidth="1.55" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d={paths[name]} />
  </svg>;
}
