type IconName = "plus" | "chat" | "trash" | "settings" | "channels" | "folder" | "close" | "menu" | "chevron";

const paths: Record<IconName, string> = {
  plus: "M10 4v12M4 10h12",
  chat: "M4 3h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H8l-5 3V5a2 2 0 0 1 1-2Z",
  trash: "M3 5h14M7 5V3h6v2M5 5l1 12h8l1-12M8 8v6M12 8v6",
  settings: "M3 5h14M3 10h14M3 15h14M7 3v4M13 8v4M8 13v4",
  channels: "M3 7h4v6H3zM13 2h4v5h-4zM13 13h4v5h-4zM7 10h3V5h3M10 10v5h3",
  folder: "M2 5a1 1 0 0 1 1-1h5l2 2h7a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1Z",
  close: "m5 5 10 10M15 5 5 15",
  menu: "M3 5h14M3 10h14M3 15h14",
  chevron: "m7 4 6 6-6 6",
};

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 20 20" fill="none" stroke="currentColor"
    strokeWidth="1.55" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d={paths[name]} />
  </svg>;
}
