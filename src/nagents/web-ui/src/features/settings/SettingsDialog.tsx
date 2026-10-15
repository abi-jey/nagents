import { useEffect, useRef, useState } from "react";
import { createDraft, executionLimits, compactionLimits } from "./draft.js";
import { ModelSelector, type ModelCatalog } from "./ModelSelector.js";
import { ReasoningEffort, effortLabel } from "./ReasoningEffort.js";
import { ProvidersPanel } from "./ProvidersPanel.js";
import { ProviderLogo } from "../../components/ProviderLogo.js";
import { Icon } from "../../components/Icon.js";
import { LoginDialog } from "./LoginDialog.js";
import type { useSettings } from "./useSettings";
import type { SettingsScope } from "./transport.js";
import type { SettingsDraft } from "./draft.js";
import { revealAncestors } from "../../components/disclosures.js";

export function SettingsDialog({
  settings,
}: {
  settings: ReturnType<typeof useSettings>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const feedback = useRef<HTMLDivElement>(null);
  const confirmationHeading = useRef<HTMLHeadingElement>(null);
  const restoreFocus = useRef<"" | "heading" | SettingsScope>("");
  const [confirmation, setConfirmation] = useState<"" | "refresh" | "reset" | "connection" | SettingsScope>("");
  const [connectionDirty, setConnectionDirty] = useState(false);
  const [connectionBusy, setConnectionBusy] = useState(false);
  const [catalog, setCatalog] = useState<ModelCatalog>();
  const [loginOpen, setLoginOpen] = useState(false);
  const activateConnection = useRef<() => Promise<void>>(undefined);
  const { snapshot, draft, errors, disabled, pending, loading } = settings;
  const global = settings.scope === "global";
  const globalDraft = snapshot && createDraft(snapshot.defaults, snapshot.default_model_binding);
  const savedDraft = snapshot && createDraft(snapshot.values, snapshot.model_binding);
  const bindingChanged = !!draft && !!savedDraft && (draft.model_provider !== savedDraft.model_provider || draft.model !== savedDraft.model);
  const profile = snapshot?.profiles.find(item => item.name === draft?.agent);

  function confirmConnection(apply: () => Promise<void>) {
    if (settings.dirty) { activateConnection.current = apply; setConfirmation("connection"); }
    else void apply();
  }

  function origin(key: Exclude<keyof SettingsDraft, "model_provider">) {
    if (global || !snapshot || !draft || !globalDraft || !savedDraft) return null;
    const inherited = draft[key] === globalDraft[key];
    const changed = draft[key] !== savedDraft[key];
    const value = snapshot.defaults[key];
    return <span className="settings-origin">
      {inherited ? (changed ? "Will inherit Global after saving" : "Inherited from Global")
        : (changed ? "Unsaved workspace override" : "Workspace override")}
      {!inherited && <> · Global default: {key === "reasoning_effort" ? effortLabel(String(value || "")) : typeof value === "boolean" ? (value ? "On" : "Off") : value}</>}
    </span>;
  }

  function focusScope(scope: SettingsScope) {
    dialog.current?.querySelector<HTMLButtonElement>(`[data-settings-scope="${scope}"]`)?.focus();
  }

  function selectScope(next: SettingsScope) {
    if (next === settings.scope || pending || connectionBusy || settings.blocked || confirmation) return;
    if (settings.dirty || connectionDirty) setConfirmation(next);
    else {
      settings.switchScope(next);
      if (body.current) body.current.scrollTop = 0;
    }
  }

  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement;
    element?.showModal();
    heading.current?.focus();
    return () => {
      element?.close();
      if (previous instanceof HTMLElement)
        previous.focus({ preventScroll: true });
    };
  }, []);

  useEffect(() => {
    if (confirmation) confirmationHeading.current?.focus();
    else if (restoreFocus.current) {
      if (restoreFocus.current === "heading") heading.current?.focus();
      else focusScope(restoreFocus.current);
      restoreFocus.current = "";
    }
  }, [confirmation]);

  useEffect(() => {
    if (settings.error || settings.notice) feedback.current?.focus();
  }, [settings.error, settings.notice]);

  function finishConfirmation() {
    restoreFocus.current = confirmation === "workspace" || confirmation === "global" ? settings.scope : "heading";
    setConfirmation("");
    activateConnection.current = undefined;
  }

  return (
    <dialog
      ref={dialog}
      className="settings-dialog settings-workspace-panel"
      aria-labelledby="settings-title"
      aria-describedby="settings-description"
      onCancel={(event) => {
        event.preventDefault();
        if (loginOpen) return;
        if (confirmation) finishConfirmation();
        else if (!connectionBusy) settings.close();
      }}
      onKeyDown={(event) => {
        if (loginOpen) return;
        if (event.key !== "Tab") return;
        const controls = [
          ...event.currentTarget.querySelectorAll<HTMLElement>(
            'button:not(:disabled), input:not(:disabled), select:not(:disabled), summary, [tabindex="0"]',
          ),
        ].filter((control) => control.getClientRects().length > 0);
        const first = controls[0];
        const last = controls.at(-1);
        if (!first) {
          event.preventDefault();
          heading.current?.focus();
        } else if (
          event.shiftKey &&
          (document.activeElement === first ||
            document.activeElement === heading.current ||
            document.activeElement === feedback.current)
        ) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }}
    >
      <header className="settings-heading">
        <div className="settings-title-row"><div><span className="settings-eyebrow">{global ? "Across your workspaces" : "Workspace settings"}</span><h2 id="settings-title" ref={heading} tabIndex={-1}>Provider &amp; model</h2></div><button type="button" aria-label="Close settings" disabled={pending || connectionBusy} onClick={settings.close}><Icon name="close" size={19} /></button></div>
        <div className="settings-scopes" role="group" aria-label="Settings scope">
          {(["workspace", "global"] as const).map((scope) => (
            <button key={scope} type="button" data-settings-scope={scope}
              aria-pressed={settings.scope === scope} aria-controls="settings-scope-content"
              disabled={pending || connectionBusy || settings.blocked || !!confirmation}
              onClick={() => selectScope(scope)}>
              {scope === "workspace" ? "Workspace" : "Global"}
            </button>
          ))}
        </div>
        <p id="settings-description">
          {global
            ? "Global defaults and provider connections are shared across workspaces. Workspace overrides take priority."
            : "Choose the defaults for this workspace. Individual agent profiles can override them."}
        </p>
      </header>
      <form
        noValidate
        onSubmit={async (event) => {
          event.preventDefault();
          if (confirmation || connectionBusy) return;
          await settings.save();
          requestAnimationFrame(() => {
            const invalid = dialog.current?.querySelector<HTMLElement>('[aria-invalid="true"]');
            if (invalid && dialog.current) {
              revealAncestors(invalid, dialog.current);
              invalid.focus();
            }
          });
        }}
      >
        <div id="settings-scope-content" ref={body} className="settings-body" aria-busy={loading || pending}>
          <div ref={feedback} tabIndex={-1} className="settings-feedback">
            {(loading || pending || settings.notice) && (
              <div className="settings-status" role="status">
                {loading ? "Loading current settings..." : pending
                  ? "Applying settings. Please wait before closing."
                  : settings.notice}
              </div>
            )}
            {settings.blocked && !pending && (
              <p className="settings-notice" role="status">
                A settings request is in progress. Your current conversation continues.
              </p>
            )}
            {settings.error && (
              <p className="settings-error error-text" role="alert">
                {settings.error}
              </p>
            )}
            {settings.readError && (
              <p className="settings-error error-text" role="alert">
                {settings.readError}
              </p>
            )}
            {Object.values(errors).some(Boolean) && (
              <p className="error-text" role="alert">
                Check the highlighted fields before saving.
              </p>
            )}
          </div>
          {(settings.needsRefresh || (!snapshot && settings.error)) && (
            <div className="settings-refresh">
              <button
                type="button"
                disabled={disabled || !!confirmation}
                onClick={() => {
                  if (settings.dirty) setConfirmation("refresh");
                  else void settings.reload();
                }}
              >
                {settings.needsRefresh ? "Reload saved settings" : "Retry loading settings"}
              </button>
              {settings.needsRefresh && (
                <span role={settings.changedElsewhere ? "alert" : "status"}>
                  {settings.changedElsewhere
                    ? "Saved settings changed while you were editing. Your draft is kept. Reload before saving or resetting."
                    : "Reload to check saved settings before saving or resetting. Your draft is kept."}
                </span>
              )}
            </div>
          )}
          {confirmation && (
            <section
              className="settings-confirmation"
              aria-labelledby="settings-confirmation-title"
            >
              <h3
                id="settings-confirmation-title"
                ref={confirmationHeading}
                tabIndex={-1}
              >
                {confirmation === "reset"
                  ? (global ? "Restore startup defaults?" : "Use global defaults?")
                  : confirmation === "refresh" ? "Discard draft and reload?" : confirmation === "connection" ? "Discard settings draft and use connection?" : `Discard changes and switch to ${confirmation === "global" ? "Global" : "Workspace"}?`}
              </h3>
              <p>
                {confirmation === "reset"
                  ? (global ? "This removes saved global defaults. Workspace overrides are kept." : "This removes saved workspace overrides and discards your draft. Global defaults apply to your next run.")
                  : confirmation === "refresh"
                    ? "This replaces your unsaved draft with the latest saved settings. It does not change saved settings or retry your save."
                    : confirmation === "connection" ? "This discards unsaved model and workspace settings, then applies the selected connection immediately. Saved settings are kept."
                      : "Switching scopes discards unsaved settings and connection edits in this scope. Saved settings and connections are kept."}
              </p>
              {confirmation === "reset" && snapshot && (
                <p>
                  Startup profile: <code>{snapshot.defaults.agent}</code>.
                  Model: <code>{snapshot.defaults.model}</code>.
                </p>
              )}
              <div className="settings-confirmation-actions">
                <button
                  type="button"
                  disabled={
                    disabled || connectionBusy ||
                    (confirmation === "reset" && settings.needsRefresh)
                  }
                  onClick={async () => {
                    if (confirmation === "reset") await settings.reset();
                    else if (confirmation === "refresh") await settings.reload();
                    else if (confirmation === "connection") {
                      const apply = activateConnection.current;
                      await settings.reload();
                      await apply?.();
                    } else {
                      const next = confirmation;
                      settings.switchScope(next);
                      setConnectionDirty(false);
                      if (body.current) body.current.scrollTop = 0;
                      restoreFocus.current = next;
                      setConfirmation("");
                      return;
                    }
                    finishConfirmation();
                  }}
                >
                  {confirmation === "reset"
                    ? "Remove overrides and reset"
                    : confirmation === "refresh" ? "Discard draft and reload" : confirmation === "connection" ? "Discard draft and use connection" : "Discard and switch"}
                </button>
                <button
                  type="button"
                  disabled={pending}
                  onClick={finishConfirmation}
                >
                  {confirmation === "refresh" ? "Keep draft" : "Keep editing"}
                </button>
              </div>
            </section>
          )}
          {snapshot && draft && (
            <>
              <ProvidersPanel key={settings.scope} token={settings.token} scope={settings.scope} blocked={disabled || !!confirmation} applied={settings.refresh} openGlobal={() => selectScope("global")} onDraftChange={setConnectionDirty} onBusyChange={setConnectionBusy} bindingProvider={draft.model_provider} confirmActivation={confirmConnection} login={() => setLoginOpen(true)} loginConnection={snapshot.connection.provider_id}>
                {connection => {
                  const browsing = connection.id !== draft.model_provider;
                  const metadata = !browsing && catalog?.route === `workspace/${connection.id}/${connection.revision}` ? catalog.metadata[draft.model] : undefined;
                  const contextWindow = connection.modelContextWindows[draft.model] || connection.contextWindow || metadata?.context_window || 0;
                  const contextSource = connection.modelContextWindows[draft.model] || connection.contextWindow ? "configured" : metadata?.context_window_source || "";
                  return <>
                  <section className="settings-selection-summary" aria-label="Model selection summary">
                    <div className="selection-summary-caption"><span>{browsing ? "Browsing connection" : global ? "Global default" : "Workspace default"}</span><span>{connection.origin === "global" ? "Global connection" : connection.origin === "workspace" ? "Workspace connection" : "Startup connection"}</span></div>
                    <div className="selection-summary-model"><ProviderLogo provider={connection.kind || snapshot.connection.provider} /><div><strong>{browsing ? connection.label : draft.model || "Choose a model"}</strong><span>{connection.id || snapshot.connection.provider} · {connection.auth === "chatgpt" || connection.auth === "codex" ? "ChatGPT / Codex login" : connection.auth === "entra" ? "Microsoft Entra ID" : connection.auth === "api-key" ? "API key" : "Automatic authentication"}</span></div>{connection.canConfigure && <button type="button" title="Connection settings" aria-label="Connection settings" disabled={connection.busy} onClick={connection.configure}><Icon name="settings" size={16} /></button>}</div>
                    {browsing ? <p className="settings-preview-action">Choose a model to use this connection. Both are applied when you save.</p> : <div className="selection-summary-metrics"><span>{effortLabel(draft.reasoning_effort)} reasoning</span><span>{draft.compact_trigger === "auto" ? "Compact at 70%" : draft.compact_trigger === "off" ? "Auto compaction off" : draft.compact_trigger === "messages" ? `Compact at ${draft.compact_messages} messages` : `Compact at ${Math.floor(Number(draft.compact_tokens) * .7).toLocaleString()} tokens`}</span><span>{contextWindow ? `${contextWindow.toLocaleString()} context · ${contextSource}` : "Context · automatic"}</span></div>}
                  </section>
                  <fieldset className="settings-group settings-model-selection" disabled={disabled || !!confirmation || connection.busy}>
                    <legend>Choose a model</legend>
                    <ModelSelector key={`${settings.scope}-${connection.id}-${connection.revision}`} token={settings.token} scope="workspace" provider={connection.id} revision={connection.revision} model={browsing ? "" : draft.model} error={errors.model} disabled={disabled || !!confirmation || connection.busy} update={model => settings.updateModel({ provider: connection.id, id: model })} autoLoad onCatalog={setCatalog} />
                    <p className="settings-origin">{bindingChanged ? `Unsaved ${settings.scope} model` : snapshot.model_binding_source === "startup" ? "Startup default" : global ? "Global default" : snapshot.model_binding_source === "global" ? "Inherited from Global" : "Workspace default"} · {draft.model_provider || "Startup connection"} / {draft.model}</p>
                    {!bindingChanged && snapshot.model_binding_available === false && <p className="error-text" role="status">{snapshot.model_binding_unavailable_reason || "The saved model connection is unavailable in this workspace."}</p>}
                    {global && !browsing && connection.origin === "workspace" && <p className="settings-origin">This global default uses a workspace connection. Other workspaces need a connection with this name.</p>}
                    {profile && (profile.model || profile.provider || profile.reasoning_effort) && <div className="settings-profile-override"><Icon name="team" size={15} /><div><strong>{profile.name} has its own settings</strong><p>{profile.model ? `Model: ${profile.model}. ` : ""}{profile.provider ? `Connection: ${profile.provider}. ` : ""}{profile.reasoning_effort ? `Reasoning: ${effortLabel(profile.reasoning_effort)}. ` : ""}These take precedence over workspace defaults.{profile.source ? <> Configure this agent in Team, or edit <code>{profile.source}</code>.</> : " Choose another profile below to use the workspace defaults."}</p></div></div>}
                  </fieldset>
                  <fieldset className="settings-group settings-tuning" disabled={disabled || !!confirmation}>
                    <legend>Reasoning &amp; context</legend>
                    <ReasoningEffort id="settings-reasoning_effort" token={settings.token} scope="workspace"
                      provider={draft.model_provider} model={draft.model}
                      knownOptions={metadata?.reasoning_efforts}
                      value={draft.reasoning_effort} error={errors.reasoning_effort} disabled={disabled || !!confirmation}
                      update={effort => settings.update("reasoning_effort", effort)}>{origin("reasoning_effort")}</ReasoningEffort>
                    <p className="settings-context-help">Context limits follow the connection and model metadata. {connection.canConfigure && <button type="button" disabled={connection.busy} onClick={connection.configure}>Configure a limit</button>}</p>
                  </fieldset>
              <fieldset
                className="settings-group"
                disabled={disabled || !!confirmation}
              >
                <legend>Agent profile and permissions</legend>
                <label className="settings-checkbox"><input id="settings-read_only" type="checkbox" aria-describedby="settings-read_only-help" checked={draft.read_only || !!snapshot.read_only_locked} disabled={disabled || !!confirmation || !!snapshot.read_only_locked} onChange={(event) => settings.update("read_only", event.target.checked)} />Read-only workspace</label>
                <p id="settings-read_only-help">{snapshot.read_only_locked ? "Read-only operation is required by the startup configuration." : "Limit agents to inspection: file changes, shell, and custom tools remain blocked."}{!snapshot.read_only_locked && origin("read_only")}</p>
                <div className="settings-field">
                  <label htmlFor="settings-agent">Workspace default agent</label>
                  <select
                    id="settings-agent"
                    disabled={global}
                    value={draft.agent}
                    aria-describedby={`settings-agent-help${errors.agent ? " settings-agent-error" : ""}`}
                    aria-invalid={!!errors.agent}
                    onChange={(event) =>
                      settings.update("agent", event.target.value)
                    }
                  >
                    {!snapshot.profiles.some(
                      (profile) => profile.name === draft.agent,
                    ) && (
                      <option value={draft.agent} disabled>
                        {draft.agent || "Choose a profile"}
                      </option>
                    )}
                    {snapshot.profiles.map((profile) => (
                      <option key={profile.name} value={profile.name}>
                        {profile.name}{profile.description ? ` · ${profile.description}` : ""}{profile.provider ? ` → ${profile.provider}` : ""}{profile.mode === "reviewer" ? " (read-only)" : ""}
                      </option>
                    ))}
                  </select>
                   <p id="settings-agent-help">{global ? "Agent profiles belong to individual workspaces." : "Used for new chats. Existing conversations keep their selected agent. Profiles may set their own model or connection."}</p>
                  {snapshot.profiles.find(profile => profile.name === draft.agent)?.source && (
                    <p>Source: <code>{snapshot.profiles.find(profile => profile.name === draft.agent)?.source}</code></p>
                  )}
                  {!!snapshot.agent_diagnostics?.length && (
                    <details className="settings-disclosure"><summary>Agent loading diagnostics ({snapshot.agent_diagnostics.length})</summary>
                      <ul className="settings-agent-diagnostics">{snapshot.agent_diagnostics.map((note, index) => <li key={index}>{note}</li>)}</ul>
                    </details>
                  )}
                  {errors.agent && (
                    <p id="settings-agent-error" className="error-text">
                      {errors.agent}
                    </p>
                  )}
                </div>
              </fieldset>
              <fieldset className="settings-group" disabled={disabled || !!confirmation}>
                <legend>Message submission</legend>
                <div className="settings-field"><label htmlFor="settings-submit_mode">When this conversation is working</label>
                  <select id="settings-submit_mode" value={draft.submit_mode} aria-invalid={!!errors.submit_mode} aria-describedby="settings-submit-help" onChange={(event) => settings.update("submit_mode", event.target.value)}>
                    <option value="queue">Queue · finish current work first (default)</option>
                    <option value="interrupt">Interrupt · stop current work, then continue</option>
                  </select>
                  <p id="settings-submit-help">New web messages are always saved to the queue. Interrupt also cancels the active run in this same conversation and waits for cleanup before processing queued messages.{origin("submit_mode")}</p>
                  {errors.submit_mode && <p className="error-text">{errors.submit_mode}</p>}
                </div>
              </fieldset>
              <details className="settings-disclosure">
                <summary>Execution limits</summary>
                <fieldset className="settings-group" disabled={disabled || !!confirmation}>
                  <legend className="sr-only">Execution limits</legend>
                  <div className="settings-limits">
                    {executionLimits.map((limit) => (
                      <div className="settings-field" key={limit.key}>
                        <label htmlFor={`settings-${limit.key}`}>{limit.label}</label>
                        <div className="settings-number">
                          <input
                            id={`settings-${limit.key}`}
                            type="text"
                            inputMode={limit.key === "shell_timeout" ? "decimal" : "numeric"}
                            autoComplete="off"
                            spellCheck={false}
                            required
                            value={draft[limit.key]}
                            aria-describedby={`settings-${limit.key}-help${errors[limit.key] ? ` settings-${limit.key}-error` : ""}`}
                            aria-invalid={!!errors[limit.key]}
                            onChange={(event) => settings.update(limit.key, event.target.value)}
                          />
                          <span aria-hidden="true">{limit.unit}</span>
                        </div>
                        <p id={`settings-${limit.key}-help`}>{limit.help}{origin(limit.key)}</p>
                        {errors[limit.key] && (
                          <p id={`settings-${limit.key}-error`} className="error-text">{errors[limit.key]}</p>
                        )}
                      </div>
                    ))}
                  </div>
                </fieldset>
              </details>
              <details className="settings-disclosure">
                <summary>Context compaction</summary>
                <fieldset className="settings-group" disabled={disabled || !!confirmation}>
                  <legend className="sr-only">Context compaction</legend>
                  <div className="settings-field">
                    <label htmlFor="settings-compact_trigger">Trigger</label>
                    <select
                      id="settings-compact_trigger"
                      value={draft.compact_trigger}
                      aria-describedby={`settings-compact_trigger-help${errors.compact_trigger ? " settings-compact_trigger-error" : ""}`}
                      aria-invalid={!!errors.compact_trigger}
                      onChange={(event) => settings.update("compact_trigger", event.target.value)}
                    >
                      <option value="auto">Automatic (70% of model window)</option>
                      <option value="tokens">Token budget</option>
                      <option value="messages">Message count</option>
                      <option value="off">Off</option>
                    </select>
                    <p id="settings-compact_trigger-help">
                      Automatic compacts at 70% of the model&apos;s context window.
                      Token and message thresholds apply before the next model
                      call; off disables automatic compaction. Manual compaction
                      stays available.
                      {origin("compact_trigger")}
                    </p>
                    {errors.compact_trigger && (
                      <p id="settings-compact_trigger-error" className="error-text">
                        {errors.compact_trigger}
                      </p>
                    )}
                  </div>
                  {compactionLimits.map((limit) => (
                    <div className="settings-field" key={limit.key}>
                      <label htmlFor={`settings-${limit.key}`}>{limit.label}</label>
                      <div className="settings-number">
                        <input
                          id={`settings-${limit.key}`}
                          type="text"
                          inputMode="numeric"
                          autoComplete="off"
                          spellCheck={false}
                          required
                          value={draft[limit.key]}
                          aria-describedby={`settings-${limit.key}-help${errors[limit.key] ? ` settings-${limit.key}-error` : ""}`}
                          aria-invalid={!!errors[limit.key]}
                          onChange={(event) => settings.update(limit.key, event.target.value)}
                        />
                        <span aria-hidden="true">{limit.unit}</span>
                      </div>
                      <p id={`settings-${limit.key}-help`}>{limit.help}{origin(limit.key)}</p>
                      {errors[limit.key] && (
                        <p id={`settings-${limit.key}-error`} className="error-text">
                          {errors[limit.key]}
                        </p>
                      )}
                    </div>
                  ))}
                </fieldset>
              </details>
              <details className="settings-disclosure">
                <summary>Settings defaults</summary>
                <section className="settings-reset" aria-labelledby="settings-reset-title">
                  <h3 id="settings-reset-title">{global ? "Startup defaults" : "Inherited defaults"}</h3>
                  <p>Reset removes saved settings in this scope, not just this draft.</p>
                  <button
                    type="button"
                    disabled={disabled || settings.needsRefresh || !!confirmation}
                    onClick={() => setConfirmation("reset")}
                  >
                    {global ? "Reset global defaults" : "Use global defaults"}
                  </button>
                </section>
              </details>
                </>; }}
              </ProvidersPanel>
            </>
          )}
        </div>
        <footer className="settings-actions">
          <span>{settings.dirty ? `Unsaved ${settings.scope} settings` : connectionDirty ? "Unsaved connection edits" : "No unsaved changes"}</span>
          <button type="button" disabled={pending || connectionBusy} onClick={settings.close}>
            {settings.dirty || connectionDirty ? "Discard changes" : "Cancel"}
          </button>
          <button
            className="primary"
            type="submit"
            disabled={
              disabled || connectionBusy || settings.needsRefresh || !settings.dirty || !!confirmation
            }
          >
            {pending ? "Applying..." : `Save ${settings.scope} settings`}
          </button>
        </footer>
      </form>
      {loginOpen && <LoginDialog token={settings.token} close={() => setLoginOpen(false)} applied={settings.refresh} />}
    </dialog>
  );
}
