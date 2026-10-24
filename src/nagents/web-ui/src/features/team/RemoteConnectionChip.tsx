import { useRemoteAuthLabel, type RemoteAuthentication } from "./RemoteStatus.js";

export function remoteConnectionState(label: string): "ready" | "attention" | "unknown" {
  if (["Signed in", "Signed in · auto refresh", "Server token configured", "No sign-in required"].includes(label)) return "ready";
  return label === "Auth status unavailable" ? "unknown" : "attention";
}

export function RemoteConnectionChip({ authentication, name, activate }: {
  authentication?: RemoteAuthentication; name: string; activate: () => void;
}) {
  const label = useRemoteAuthLabel(authentication);
  const state = remoteConnectionState(label);
  const ready = state === "ready";
  const action = label === "Sign-in required" ? `Sign in to ${name}. Open connection settings.` : `Open ${name} connection settings.`;
  return <button type="button" className="remote-connection-chip" data-state={state}
    title={`A2A · ${label} · Open connection settings`} aria-label={`A2A · ${label}. ${action}`} onClick={activate}>
    <span>A2A</span><span className="remote-connection-separator" aria-hidden="true">·</span>
    <svg className="remote-connection-lock" width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {state === "unknown" ? <><circle cx="10" cy="10" r="7" /><path d="M8 7a2 2 0 0 1 4 0c0 2-2 2-2 4m0 3h.01" /></> : <><rect x="5" y="9" width="10" height="8" rx="2" /><path className="remote-connection-shackle" d={ready ? "M7 9V6a3 3 0 0 1 6 0" : "M7 9V6a3 3 0 0 1 6 0v3"} /><path d="M10 12v2" /></>}
    </svg>
  </button>;
}
