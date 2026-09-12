import type { DictationConfig } from "../dictation/types.js";
import type { DraftErrors, SettingsDraft } from "./draft.js";
import type { useSettings } from "./useSettings.js";

export function DictationSettings({ config, draft, errors, disabled, update }: {
  config: DictationConfig;
  draft: SettingsDraft;
  errors: DraftErrors;
  disabled: boolean;
  update: ReturnType<typeof useSettings>["update"];
}) {
  return (
    <fieldset className="settings-group dictation-settings" disabled={disabled}>
      <legend>Dictation</legend>
      <p>Record audio, review the transcription, then insert it into your message. Only Send starts a chat run.</p>
      <div className="settings-field">
        <label className="settings-checkbox" htmlFor="settings-dictation_enabled">
          <input
            id="settings-dictation_enabled"
            type="checkbox"
            checked={draft.dictation_enabled}
            aria-describedby="settings-dictation_enabled-help settings-dictation_enabled-error"
            aria-invalid={!!errors.dictation_enabled}
            onChange={(event) => update("dictation_enabled", event.target.checked)}
          />
          Enable microphone dictation
        </label>
        <p id="settings-dictation_enabled-help">This preference cannot override an administrator's disabled setting or recording ceiling.</p>
        <p id="settings-dictation_enabled-error" className="error-text">{errors.dictation_enabled}</p>
      </div>
      {([
        { key: "dictation_model", label: "Transcription model ID", help: "OpenAI API-key transcription, separate from your chat model and Codex login. Default: gpt-4o-mini-transcribe.", placeholder: "gpt-4o-mini-transcribe" },
        { key: "dictation_language", label: "Transcription language", help: "Two lowercase letters, such as en. Leave blank for automatic detection.", placeholder: "Automatic" },
        { key: "dictation_max_seconds", label: "Recording limit (seconds)", help: "1 to 300 seconds, further bounded by the administrator. Reaching the limit stops recording without uploading.", placeholder: "" },
      ] as const).map((field) => (
        <div className="settings-field" key={field.key}>
          <label htmlFor={`settings-${field.key}`}>{field.label}</label>
          <input
            id={`settings-${field.key}`}
            type="text"
            inputMode={field.key === "dictation_max_seconds" ? "numeric" : "text"}
            value={draft[field.key]}
            placeholder={field.placeholder}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            aria-describedby={`settings-${field.key}-help${errors[field.key] ? ` settings-${field.key}-error` : ""}`}
            aria-invalid={!!errors[field.key]}
            onChange={(event) => update(field.key, event.target.value)}
          />
          <p id={`settings-${field.key}-help`}>{field.help}</p>
          {errors[field.key] && <p id={`settings-${field.key}-error`} className="error-text">{errors[field.key]}</p>}
        </div>
      ))}
      <section className="settings-connection" aria-labelledby="dictation-connection-title">
        <h3 id="dictation-connection-title">Transcription availability <span>Read-only</span></h3>
        <p role="status">{config.status}</p>
        <dl>
          <dt>Available</dt><dd>{config.available ? "Yes" : "No"}</dd>
          <dt>Administrator enabled</dt><dd>{config.admin_enabled ? "Yes" : "No"}</dd>
          <dt>Effective recording ceiling</dt><dd>{config.max_seconds} seconds</dd>
          <dt>Effective upload ceiling</dt><dd>{config.max_bytes.toLocaleString("en-US")} bytes</dd>
          <dt>API key environment name</dt><dd><code>{config.api_key_env}</code></dd>
          <dt>Audio format</dt><dd>16 kHz mono PCM16 WAV</dd>
        </dl>
        <p>Availability reflects saved settings. The API key and transcription connection are configured on the server; no key is entered or stored in this browser.</p>
      </section>
    </fieldset>
  );
}
