import { useEffect, useRef, useState } from "react";
import { request, RequestError } from "../../api/client.js";
import type { RetainedTask } from "../../types.js";
import { TaskDrafts, type TaskActivity, type TaskDetail, type TaskReceipt, type TeamProfile, type TeamSubagentUsage } from "./model.js";

const profilePrefix = "profile:";
export const profileTarget = (id: string, parentId = "") => `${profilePrefix}${id}${parentId ? `@${parentId}` : ""}`;
export const isProfileTarget = (id: string) => id.startsWith(profilePrefix);
export function profileSelection(target: string): { profile: string; parentId: string } {
  const [profile, parentId = ""] = target.slice(profilePrefix.length).split("@");
  return { profile, parentId };
}

export function useTeam(token: string, sessionId: string, revision: number, running: boolean, activity: TaskActivity[]) {
  const [selectedState, setSelected] = useState({ session: sessionId, id: "" });
  const selected = selectedState.session === sessionId ? selectedState.id : "";
  const [index, setIndex] = useState<{ session: string; tasks: RetainedTask[]; profiles: TeamProfile[]; subagentUsage?: TeamSubagentUsage }>({ session: "", tasks: [], profiles: [] });
  const [details, setDetails] = useState<{ session: string; task: string; value: TaskDetail }>();
  const [failure, setFailure] = useState<{ session: string; text: string; source?: string }>({ session: "", text: "" });
  const [startNotice, setStartNotice] = useState({ session: "", target: "", text: "" });
  const [loading, setLoading] = useState(false);
  const [earlier, setEarlier] = useState<{ session: string; task: string; history: TaskDetail["history"]; before: string }>();
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [sendingState, setSending] = useState("");
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [draftVersion, setDraftVersion] = useState(0);
  const drafts = useRef(new TaskDrafts());
  const active = useRef({ session: sessionId, selected });
  active.current = { session: sessionId, selected };
  const sending = useRef(new Set<string>());
  const indexRefresh = useRef<() => void>(() => {});
  const detailRefresh = useRef<() => void>(() => {});
  const path = `sessions/${encodeURIComponent(sessionId)}/tasks`;

  useEffect(() => {
    if (!token || !sessionId) return;
    let disposed = false, pending = false, rerun = false;
    const controller = new AbortController();
    const refresh = async () => {
      if (pending) { rerun = true; return; }
      pending = true;
      try {
        const result = await (await request(path, token, undefined, controller.signal)).json() as {
          tasks: RetainedTask[]; profiles: TeamProfile[]; subagent_usage?: TeamSubagentUsage;
        };
        if (!disposed) { setIndex({ session: sessionId, tasks: result.tasks, profiles: result.profiles || [], subagentUsage: result.subagent_usage }); setFailure(current => current.session === sessionId && current.source === "index" ? { session: sessionId, text: "" } : current); }
      } catch (error) { if (!disposed) setFailure({ session: sessionId, source: "index", text: error instanceof Error ? error.message : "Could not load agents." }); }
      finally { pending = false; if (rerun && !disposed) { rerun = false; void refresh(); } }
    };
    indexRefresh.current = () => void refresh();
    void refresh();
    // Retained child activity can continue after the main response ends.
    const timer = setInterval(() => void refresh(), running ? 1800 : 5000);
    return () => { disposed = true; controller.abort(); clearInterval(timer); indexRefresh.current = () => {}; };
  }, [token, sessionId, running, refreshVersion]);
  useEffect(() => { const timer = setTimeout(() => { indexRefresh.current(); detailRefresh.current(); }, 300); return () => clearTimeout(timer); }, [revision]);

  useEffect(() => {
    if (!token || !sessionId || !selected || isProfileTarget(selected)) { setLoading(false); return; }
    let disposed = false, pending = false, rerun = false;
    const controller = new AbortController();
    const refresh = async () => {
      if (pending) { rerun = true; return; }
      pending = true;
      try {
        const value = await (await request(`${path}/${encodeURIComponent(selected)}`, token, undefined, controller.signal)).json() as TaskDetail;
        if (!disposed) {
          setDetails({ session: sessionId, task: selected, value });
          setIndex(current => current.session === sessionId ? { ...current, tasks: current.tasks.map(task => task.id === selected ? { ...value.task, context: value.context } : task) } : current);
          setFailure(current => current.session === sessionId && current.source === "detail" ? { session: sessionId, text: "" } : current);
        }
      } catch (error) { if (!disposed) setFailure({ session: sessionId, source: "detail", text: error instanceof Error ? error.message : "Could not load agent conversation." }); }
      finally { pending = false; if (!disposed) setLoading(false); if (rerun && !disposed) { rerun = false; void refresh(); } }
    };
    setLoading(true); detailRefresh.current = () => void refresh(); void refresh();
    const timer = setInterval(() => void refresh(), 1500);
    return () => { disposed = true; controller.abort(); clearInterval(timer); detailRefresh.current = () => {}; };
  }, [token, sessionId, selected, refreshVersion]);

  const tasks = index.session === sessionId ? index.tasks : [];
  const profiles = index.session === sessionId ? index.profiles : [];
  const task = tasks.find(item => item.id === selected);
  const profileScope = isProfileTarget(selected) ? profileSelection(selected) : { profile: "", parentId: "" };
  const profile = profiles.find(item => item.id === profileScope.profile);
  const startParent = tasks.find(item => item.id === profileScope.parentId);
  const savedDetail = details?.session === sessionId && details.task === selected ? details.value : undefined;
  const older = earlier?.session === sessionId && earlier.task === selected ? earlier : undefined;
  const detail = savedDetail && older ? { ...savedDetail, before: older.before,
    history: [...older.history.filter(row => !savedDetail.history.some(current => current.history_id === row.history_id)), ...savedDetail.history] } : savedDetail;
  const prompt = drafts.current.get(sessionId, selected);
  // Keep React subscribed to edits without moving any draft when the recipient changes.
  void draftVersion;
  const canMessage = profile ? profileScope.parentId
    ? startParent?.can_start_children === true && startParent.child_agents?.includes(profile.id) === true
    : profile.can_start ?? profile.available : task?.can_message === true;
  const startUnavailable = profileScope.parentId ? startParent?.child_start_unavailable_reason ||
    (!startParent ? "This parent runtime is unavailable." : !canMessage ? "This definition is unavailable under the parent's policy." : "")
    : profile?.unavailable_reason || "";

  async function submit() {
    const root = sessionId, target = selected, text = drafts.current.get(root, target);
    const key = JSON.stringify([root, target]);
    if (!target || !text.trim() || text.length > 16000 || !canMessage || sending.current.has(key)) return;
    const message = drafts.current.prepare(root, target, text, () => crypto.randomUUID());
    sending.current.add(key); setSending(key); setFailure({ session: root, text: "" });
    try {
      const result = await (await request(profile ? path : `${path}/${encodeURIComponent(target)}/messages`, token,
        profile ? { ...message, agent: profile.id, ...(profileScope.parentId ? { parent_task_id: profileScope.parentId } : {}) } : message)).json() as TaskReceipt | { task: RetainedTask; receipt: TaskReceipt };
      const receipt = "receipt" in result ? result.receipt : result;
      if (receipt.status === "error" || receipt.status === "cancelled") {
        drafts.current.rejected(root, target, message.message_id);
        setFailure({ session: root, text: receipt.reason || "The agent did not accept this message. Your draft is kept." });
        return;
      }
      drafts.current.confirmed(root, target, message); setDraftVersion(value => value + 1);
      if ("task" in result && result.task.id && active.current.session === root && active.current.selected === target) {
        setIndex(current => ({ session: root, tasks: [...(current.session === root ? current.tasks.filter(item => item.id !== result.task.id) : []), result.task], profiles: current.profiles, subagentUsage: current.subagentUsage }));
        setSelected({ session: root, id: result.task.id });
      } else if ("task" in result && !result.task.id) {
        setStartNotice({ session: root, target, text: "Your message is queued while this agent starts. Its conversation will appear in the team tree." });
      }
      indexRefresh.current(); detailRefresh.current();
    } catch (error) {
      if (error instanceof RequestError && [400, 404, 409, 413, 422].includes(error.status)) drafts.current.rejected(root, target, message.message_id);
      if (active.current.session === root) setFailure({ session: root, text: error instanceof RequestError ? error.message : "Message delivery is unconfirmed. Your draft is kept; retrying the same message checks its existing identity." });
    } finally { sending.current.delete(key); setSending(current => current === key ? "" : current); }
  }

  async function loadEarlier() {
    const before = detail?.before;
    if (!before || loadingEarlier) return;
    const root = sessionId, target = selected;
    setLoadingEarlier(true);
    try {
      const result = await (await request(`${path}/${encodeURIComponent(target)}?before=${encodeURIComponent(before)}`, token)).json() as TaskDetail;
      if (active.current.session !== root || active.current.selected !== target) return;
      setEarlier(current => ({ session: root, task: target, before: result.before || "", history: [...result.history,
        ...(current?.session === root && current.task === target ? current.history.filter(row => !result.history.some(other => other.history_id === row.history_id)) : [])] }));
    } catch (error) { if (active.current.session === root) setFailure({ session: root, text: error instanceof Error ? error.message : "Could not load earlier messages." }); }
    finally { setLoadingEarlier(false); }
  }

  return { selected, select: (id: string) => setSelected({ session: sessionId, id }), tasks, profiles,
    subagentUsage: index.session === sessionId ? index.subagentUsage : undefined, task, profile, detail, loaded: index.session === sessionId,
    activity: activity.filter(event => event.task_id === selected), loading, error: failure.session === sessionId ? failure.text : "",
    startNotice: startNotice.session === sessionId && startNotice.target === selected ? startNotice.text : "",
    refresh: () => setRefreshVersion(value => value + 1), dismissError: () => setFailure({ session: sessionId, text: "" }),
    prompt, setPrompt: (text: string) => { drafts.current.set(sessionId, selected, text); setDraftVersion(value => value + 1); },
    canMessage, startParent, startParentId: profileScope.parentId, startUnavailable,
    submitting: sendingState === JSON.stringify([sessionId, selected]), submit, loadEarlier, loadingEarlier };
}
export type TeamController = ReturnType<typeof useTeam>;
