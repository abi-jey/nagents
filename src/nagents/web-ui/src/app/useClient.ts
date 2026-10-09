import { useEffect, useState } from "react";
import { useChatRun } from "../features/chat/useChatRun";
import { useSessions } from "../features/sessions/useSessions";
import { useSettings } from "../features/settings/useSettings";
import { queuedMessageFailure } from "../api/messages";
import { useChannels } from "../features/channels/useChannels";
import { useContextStats } from "../features/context/useContext";
import { useSessionActions } from "../features/sessions/useSessionActions";
import { useUploads } from "../features/chat/useUploads";
import { useOperations } from "./useOperations";
import { availability } from "./availability";
import { recoverOrphanDraft } from "../features/chat/draftRecovery.js";
import { commandIntent, type CommandIntent } from "../features/chat/commands.js";
import { request } from "../api/client.js";
import { readSettings, saveSettings } from "../features/settings/transport.js";
import { createDraft, parseDraft, selectProfile } from "../features/settings/draft.js";
import { useVoicePresence } from "./useVoicePresence.js";

export function useClient() {
  const sessions = useSessions();
  const chat = useChatRun(
    sessions.config?.token || "",
    sessions.sessionId,
    sessions.receive,
    sessions.acceptCredentials,
    () => void connect(),
  );
  const operations = useOperations();
  const { operating, error, setError } = operations;
  const [panel, setPanel] = useState<"none" | "tools" | "designer">("none");
  const voice = useVoicePresence();
  const [commandHelp, setCommandHelp] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);
  const [navigationRequest, setNavigationRequest] = useState(0);
  const busy = operating || !!chat.runId || sessions.globalBusy;

  // The UI rejects competing operations immediately, matching the backend's 409 policy.
  async function operate(action: () => Promise<void>): Promise<boolean> {
    return operations.operate(action);
  }

  const settings = useSettings({
    token: sessions.config?.token || "",
    blocked: busy || !!sessions.externalRun || !!chat.approval.pending,
    operate: (action) => busy ? Promise.resolve(false) : operate(action),
    accept: sessions.acceptSettings,
  });
  const channels = useChannels(sessions.config?.token || "", sessions.sessions, sessions.sessionId);
  const token = sessions.config?.token || "";
  const configuration = `${sessions.config?.provider}:${sessions.config?.model}:${sessions.config?.agent}:${settings.snapshot?.revision}`;
  const { uploads, uploadState } = useUploads(token, sessions.sessionId, configuration);
  const context = useContextStats(
    token,
    sessions.sessionId,
    { active: !!chat.runId, revision: chat.contextRevision, enabled: !sessions.activityOnly,
      configuration },
  );
  const membership = useSessionActions({ sessions, chat, operations, busy,
    blocked: channels.open || settings.open || panel !== "none" || !!chat.approval.pending });

  function access() {
    return availability({
      ready: !!token && !!sessions.sessionId,
      operating: operations.occupied(),
      running: !!chat.runId || sessions.globalBusy,
      modal: channels.open || settings.open || !!membership.deletion.target ||
        membership.trashController.getSnapshot().open || panel !== "none" || commandHelp || loginOpen,
      approval: !!chat.approval.pending,
      uploadsReady: uploadState.items.every((item) => item.status === "ready"),
    });
  }

  async function connect() {
    await operate(async () => {
      chat.pause();
      chat.setStatus("Connecting to local harness");
      try {
        const snapshot = await sessions.connect();
        if (snapshot) {
          if (sessions.sessionId && !snapshot.sessions.some((session) => session.id === sessions.sessionId))
            chat.forgetSession(sessions.sessionId);
          chat.loadHistory(snapshot);
        }
        chat.reconnect();
      } catch (cause) {
        chat.setStatus("Disconnected");
        throw cause;
      }
    });
  }

  useEffect(() => {
    void connect();
  }, []);

  async function select(id = "") {
    if (id && id === sessions.sessionId) return true;
    if (!(id ? access().navigate : access().create)) return false;
    return operate(async () => {
      // A voice connection belongs to the chat where it was opened.
      voice.close();
      chat.pause();
      try {
        const snapshot = await sessions.select(id);
        if (snapshot) chat.loadHistory(snapshot);
      } finally { chat.reconnect(); }
    });
  }

  async function command({ name, argument }: CommandIntent): Promise<boolean> {
    if (uploadState.items.length) throw new Error("Remove draft attachments before running a command. Your draft is kept.");
    if (["login", "settings", "model", "agent", "provider"].includes(name) && !access().settings)
      throw new Error("Finish the active run before changing settings or signing in. Your draft is kept.");
    if (name === "help") { setCommandHelp(true); return true; }
    if (name === "login") { voice.close(); setLoginOpen(true); return true; }
    if (name === "new") {
      if (!access().create) throw new Error("Finish the active run before starting a new conversation.");
      return select();
    }
    if (name === "sessions") {
      if (argument) return select(argument);
      setNavigationRequest(value => value + 1); return true;
    }
    if (name === "compact") return operate(async () => { await chat.submit("/compact", [], "compact"); });
    if (name === "stop") {
      if (!chat.runId) throw new Error("There is no active run in this chat to stop.");
      return operate(async () => { await chat.cancel(chat.runId); });
    }
    if (name === "context") { context.setOpen(true); context.refresh(); return true; }
    if (name === "tools") { setPanel("tools"); return true; }
    if (name === "channels") { channels.show(); return true; }
    if (name === "voice") { voice.show("settings", token, sessions.sessionId); return true; }
    if (!argument) { settings.show(); return true; }
    return operate(async () => {
      if (name === "provider") {
        const registry = await (await request("provider-scopes/workspace/providers", token)).json() as {
          revision: string; providers: Record<string, unknown>;
        };
        if (!Object.hasOwn(registry.providers, argument)) throw new Error(`Provider connection not found: ${argument}. Use /provider to manage connections.`);
        await request(`provider-scopes/workspace/providers/${encodeURIComponent(argument)}/activate`, token, { revision: registry.revision });
        sessions.acceptSettings(await readSettings(token, new AbortController().signal));
        chat.setStatus(`Using provider ${argument}`);
      } else {
        const snapshot = await readSettings(token, new AbortController().signal);
        if (name === "agent" && !snapshot.profiles.some(profile => profile.name === argument)) throw new Error(`Agent profile not found: ${argument}. Use /agent to choose a profile.`);
        const draft = name === "agent" ? selectProfile(createDraft(snapshot.values), argument, snapshot.profiles)
          : { ...createDraft(snapshot.values), model: argument };
        const parsed = parseDraft(draft, snapshot.profiles);
        if (!parsed.ok) throw new Error(Object.values(parsed.errors).filter(Boolean).join(" "));
        const saved = await saveSettings(token, snapshot.revision, parsed.values);
        sessions.acceptSettings(saved);
        chat.setStatus(name === "agent" ? `Using agent ${saved.values.agent}` : `Using model ${saved.effective_model || saved.values.model}`);
      }
      context.refresh();
    });
  }

  async function submit(value = chat.prompt) {
    if (
      !access().submit ||
      sessions.activityOnly ||
      (!value.trim() && !uploadState.items.length)
    )
      return;
    try {
      const intent = commandIntent(value);
      if (intent) {
        setError("");
        const root = sessions.sessionId, draft = chat.drafts.get(root);
        if (await command(intent)) chat.drafts.submitted(root, draft, value);
        return;
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Browser command failed. Your draft is kept.");
      return;
    }
    const failure = value.length > 32000
      ? "The draft exceeds 32,000 characters. Shorten it before sending; it is kept."
      : queuedMessageFailure(value, sessions.sessionId, uploadState.items.map((item) => item.id));
    if (failure) {
      setError(failure);
      return;
    }
    await operate(async () => {
      const ids = uploads.begin();
      let confirmed = false;
      try { await chat.submit(value, ids); confirmed = true; }
      finally { uploads.finish(ids, confirmed); }
    });
  }

  async function recoverDraft(sourceId: string): Promise<boolean> {
    if (!access().create) return false;
    return operate(async () => {
      voice.close(); chat.pause();
      try {
        await recoverOrphanDraft(sourceId, { drafts: chat.drafts,
          openBlank: () => sessions.select(""), selected: () => sessions.currentSelection().id,
          load: chat.loadHistory,
        });
        chat.setStatus("Draft recovered. Review it before sending.");
      } finally { chat.reconnect(); }
    });
  }

  async function cancel() {
    try {
      await chat.cancel(chat.runId);
      if (!chat.connected) await connect();
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Cancel failed. Reconnect to check the harness.",
      );
    }
  }

  return {
    ...membership,
    available: access(),
    commandHelp, closeCommandHelp: () => setCommandHelp(false),
    loginOpen, closeLogin: () => setLoginOpen(false),
    navigationRequest,
    panel,
    liveOpen: voice.open,
    liveIntent: voice.intent,
    liveAutoStart: voice.shouldStart(token, sessions.sessionId),
    consumeLiveStart: () => voice.consumeStart(voice.request),
    showTools: () => { if (access().tools) setPanel("tools"); },
    showDesigner: () => { if (access().designer) { voice.close(); setPanel("designer"); } },
    showLive: (intent: "start" | "settings" = "start") => { if (access().live) voice.show(intent, token, sessions.sessionId); },
    closeLive: voice.close,
    closePanel: () => { setPanel("none"); void connect(); },
    dismissError: () => setError(""),
    sessions,
    context,
    chat,
    uploads,
    uploadState,
    settings,
    channels,
    busy,
    operating,
    error,
    orphanedDrafts: sessions.config && sessions.sessionId ? chat.drafts.orphaned(sessions.sessions.map((session) => session.id)) : [],
    recoverDraft,
    connect,
    select,
    submit,
    cancel,
  };

}

export type Client = ReturnType<typeof useClient>;
