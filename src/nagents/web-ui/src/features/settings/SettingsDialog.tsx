import { useEffect, useRef, useState } from "react";
import { executionLimits } from "./draft.js";
import { ModelField } from "./ModelField.js";
import { DictationSettings } from "./DictationSettings.js";
import type { useSettings } from "./useSettings";
import { revealAncestors } from "../../components/disclosures.js";

export function SettingsDialog({
  settings,
}: {
  settings: ReturnType<typeof useSettings>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const feedback = useRef<HTMLDivElement>(null);
  const confirmationHeading = useRef<HTMLHeadingElement>(null);
  const [confirmation, setConfirmation] = useState<"" | "refresh" | "reset">("");
  const { snapshot, draft, errors, disabled, pending, loading } = settings;

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
  }, [confirmation]);

  useEffect(() => {
    if (settings.error || settings.notice) feedback.current?.focus();
  }, [settings.error, settings.notice]);

  function finishConfirmation() {
    setConfirmation("");
    heading.current?.focus();
  }

  return (
    <dialog
      ref={dialog}
      className="settings-dialog"
      aria-labelledby="settings-title"
      aria-describedby="settings-description"
      onCancel={(event) => {
        event.preventDefault();
        settings.close();
      }}
      onKeyDown={(event) => {
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
        <h2 id="settings-title" ref={heading} tabIndex={-1}>
          Settings
        </h2>
        <p id="settings-description">
          Applies to your next run or recording. Saved with workspace data.
        </p>
      </header>
      <form
        noValidate
        onSubmit={async (event) => {
          event.preventDefault();
          if (confirmation) return;
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
        <div className="settings-body" aria-busy={loading || pending}>
          <div ref={feedback} tabIndex={-1} className="settings-feedback">
            <div className="settings-status" role="status">
              {loading
                ? "Loading current settings..."
                : pending
                  ? "Applying settings. Please wait before closing."
                  : settings.notice ||
                    (snapshot
                      ? snapshot.persisted
                        ? "Saved workspace overrides are active."
                        : "Using startup defaults. No saved overrides."
                      : "Settings have not loaded.")}
            </div>
            {settings.blocked && !pending && (
              <p className="settings-notice" role="status">
                Settings are read-only while work is active. Wait for it to
                finish before making changes.
              </p>
            )}
            {settings.error && (
              <p className="settings-error error-text" role="alert">
                {settings.error}
              </p>
            )}
            {Object.values(errors).some(Boolean) && (
              <p className="error-text" role="alert">
                Check the highlighted fields before saving.
              </p>
            )}
          </div>
          <div className="settings-refresh">
            <button
              type="button"
              disabled={disabled || !!confirmation}
              onClick={() => {
                if (settings.dirty) setConfirmation("refresh");
                else void settings.refresh();
              }}
            >
              Refresh settings
            </button>
            {settings.needsRefresh && (
              <span>Refresh is required before saving or resetting.</span>
            )}
          </div>
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
                  ? "Restore startup defaults?"
                  : "Discard draft and refresh?"}
              </h3>
              <p>
                {confirmation === "reset"
                  ? "This removes saved workspace overrides and discards your draft. The server's trusted startup defaults apply to your next run or recording."
                  : "This replaces your unsaved draft with the latest server settings. It does not change any saved settings or retry your save."}
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
                    disabled ||
                    (confirmation === "reset" && settings.needsRefresh)
                  }
                  onClick={async () => {
                    if (confirmation === "reset") await settings.reset();
                    else await settings.refresh();
                    finishConfirmation();
                  }}
                >
                  {confirmation === "reset"
                    ? "Remove overrides and reset"
                    : "Discard draft and refresh"}
                </button>
                <button
                  type="button"
                  disabled={pending}
                  onClick={finishConfirmation}
                >
                  Keep draft
                </button>
              </div>
            </section>
          )}
          {snapshot && draft && (
            <>
              <fieldset
                className="settings-group"
                disabled={disabled || !!confirmation}
              >
                <legend>Provider connection</legend>
                <div className="settings-field">
                  <label htmlFor="settings-provider">Provider</label>
                  <select
                    id="settings-provider"
                    value={draft.provider}
                    aria-describedby={`settings-provider-help${errors.provider ? " settings-provider-error" : ""}`}
                    aria-invalid={!!errors.provider}
                    onChange={(event) =>
                      settings.update("provider", event.target.value)
                    }
                  >
                    {!snapshot.providers.includes(draft.provider) && (
                      <option value={draft.provider} disabled>
                        {draft.provider || "Choose a provider"}
                      </option>
                    )}
                    {snapshot.providers.map((provider) => (
                      <option key={provider} value={provider}>
                        {provider}
                      </option>
                    ))}
                  </select>
                  <p id="settings-provider-help">
                    Deployment defaults fill these fields; saving stores
                    allowlisted overrides in this workspace.
                  </p>
                  {errors.provider && (
                    <p id="settings-provider-error" className="error-text">
                      {errors.provider}
                    </p>
                  )}
                </div>
                <div className="settings-field">
                  <label htmlFor="settings-api">API</label>
                  <select
                    id="settings-api"
                    value={draft.api}
                    onChange={(event) => settings.update("api", event.target.value)}
                  >
                    {snapshot.apis.map((api) => (
                      <option key={api} value={api}>
                        {api}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="settings-field">
                  <label htmlFor="settings-auth">Authentication</label>
                  <select
                    id="settings-auth"
                    value={draft.auth}
                    aria-describedby="settings-auth-help"
                    onChange={(event) => settings.update("auth", event.target.value)}
                  >
                    {snapshot.auths.map((auth) => (
                      <option key={auth} value={auth}>
                        {auth}
                      </option>
                    ))}
                  </select>
                  <p id="settings-auth-help">
                    ChatGPT login is available only for the default OpenAI
                    provider without an endpoint or API override.
                  </p>
                </div>
                <div className="settings-field">
                  <label htmlFor="settings-base_url">Endpoint override</label>
                  <input
                    id="settings-base_url"
                    type="text"
                    autoComplete="off"
                    spellCheck={false}
                    value={draft.base_url}
                    placeholder={snapshot.defaults.base_url || "Provider default"}
                    aria-describedby={`settings-base_url-help${errors.base_url ? " settings-base_url-error" : ""}`}
                    aria-invalid={!!errors.base_url}
                    onChange={(event) =>
                      settings.update("base_url", event.target.value)
                    }
                  />
                  <p id="settings-base_url-help">
                    Leave blank to use the provider&apos;s default endpoint, or
                    enter an HTTP(S) gateway URL without credentials. Required
                    for litellm.
                  </p>
                  {errors.base_url && (
                    <p id="settings-base_url-error" className="error-text">
                      {errors.base_url}
                    </p>
                  )}
                </div>
                <div className="settings-field">
                  <label htmlFor="settings-api_key_env">
                    API key environment name
                  </label>
                  <input
                    id="settings-api_key_env"
                    type="text"
                    autoComplete="off"
                    spellCheck={false}
                    value={draft.api_key_env}
                    aria-describedby={`settings-api_key_env-help${errors.api_key_env ? " settings-api_key_env-error" : ""}`}
                    aria-invalid={!!errors.api_key_env}
                    onChange={(event) =>
                      settings.update("api_key_env", event.target.value)
                    }
                  />
                  <p id="settings-api_key_env-help">
                    The variable name used for the deployment environment. The
                    key value itself is never displayed.
                  </p>
                  {errors.api_key_env && (
                    <p id="settings-api_key_env-error" className="error-text">
                      {errors.api_key_env}
                    </p>
                  )}
                </div>
                <div className="settings-field">
                  <label htmlFor="settings-provider-key">API key</label>
                  <input
                    id="settings-provider-key"
                    type="password"
                    autoComplete="new-password"
                    spellCheck={false}
                    value={settings.apiKey}
                    disabled={disabled || !!confirmation || settings.clearKey}
                    aria-describedby="settings-provider-key-help"
                    onChange={(event) => settings.updateKey(event.target.value)}
                  />
                  <p id="settings-provider-key-help">
                    {snapshot.connection.key_configured
                      ? "A key is stored in this workspace. Leave blank to keep it, or check below to remove it."
                      : "Optional. A submitted key is stored write-only for this provider and environment."}
                  </p>
                  {(snapshot.connection.key_configured || settings.apiKey) && (
                    <label className="settings-checkbox">
                      <input
                        type="checkbox"
                        checked={settings.clearKey}
                        disabled={disabled || !!confirmation}
                        onChange={(event) =>
                          settings.updateClearKey(event.target.checked)
                        }
                      />
                      Remove the stored key and fall back to the environment
                    </label>
                  )}
                </div>
              </fieldset>
              <fieldset
                className="settings-group"
                disabled={disabled || !!confirmation}
              >
                <legend>Model and profile</legend>
                <div className="settings-field">
                  <label htmlFor="settings-agent">Agent profile</label>
                  <select
                    id="settings-agent"
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
                        {profile.name} ({profile.mode})
                      </option>
                    ))}
                  </select>
                  <p id="settings-agent-help">
                    Server profiles preset their model; you can override it below.
                  </p>
                  {errors.agent && (
                    <p id="settings-agent-error" className="error-text">
                      {errors.agent}
                    </p>
                  )}
                </div>
                {/* A new connection replaces and aborts discovery, not the draft. */}
                <ModelField
                  key={JSON.stringify([settings.token, snapshot.connection])}
                  token={settings.token}
                  model={draft.model}
                  error={errors.model}
                  update={(model) => settings.update("model", model)}
                />
              </fieldset>
              <DictationSettings
                config={snapshot.dictation}
                draft={draft}
                errors={errors}
                disabled={disabled || !!confirmation}
                update={settings.update}
              />
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
                        <p id={`settings-${limit.key}-help`}>{limit.help}</p>
                        {errors[limit.key] && (
                          <p id={`settings-${limit.key}-error`} className="error-text">{errors[limit.key]}</p>
                        )}
                      </div>
                    ))}
                  </div>
                </fieldset>
              </details>
              <details className="settings-disclosure">
                <summary>Connection and startup defaults</summary>
                <section className="settings-connection" aria-labelledby="settings-connection-title">
                  <h3 id="settings-connection-title">Current connection <span>Read-only</span></h3>
                  <dl>
                    <dt>Provider</dt><dd>{snapshot.connection.provider}</dd>
                    <dt>API</dt><dd>{snapshot.connection.api}</dd>
                    <dt>Authentication</dt><dd>{snapshot.connection.auth}</dd>
                    <dt>Endpoint</dt><dd>{snapshot.connection.base_url || "Provider default"}</dd>
                    <dt>API key environment</dt>
                    <dd>
                      {snapshot.connection.api_key_env}
                      {snapshot.connection.key_configured
                        ? " (workspace key stored)"
                        : ""}
                    </dd>
                    <dt>Auth status</dt><dd>{snapshot.connection.auth_status}</dd>
                    <dt>Effective permissions</dt><dd>{snapshot.effective_mode}</dd>
                  </dl>
                  <p>
                    Current saved permissions are shown, not unsaved profile
                    changes. Provider routing is limited to allowlisted values;
                    plugins, storage, and trust remain server-managed. Stored
                    keys are never displayed.
                  </p>
                </section>
                <section className="settings-reset" aria-labelledby="settings-reset-title">
                  <h3 id="settings-reset-title">Startup defaults</h3>
                  <p>Reset removes saved overrides from workspace data, not just this draft.</p>
                  <button
                    type="button"
                    disabled={disabled || settings.needsRefresh || !!confirmation}
                    onClick={() => setConfirmation("reset")}
                  >
                    Reset to startup defaults
                  </button>
                </section>
              </details>
            </>
          )}
        </div>
        <footer className="settings-actions">
          <span>{settings.dirty ? "Unsaved changes" : "No unsaved changes"}</span>
          <button type="button" disabled={pending} onClick={settings.close}>
            {settings.dirty ? "Discard changes" : "Cancel"}
          </button>
          <button
            className="primary"
            type="submit"
            disabled={
              disabled || settings.needsRefresh || !settings.dirty || !!confirmation
            }
          >
            {pending ? "Applying..." : "Save"}
          </button>
        </footer>
      </form>
    </dialog>
  );
}
