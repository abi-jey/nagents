/** A single team mark, drawn for ngn's 16–20 px navigation controls. */
export function TeamIcon({ size = 18 }: { size?: number }) {
  return <svg className="team-icon" width={size} height={size} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.55" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="10" cy="5.5" r="2.4" />
    <path d="M6 17v-1.8a4 4 0 0 1 8 0V17M4.2 5a2.2 2.2 0 0 0 0 4.4M15.8 5a2.2 2.2 0 0 1 0 4.4M2 16v-1.3a3 3 0 0 1 1.8-2.7M18 16v-1.3a3 3 0 0 0-1.8-2.7" />
  </svg>;
}
