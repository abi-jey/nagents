import { events } from "./fixtures";
import type { ActivityState, AgentId, AgentRun } from "./types";

export interface ActivityTreeNode {
  agent: AgentRun;
  ownActivityAt: number;
  /** Includes every descendant, even when that descendant is not loaded yet. */
  latestActivityAt: number;
  children: ActivityTreeNode[];
}

export interface ActivityTree {
  root: ActivityTreeNode;
  nodesById: ReadonlyMap<AgentId, ActivityTreeNode>;
  /** Includes the main conversation at the root. */
  totalCount: number;
}

export interface ActivityTreeRow {
  node: ActivityTreeNode;
  depth: number;
}

export interface ActivityTreePage {
  root: ActivityTreeNode;
  /** Ordered depth-first, including the root at depth zero. */
  rows: ActivityTreeRow[];
  visibleIds: ReadonlySet<AgentId>;
  /** Eligible rows still to load; descendants behind a collapsed parent are excluded. */
  remainingCount: number;
  /** Includes the root and all nodes not loaded yet. */
  totalCount: number;
}

export interface ActivityTreePageOptions {
  /** Parent identities whose next child level should be visible. */
  expandedIds?: ReadonlySet<AgentId>;
  /** Root is depth zero; children and grandchildren are visible by default. */
  defaultDepth?: number;
}

// The demo stores event identities rather than a second status-event transcript.
// Only acknowledged events contribute; future fixture events are never activity.
const eventsById = new Map(events.map(event => [event.id, event]));

function compareIds(a: AgentId, b: AgentId): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function compareNodes(a: ActivityTreeNode, b: ActivityTreeNode): number {
  return b.latestActivityAt - a.latestActivityAt || compareIds(a.agent.definition.id, b.agent.definition.id);
}

/** Build one parent/child tree, ordered by real activity rather than clock ticks. */
export function buildActivityTree(state: ActivityState, mainAgent: AgentRun): ActivityTree {
  const now = Number.isFinite(state.now) ? Math.max(0, state.now) : 0;
  const rootId = mainAgent.definition.id;
  const occurred = (at: number | undefined): at is number => at !== undefined && Number.isFinite(at) && at >= 0 && at <= now;
  const agents = new Map<AgentId, AgentRun>([[rootId, mainAgent]]);
  for (const agent of Object.values(state.agents)) {
    if (agent.definition.id !== rootId && occurred(agent.definition.spawnedAt)) agents.set(agent.definition.id, agent);
  }

  const observedAt = new Map<AgentId, number>();
  for (const id of state.seenEvents) {
    const event = eventsById.get(id);
    const agent = event && agents.get(event.agentId);
    if (event && agent && occurred(event.at) && event.at >= agent.definition.spawnedAt) {
      observedAt.set(event.agentId, Math.max(observedAt.get(event.agentId) ?? 0, event.at));
    }
  }

  const ownActivity = (agent: AgentRun): number => {
    let latest = 0;
    const include = (at: number | undefined) => { if (occurred(at)) latest = Math.max(latest, at); };
    include(agent.definition.spawnedAt);
    include(agent.startedAt);
    include(agent.finishedAt);
    include(observedAt.get(agent.definition.id));
    for (const record of agent.records) {
      if (record.kind === "message") {
        if (!occurred(record.message.at)) continue;
        include(record.message.at);
        include(record.message.pickedUpAt);
      } else {
        if (!occurred(record.tool.startedAt)) continue;
        include(record.tool.startedAt);
        include(record.tool.finishedAt);
      }
    }
    // The root adapter may omit transcript records; main messages still belong
    // to this same node. Scheduled replies and follow-ups are not activity yet.
    if (agent.definition.id === rootId) for (const message of state.mainMessages) include(message.at);
    return latest;
  };

  const ids = [...agents.keys()].filter(id => id !== rootId).sort(compareIds);
  const parents = new Map<AgentId, AgentId>();
  for (const id of ids) {
    const parent = agents.get(id)?.definition.parentId;
    // Missing parents and self-links remain inspectable directly under root.
    parents.set(id, parent && parent !== id && agents.has(parent) ? parent : rootId);
  }
  for (const id of ids) {
    const path: AgentId[] = [];
    const positions = new Map<AgentId, number>();
    let current = id;
    while (current !== rootId) {
      const position = positions.get(current);
      if (position !== undefined) {
        // Break malformed cycles at a stable member instead of losing agents
        // or making the result depend on the input object's insertion order.
        const cycleRoot = path.slice(position).sort(compareIds)[0];
        parents.set(cycleRoot, rootId);
        break;
      }
      positions.set(current, path.length);
      path.push(current);
      current = parents.get(current) ?? rootId;
    }
  }

  const childrenById = new Map<AgentId, AgentId[]>();
  for (const id of ids) {
    const parent = parents.get(id) ?? rootId;
    const siblings = childrenById.get(parent) ?? [];
    siblings.push(id);
    childrenById.set(parent, siblings);
  }
  const nodesById = new Map<AgentId, ActivityTreeNode>();
  const makeNode = (agent: AgentRun): ActivityTreeNode => {
    const children = (childrenById.get(agent.definition.id) ?? []).map(id => makeNode(agents.get(id)!)).sort(compareNodes);
    const ownActivityAt = ownActivity(agent);
    const latestActivityAt = children.reduce((latest, child) => Math.max(latest, child.latestActivityAt), ownActivityAt);
    const node = { agent, ownActivityAt, latestActivityAt, children };
    nodesById.set(agent.definition.id, node);
    return node;
  };
  const root = makeNode(mainAgent);
  return { root, nodesById, totalCount: nodesById.size };
}

/** Load recent non-root candidates plus every required ancestor/selected path. */
export function pageActivityTree(tree: ActivityTree, limit = 4, selectedId: AgentId = "", options: ActivityTreePageOptions = {}): ActivityTreePage {
  const rootId = tree.root.agent.definition.id;
  const parents = new Map<AgentId, AgentId>();
  const depths = new Map<AgentId, number>([[rootId, 0]]);
  const collect = (node: ActivityTreeNode, depth: number) => {
    for (const child of node.children) {
      const id = child.agent.definition.id;
      parents.set(id, node.agent.definition.id);
      depths.set(id, depth + 1);
      collect(child, depth + 1);
    }
  };
  collect(tree.root, 0);
  const selectedPath = new Set<AgentId>([rootId]);
  let selected = selectedId;
  while (tree.nodesById.has(selected) && !selectedPath.has(selected)) {
    selectedPath.add(selected);
    selected = parents.get(selected) ?? rootId;
  }
  const requestedDepth = options.defaultDepth ?? 2;
  const defaultDepth = Number.isFinite(requestedDepth) ? Math.max(0, Math.floor(requestedDepth)) : 2;
  const eligibleIds = new Set<AgentId>([rootId]);
  const collectEligible = (node: ActivityTreeNode, depth: number) => {
    for (const child of node.children) {
      const id = child.agent.definition.id;
      if (depth < defaultDepth || options.expandedIds?.has(node.agent.definition.id) || selectedPath.has(id)) {
        eligibleIds.add(id);
        collectEligible(child, depth + 1);
      }
    }
  };
  collectEligible(tree.root, 0);
  const count = Number.isFinite(limit) ? Math.max(0, Math.floor(limit)) : 4;
  const ranked = [...tree.nodesById.values()].filter(node => node.agent.definition.id !== rootId && eligibleIds.has(node.agent.definition.id)).sort((a, b) =>
    b.latestActivityAt - a.latestActivityAt
    || b.ownActivityAt - a.ownActivityAt
    || (depths.get(b.agent.definition.id) ?? 0) - (depths.get(a.agent.definition.id) ?? 0)
    || compareIds(a.agent.definition.id, b.agent.definition.id));
  const visibleIds = new Set<AgentId>([rootId]);
  const includePath = (id: AgentId) => {
    let current = id;
    while (tree.nodesById.has(current) && !visibleIds.has(current)) {
      visibleIds.add(current);
      current = parents.get(current) ?? rootId;
    }
  };
  for (const node of ranked.slice(0, count)) includePath(node.agent.definition.id);
  includePath(selectedId);
  // An explicit expansion must reveal its direct children immediately, even
  // when newer branches already occupy the lazy-loading candidate budget.
  for (const id of eligibleIds) {
    const parent = parents.get(id);
    if (parent && options.expandedIds?.has(parent)) includePath(id);
  }
  const rows: ActivityTreeRow[] = [];
  const trim = (node: ActivityTreeNode, depth: number): ActivityTreeNode => {
    const pageNode = { ...node, children: [] as ActivityTreeNode[] };
    rows.push({ node: pageNode, depth });
    pageNode.children = node.children.filter(child => visibleIds.has(child.agent.definition.id)).map(child => trim(child, depth + 1));
    return pageNode;
  };
  const root = trim(tree.root, 0);
  return { root, rows, visibleIds, remainingCount: eligibleIds.size - visibleIds.size, totalCount: tree.totalCount };
}
