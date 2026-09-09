import { useEffect, useRef, useState } from "react";
import { executionLimits } from "./draft";
import type { useSettings } from "./useSettings";
import "./settings.css";

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
            'button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex="0"]',
          ),
        ];
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
          Applies to your next run. Saved with workspace data.
        </p>
      </header>
      <form
        noValidate
        onSubmit={async (event) => {
          event.preventDefault();
          if (confirmation) return;
          await settings.save();
          requestAnimationFrame(() => {
            dialog.current
              ?.querySelector<HTMLElement>('[aria-invalid="true"]')
              ?.focus();
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
                  ? "This removes saved workspace overrides and discards your draft. The server's trusted startup defaults apply to your next run."
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
                    Profiles come from the server. Choosing one presets its
                    model when provided; you can then override the model below.
                  </p>
                  {errors.agent && (
                    <p id="settings-agent-error" className="error-text">
                      {errors.agent}
                    </p>
                  )}
                </div>
                <div className="settings-field">
                  <label htmlFor="settings-model">Model ID</label>
                  <input
                    id="settings-model"
                    type="text"
                    value={draft.model}
                    autoComplete="off"
                    spellCheck={false}
                    required
                    aria-describedby={`settings-model-help${errors.model ? " settings-model-error" : ""}`}
                    aria-invalid={!!errors.model}
                    onChange={(event) =>
                      settings.update("model", event.target.value)
                    }
                  />
                  <p id="settings-model-help">
                    Exact model ID for the current connection, up to 200
                    characters. Model availability and account access are not
                    checked here.
                  </p>
                  {errors.model && (
                    <p id="settings-model-error" className="error-text">
                      {errors.model}
                    </p>
                  )}
                </div>
              </fieldset>
              <fieldset
                className="settings-group"
                disabled={disabled || !!confirmation}
              >
                <legend>Execution limits</legend>
                <div className="settings-limits">
                  {executionLimits.map((limit) => (
                    <div className="settings-field" key={limit.key}>
                      <label htmlFor={`settings-${limit.key}`}>
                        {limit.label}
                      </label>
                      <div className="settings-number">
                        <input
                          id={`settings-${limit.key}`}
                          type="text"
                          inputMode={
                            limit.key === "shell_timeout" ? "decimal" : "numeric"
                          }
                          autoComplete="off"
                          spellCheck={false}
                          required
                          value={draft[limit.key]}
                          aria-describedby={`settings-${limit.key}-help${errors[limit.key] ? ` settings-${limit.key}-error` : ""}`}
                          aria-invalid={!!errors[limit.key]}
                          onChange={(event) =>
                            settings.update(limit.key, event.target.value)
                          }
                        />
                        <span aria-hidden="true">{limit.unit}</span>
                      </div>
                      <p id={`settings-${limit.key}-help`}>{limit.help}</p>
                      {errors[limit.key] && (
                        <p
                          id={`settings-${limit.key}-error`}
                          className="error-text"
                        >
                          {errors[limit.key]}
                        </p>
                      )}
                    </div>
                  ))}
                </div>
              </fieldset>
              <section
                className="settings-connection"
                aria-labelledby="settings-connection-title"
              >
                <h3 id="settings-connection-title">
                  Current connection <span>Read-only</span>
                </h3>
                <dl>
                  <dt>Provider</dt>
                  <dd>{snapshot.connection.provider}</dd>
                  <dt>API</dt>
                  <dd>{snapshot.connection.api}</dd>
                  <dt>Auth status</dt>
                  <dd>{snapshot.connection.auth_status}</dd>
                  <dt>Effective permissions</dt>
                  <dd>{snapshot.effective_mode}</dd>
                </dl>
                <p>
                  Current saved permissions are shown, not unsaved profile
                  changes. Connection credentials, plugins, storage, and trust
                  remain server-managed.
                </p>
              </section>
              <section
                className="settings-reset"
                aria-labelledby="settings-reset-title"
              >
                <h3 id="settings-reset-title">Startup defaults</h3>
                <p>
                  Reset removes saved overrides from workspace data, not just
                  this draft.
                </p>
                <button
                  type="button"
                  disabled={disabled || settings.needsRefresh || !!confirmation}
                  onClick={() => setConfirmation("reset")}
                >
                  Reset to startup defaults
                </button>
              </section>
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
