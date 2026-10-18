import { useEffect } from "react";
import { request } from "../../../api/client.js";
import { InstructionField } from "./InstructionField.js";
import { readJson, sourceLabel, targetQuery, useNavigatorDraft } from "./state.js";
import type { NavigatorScope, NavigatorTarget } from "./state.js";

export interface VoiceInstructionsReply {
  scope: NavigatorScope; agent: string; revision: string; present: boolean;
  text: string; inherited: string; inherited_source: string; source: string; source_path: string; preferences_path: string; builtin: string;
}
interface InstructionDraft { prompt?: string; }

export function VoiceInstructionEditor({ token, target, report }: { token: string; target: NavigatorTarget; report: (dirty: boolean) => void }) {
  const editor = useNavigatorDraft<VoiceInstructionsReply, InstructionDraft>({
    read: signal => readJson(`live/instructions?${targetQuery(target)}`, token, signal),
    create: snapshot => snapshot.present ? { prompt: snapshot.text } : {},
    write: async (snapshot, draft) => (await request("live/instructions", token, { scope: target.scope, ...(target.scope === "agent" ? { agent: target.agent } : {}), revision: snapshot.revision, ...draft }, undefined, "PUT")).json() as Promise<VoiceInstructionsReply>,
  });
  const { snapshot, draft, pending, recovery, dirty, error } = editor;
  useEffect(() => { report(dirty); }, [dirty, report]);
  const custom = !!draft && Object.hasOwn(draft, "prompt");
  const value = snapshot && draft ? custom ? draft.prompt! : snapshot.inherited : "";
  return <div className="an-voice-instructions">
    {pending && !snapshot && <p role="status">Loading voice instructions…</p>}
    {error && <p role="alert" className="error-text">{error}</p>}
    {(recovery || (!snapshot && error)) && <div className="an-recovery"><p>Your instruction draft is kept. Reload saved instructions before saving again.</p><button type="button" disabled={pending} onClick={() => void editor.reload()}>{dirty ? "Discard voice instruction draft and reload" : "Reload voice instructions"}</button></div>}
    {snapshot && draft && <>
      <InstructionField label="Voice instructions" value={value} custom={custom} source={sourceLabel(snapshot.inherited_source)} inheritLabel={target.scope === "agent" ? snapshot.inherited_source === "definition" ? "Use agent file prompt" : "Inherit workspace" : target.scope === "workspace" ? "Inherit global" : "Use built-in default"} scopeLabel={target.scope === "agent" ? target.agent : target.scope} disabled={pending || recovery} dirty={dirty} storagePath={snapshot.preferences_path} sourcePath={!dirty ? snapshot.source_path : undefined} maxLength={12000} description="The full voice prompt is saved with the voice profile in the agent Markdown file and replaces the default voice prompt, up to 12,000 characters / 24 KB. Changes apply to the next voice conversation." customize={() => editor.edit({ prompt: value })} inherit={() => editor.edit({})} change={prompt => editor.edit({ prompt })} />
      <div className="an-instruction-actions"><span>{dirty ? "Unsaved voice instructions" : editor.notice ? "Voice instructions saved" : !custom ? "Following inherited instructions" : "Custom voice instructions"}</span><button type="button" disabled={pending || !dirty} onClick={editor.discard}>Discard instructions</button><button type="button" className="primary" disabled={pending || recovery || !dirty} onClick={() => void editor.save()}>{pending ? "Saving…" : "Save voice instructions"}</button></div>
    </>}
  </div>;
}
