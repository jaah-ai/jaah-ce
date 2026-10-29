/* The fleet-wide `/worktrees` list's GROUP-BY vocabulary, and the flattener that turns a grouping into
 * the exact ordered rows the table renders. (The node page's pool panel is node-scoped and uses
 * `poolSlot.ts::groupSlots`.)
 * One vocabulary for the toolbar, the URL (`worktreeFilters.ts`, where it is NOT a filter) and the page.
 * Returns a FLAT list: a collapsed group's children
 * are OMITTED, so outline order/depth/counts are testable without a render — and find-in-page sees fewer
 * rows than the bars' TRUE counts. Pure of React and the API client.
 */
import { MAIN_CLONE_NUMBER } from './poolSlot'
import { orgQualifierFor } from './projectLabel'
import type { OrgRef, Worktree } from '../types'

/** ONE LIST, NO BARS — Project and Node become ordinary COLUMNS, the only shape where both facets compare
 *  at a glance. */
export const GROUP_BY_FLAT = 'flat'

/** Node, then project inside it — "what is each machine carrying". */
export const GROUP_BY_NODE_PROJECT = 'node-project'

/** Project, then node inside it — "where does each project live". */
export const GROUP_BY_PROJECT_NODE = 'project-node'

export type WorktreeGroupBy =
  | typeof GROUP_BY_FLAT
  | typeof GROUP_BY_NODE_PROJECT
  | typeof GROUP_BY_PROJECT_NODE

/** Toolbar order: widest reading first, then the two outlines. */
export const WORKTREE_GROUP_BY_VALUES: readonly WorktreeGroupBy[] = [
  GROUP_BY_FLAT,
  GROUP_BY_NODE_PROJECT,
  GROUP_BY_PROJECT_NODE,
]

/** THE PAGE'S DEFAULT (operator's pick): project-first answers "where is my work". */
export const DEFAULT_WORKTREE_GROUP_BY: WorktreeGroupBy = GROUP_BY_PROJECT_NODE

/**
 * A LARGE GROUP STARTS COLLAPSED, A SMALL ONE OPEN: hundreds of slots would bury the outline, but a project
 * of a few slots folded behind two bars costs two clicks to see anything. Bar counts cover the WHOLE group,
 * which is what makes folding payable. Used only when a group has no stored collapse state.
 */
export const OPEN_GROUP_MAX_SLOTS = 10

/** A group's fold before the operator has touched it, by how many slots it holds. */
export function defaultGroupCollapsed(slotCount: number): boolean {
  return slotCount > OPEN_GROUP_MAX_SLOTS
}

/** Guard for URL/store values; anything else falls back to the default. */
export function isWorktreeGroupBy(value: string): value is WorktreeGroupBy {
  return (WORKTREE_GROUP_BY_VALUES as readonly string[]).includes(value)
}

/**
 * A node's human name, resolved by the caller: `node_id` is an opaque uuid and the page
 * holds the roster. MUST NEVER ANSWER EMPTY — the roster is `[]` on first paint; `nodeGroupLabel`
 * enforces the raw-id fallback.
 */
export type NodeLabelFor = (nodeId: string) => string

/** Discriminated union so the table cannot render a bar's cells against a slot. */
export type WorktreeRow =
  | {
      /** THE OUTER BAR — node under `node-project`, project under `project-node`, never emitted flat. */
      kind: 'outer'
      depth: 0
      /** React key and collapse-store key, stable across regroupings; carries the FACET so a node and a
       *  project both named `acme` never share a record. */
      key: string
      label: string
      /** A project bar's org when another org's project shares its name (`lib/projectLabel.tsx`), else null. */
      org: OrgRef | null
      /** Slots UNDER this bar, collapsed or not — computed here so a closed bar never reads `0 slots`. */
      count: number
      collapsed: boolean
    }
  | {
      /** THE INNER BAR — the second facet, emitted only under an OPEN outer bar. */
      kind: 'inner'
      depth: 1
      key: string
      label: string
      /** As the outer bar's. */
      org: OrgRef | null
      /** Slots under THIS bar; inner counts sum to the outer count. */
      count: number
      collapsed: boolean
    }
  | {
      kind: 'slot'
      /** The indent; never re-derived from `kind` in JSX. */
      depth: 0 | 2
      /** The slot's uuid: unique fleet-wide. */
      key: string
      slot: Worktree
    }

/** Is a group closed? Called with a bar's `key` and its slot count; the page answers from its stored
 *  collapse state, else `defaultGroupCollapsed(slotCount)`. */
export type IsGroupCollapsed = (groupKey: string, slotCount: number) => boolean

/* `??` is not enough: a missing name can be `''`, which renders an unreadable bar. */
function nodeGroupLabel(nodeId: string, nodeLabelFor: NodeLabelFor): string {
  const resolved = nodeLabelFor(nodeId)
  return resolved && resolved.trim() !== '' ? resolved : nodeId
}

/* SLOT ORDER: the main clone first (every sibling's `.git` points into it), then numbered ascending. Tested
   EXPLICITLY, not left to `MAIN_CLONE_NUMBER` being the minimum. `id` breaks the final tie so the order is
   TOTAL and does not reshuffle on a refresh. */
function compareSlots(a: Worktree, b: Worktree): number {
  const aMain = a.number === MAIN_CLONE_NUMBER
  const bMain = b.number === MAIN_CLONE_NUMBER
  if (aMain !== bMain) return aMain ? -1 : 1
  return a.number - b.number || a.id.localeCompare(b.id)
}

/* Groups order by LABEL, locale-compared (a node's uuid is random hex); identity breaks ties (two nodes can
   share a label, e.g. before the roster loads). */
function compareGroups(a: { label: string; id: string }, b: { label: string; id: string }): number {
  return a.label.localeCompare(b.label) || a.id.localeCompare(b.id)
}

/* A Map so a project named `constructor` cannot hit a prototype key. Consumers sort. */
function bucketBy(slots: readonly Worktree[], keyOf: (slot: Worktree) => string): Map<string, Worktree[]> {
  const buckets = new Map<string, Worktree[]>()
  for (const slot of slots) {
    const bucket = buckets.get(keyOf(slot))
    if (bucket) bucket.push(slot)
    else buckets.set(keyOf(slot), [slot])
  }
  return buckets
}

/* A facet's group identity, label and org, so both outlines are one code path with facets swapped. All read
   off a member slot: every slot in a group agrees on them. */
type Facet = {
  /** The `key` prefix separating node and project collapse records. */
  name: 'node' | 'project'
  idOf: (slot: Worktree) => string
  labelOf: (slot: Worktree) => string
  orgOf: (slot: Worktree) => OrgRef | null
}

/**
 * THE FLATTENER — `(slots, groupBy, nodeLabelFor)` in, the exact ordered render list out.
 * `isCollapsed` omitted → every group takes `defaultGroupCollapsed`.
 * FILTERING HAPPENS BEFORE THIS (`worktreeFilters.ts`), so bar counts are of what survived the filter.
 */
export function buildWorktreeRows(
  slots: readonly Worktree[],
  groupBy: WorktreeGroupBy,
  nodeLabelFor: NodeLabelFor,
  isCollapsed: IsGroupCollapsed = (_groupKey, slotCount) => defaultGroupCollapsed(slotCount),
): WorktreeRow[] {
  const nodeFacet: Facet = {
    name: 'node',
    idOf: slot => slot.node_uuid,
    labelOf: slot => nodeGroupLabel(slot.node_uuid, nodeLabelFor),
    orgOf: () => null,
  }
  /* A project groups by NAME (its collapse key), split by org only when two orgs' projects share the name:
     then each bar keys `<name>@<org slug>` and draws the slug (`lib/projectLabel.tsx`). A single-org fleet
     keeps the plain-name key. */
  const qualifier = orgQualifierFor(slots.map(slot => ({ name: slot.project, org: slot.org })))
  const projectOrg = (slot: Worktree) => qualifier({ name: slot.project, org: slot.org })
  const projectFacet: Facet = {
    name: 'project',
    idOf: slot => {
      const org = projectOrg(slot)
      return org ? `${slot.project}@${org.slug}` : slot.project
    },
    labelOf: slot => slot.project,
    orgOf: projectOrg,
  }

  if (groupBy === GROUP_BY_FLAT) {
    /* Flat order = the default grouping's order minus bars (project, node, slot), so views compare. */
    return slots
      .slice()
      .sort(
        (a, b) =>
          compareGroups(
            { label: projectFacet.labelOf(a), id: projectFacet.idOf(a) },
            { label: projectFacet.labelOf(b), id: projectFacet.idOf(b) },
          ) ||
          compareGroups(
            { label: nodeFacet.labelOf(a), id: a.node_uuid },
            { label: nodeFacet.labelOf(b), id: b.node_uuid },
          ) ||
          compareSlots(a, b),
      )
      .map(slot => ({ kind: 'slot', depth: 0, key: slot.id, slot }) satisfies WorktreeRow)
  }

  const [outer, inner] =
    groupBy === GROUP_BY_NODE_PROJECT ? [nodeFacet, projectFacet] : [projectFacet, nodeFacet]

  const rows: WorktreeRow[] = []
  const outerGroups = [...bucketBy(slots, outer.idOf).entries()]
    .map(([id, members]) => ({ id, label: outer.labelOf(members[0]), org: outer.orgOf(members[0]), members }))
    .sort(compareGroups)

  for (const group of outerGroups) {
    const outerKey = `${outer.name}:${group.id}`
    const outerCollapsed = isCollapsed(outerKey, group.members.length)
    rows.push({
      kind: 'outer',
      depth: 0,
      key: outerKey,
      label: group.label,
      org: group.org,
      count: group.members.length,
      collapsed: outerCollapsed,
    })
    // A collapsed group emits its bar only — not its inner bars.
    if (outerCollapsed) continue

    const innerGroups = [...bucketBy(group.members, inner.idOf).entries()]
      .map(([id, members]) => ({ id, label: inner.labelOf(members[0]), org: inner.orgOf(members[0]), members }))
      .sort(compareGroups)

    for (const sub of innerGroups) {
      // Qualified by parent: the same project appears under every node carrying it.
      const innerKey = `${outerKey}/${inner.name}:${sub.id}`
      const innerCollapsed = isCollapsed(innerKey, sub.members.length)
      rows.push({
        kind: 'inner',
        depth: 1,
        key: innerKey,
        label: sub.label,
        org: sub.org,
        count: sub.members.length,
        collapsed: innerCollapsed,
      })
      if (innerCollapsed) continue
      for (const slot of sub.members.slice().sort(compareSlots)) {
        rows.push({ kind: 'slot', depth: 2, key: slot.id, slot })
      }
    }
  }
  return rows
}
