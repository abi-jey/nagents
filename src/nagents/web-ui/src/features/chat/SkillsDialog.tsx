import { useEffect, useRef } from "react";
import { Icon } from "../../components/Icon.js";
import { skillCommand } from "./commands.js";
import type { SkillItem, SkillsState } from "./skills.js";

export function SkillsDialog({ skills, choose }: { skills: SkillsState; choose: (skill: SkillItem) => void }) {
  const dialog = useRef<HTMLDialogElement>(null), search = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const element = dialog.current, previous = document.activeElement;
    element?.showModal(); search.current?.focus();
    return () => { element?.close(); if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);
  const query = skills.search.toLowerCase().trim();
  const matches = skills.items.filter(skill => `${skill.name} ${skill.description}`.toLowerCase().includes(query));
  return <dialog className="workspace-dialog skills-dialog" ref={dialog} aria-labelledby="skills-title"
    onCancel={event => { event.preventDefault(); skills.close(); }}>
    <header><h2 id="skills-title">Skills</h2><button type="button" aria-label="Close skills" onClick={skills.close}><Icon name="close" /></button></header>
    <p>Choose a skill, add your request, then send it in this chat.</p>
    <label className="skills-search">Search skills<input ref={search} id="skills-search" name="skills-search" type="search" placeholder="Name or description…" value={skills.search}
      onChange={event => skills.setSearch(event.target.value)} /></label>
    <div className="skills-browser-status"><span role="status">{skills.error || (skills.loading ? "Loading skills…" : skills.catalog?.enabled === false ? "Skills are disabled for this agent." : `${skills.items.length} available to ${skills.catalog?.agent || "this agent"}`)}</span>
      <button type="button" disabled={skills.loading} onClick={() => void skills.refresh()}>Refresh</button></div>
    <ul className="skills-list" aria-label="Available skills">{matches.map(skill => <li key={skill.name}><button type="button" onClick={() => choose(skill)}>
      <span className="skill-command"><code>/{skillCommand(skill)}</code><span className="slash-kind">Skill</span></span><span className="skill-description">{skill.description}</span>
    </button></li>)}</ul>
    {!skills.loading && !skills.error && skills.catalog?.enabled !== false && !matches.length && <p>{skills.items.length ? "No skills match this search." : "No skills are available for this agent. Installed skills appear here after discovery."}</p>}
  </dialog>;
}
