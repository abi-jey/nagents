import { useEffect, useState } from "react";

export type RemoteAuthentication = {
  state: "signed_in" | "sign_in_required" | "token_ready" | "token_missing" | "not_required" | "storage_error" | "host_setup_required";
  expires_at: number;
  error_code: string;
  refreshable?: boolean;
};

export function remoteAuthLabel(auth?: RemoteAuthentication, now = Date.now() / 1000): string {
  if (!auth) return "Auth status unavailable";
  if (auth.state === "signed_in") {
    if (auth.expires_at && auth.expires_at <= now + 10 && !auth.refreshable) return "Sign-in required";
    return auth.refreshable ? "Signed in · auto refresh" : "Signed in";
  }
  return { sign_in_required: "Sign-in required", token_ready: "Server token configured", token_missing: "Server token missing",
    not_required: "No sign-in required", storage_error: "Credentials unavailable", host_setup_required: "Sign-in setup required" }[auth.state];
}

export function RemoteStatus({ authentication }: { authentication?: RemoteAuthentication }) {
  const [, tick] = useState(0);
  useEffect(() => {
    if (!authentication?.expires_at || authentication.refreshable) return;
    const remaining = authentication.expires_at * 1000 - Date.now() - 10000;
    if (remaining <= 0) return;
    const timer = setTimeout(() => tick(value => value + 1), Math.min(remaining + 10, 2147483647));
    return () => clearTimeout(timer);
  }, [authentication?.expires_at, authentication?.refreshable]);
  const label = remoteAuthLabel(authentication);
  const ready = ["Signed in", "Signed in · auto refresh", "Server token configured", "No sign-in required"].includes(label);
  const expires = authentication?.expires_at;
  const expiry = expires && Number.isFinite(expires) ? new Date(expires * 1000).toLocaleString() : "";
  const title = ["Authentication shared by all chats using this connection in this workspace.", expiry && `Access token expires ${expiry}.`,
    authentication?.refreshable && "Access tokens refresh automatically when the connection is used."].filter(Boolean).join(" ");
  return <span className="remote-auth-status" data-state={ready ? "ready" : authentication ? "attention" : "unknown"} title={title}>
    <i aria-hidden="true" />{label}
  </span>;
}
