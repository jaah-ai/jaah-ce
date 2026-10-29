/* A NAMED ROSTER'S TREE MODE, AS `ui/feed` ROWS — the only place a group row or the omission node is
 * spelled, for every roster `lib/nameTree.ts` draws (Credentials, Playbooks).
 *
 * `lib/nameTree.ts` owns the SHAPE; the roster owns the LEAF (`deps.leafRow` — e.g. `lib/credentialRow.tsx`),
 * the ONE producer of its row in either mode. This file owns the GROUP row,
 * the OMISSION node, and the mapping from outline to `FeedRowSpec`s. Pure and hook-free: anything
 * viewport-dependent is already inside `leafRow`, because the CALLER owns the media-query hooks.
 * What differs per roster — the noun, the name cap, the DOM handles — is one `NameTreeKind`.
 *
 * `ui/feed/FeedList` already recurses to arbitrary depth and owns the FOLD (children are handed over
 * unconditionally). Legibility bound: the phone indent is three 10px steps then 6px per level
 * (`ui/feed/geometry.ts`) out of a 190px phone title, so ~26 segments reach `TITLE_FLOOR_PX`; the 200-char
 * name cap allows 100. Nothing clamps.
 *
 * GROUP ROW: `indicator` is required, so a neutral mark (`NAME_GROUP_GLYPH`) that claims nothing
 * about secrets. The leaf count rides the TITLE, not a slot: a slot costs width at 1024 and, gated,
 * would vanish at 448 with no second home (see `TITLE_ROW_SX`). Slots are a real leaf's array with
 * every value blanked (`blankedSlots`), so columns align. No `href`.
 *
 * TWO ACCEPTED DEVIATIONS FROM R3.6, BOTH FORCED BY N13 (no `ui/feed/*` edits):
 *   1. The expander is 18x22 at 448px, not 44px. A group row has no ⋯ menu, so there is no WCAG 2.5.8
 *      equivalent control; this rests on N13 alone and a change that may touch `ui/feed/*` should close it.
 *   2. Announced as a TABLE WITH EXPANDABLE ROWS (`/tasks`' pattern), not `role="tree"`. The disclosure is
 *      a focusable `<button>` with `aria-expanded`; children follow in DOM order.
 */
import Box from '@mui/material/Box'
import type { PointerEvent } from 'react'
import type { FeedDrag, FeedRowSpec, FeedSlot } from '../ui/feed/types'
import {
  NAME_PATH_SEPARATOR,
  nameSegments,
  type NamedRow,
  type NameTreeKind,
  type NameTreeNode,
} from './nameTree'
import {
  folderDropVerdict,
  isNameDraggable,
  movedName,
  nameDropVerdict,
  type NameFolderDrag,
} from './nameTreeMove'

/** THE GROUP'S MARK — inert and deliberately NOT DIRECTIONAL. `▸` is the console's disclosure caret
 *  (`components/ExpandToggleGlyph`), so an inert one would read as a fold control. `◈` is distinct from a leaf's `●`/`○` and is
 *  not tofu on this stack (probed in Chromium against a private-use codepoint). */
export const NAME_GROUP_GLYPH = '◈'

/** What the group's mark means — announced, since it is the only thing saying the row is not a leaf:
 *  "credential group". */
export function nameGroupLabel(kind: NameTreeKind): string {
  return `${kind.noun.one} group`
}

/** Group key prefix: a group is keyed by PATH (`nameTree.ts`), which could otherwise collide with a
 *  leaf's row id. */
const GROUP_KEY_PREFIX = 'group:'

/* THE TOP-LEVEL DROP TARGET — without it a credential dragged into a folder could never be dragged back
 * out. Drawn only during a drag that could use it (`nameTreeMove.ts::nameDropVerdict` refuses a row
 * already at top level), so the resting tree is unchanged. */
const TOP_LEVEL_KEY = '__top_level__'
export const NAME_TOP_LEVEL_GLYPH = '↥'
export const NAME_TOP_LEVEL_LABEL = 'top level'
/** The ⋯ full-name item's label; the name itself is its reason line (`leafRow`). */
export const FULL_NAME_LABEL = 'Full name'

/** THE OMISSION NODE'S KEY — a double-underscore sentinel that cannot collide with a uuid. It LEADS the
 *  list (see `overflowRow`). */
export const NAME_OVERFLOW_KEY = '__overflow__'

/* THE GROUP TITLE IS A FLEX ROW OF TWO BOXES: the label shrinks and ellipsises, the count never shrinks.
 * Inline children inside `FeedList`'s clipping `TITLE_SX` would cut the trailing count FIRST, and it has
 * no second home (no slot, no ⋯, no `href`). A clipped name is still recognisable and whole in the
 * tooltip; a partial count is useless. */
const TITLE_ROW_SX = { display: 'flex', alignItems: 'baseline', minWidth: 0 } as const

/** The group's name — the box that yields. Carries the ellipsis itself because the flex parent
 *  suppresses `TITLE_SX`'s. */
const GROUP_LABEL_SX = {
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
} as const

/** The omission node's shortfall — the half that never yields. */
const SHORTFALL_SX = { flex: '0 0 auto', whiteSpace: 'nowrap' } as const

/** The advice half: dim, and the first to ellipsise. */
const ADVICE_SX = {
  flex: '0 1 auto',
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  color: 'text.disabled',
} as const

/** The dim leaf count beside a group's name; `flex: '0 0 auto'` keeps it whole (`TITLE_ROW_SX`). */
const COUNT_SX = {
  flex: '0 0 auto',
  color: 'text.disabled',
  ml: 0.75,
  fontVariantNumeric: 'tabular-nums',
} as const

/** What is in flight: one row, or a whole folder. */
export type NameDragItem<R extends NamedRow> =
  | { kind: 'leaf'; row: R }
  | ({ kind: 'folder' } & NameFolderDrag)

/** The deps of a roster that never re-files (Automations, Playbooks): `dragging: null` is the whole off switch,
 *  the rest its inert companions, and the tree is read-only and complete. Spread first; the roster adds its own. */
export const NO_REFILING_DEPS = {
  canEdit: false, omitted: 0, dragging: null, dragOver: null, canDrag: false, canDragFolders: false, onLift: () => {},
} as const

export type NameTreeRowDeps<R extends NamedRow> = {
  /** Which roster this is: its noun, name cap and DOM handles. */
  kind: NameTreeKind
  /** The roster's own FLAT row for `row` — the one producer of a leaf in either mode. */
  leafRow: (row: R, busy: boolean) => FeedRowSpec
  /** The caller's edit key; a row's own `editable` outranks it (a folder lifts only if every leaf
   *  beneath it is editable). */
  canEdit: boolean
  /** WHICH row has a write in flight. An ID, not a boolean: this module builds a whole subtree from ONE
   *  deps object, so a shared `busy: true` would mark every row busy. */
  busyId: string | null
  /** The group PATHS currently open (persisted by the roster's view store). */
  expanded: ReadonlySet<string>
  /** Open or shut ONE group, by path. The page owns the set. */
  onToggleGroup: (path: string) => void
  /** How many rows of the filtered set are NOT here (`total − items.length`, R3.4). Zero or less means
   *  the tree is complete and nothing is claimed; otherwise the tree says so itself — a tree from a slice
   *  would silently misreport group contents. */
  omitted: number

  /* RE-FILING BY DRAGGING. `dragging: null` IS THE WHOLE OFF SWITCH: no row carries `drag.drop` and no
     top-level row is drawn. */
  /** The row or folder in flight, or `null` when nothing is being dragged. */
  dragging: NameDragItem<R> | null
  /** The drop key under the pointer (`FeedDrag.dropKey`), or `null`. A KEY, resolved from the DOM — see
   *  `nameDropKey`. */
  dragOver: string | null
  /** The roster's edit key (`credentials:edit`). The server is the authoritative gate; this only stops a
   *  row OFFERING a refused gesture. */
  canDrag: boolean
  /** May a FOLDER be lifted? Only over a complete, unfiltered tree: the server moves every credential
   *  the caller can read beneath it, so a filtered or partial tree would move rows nobody saw (it
   *  refuses that anyway, 409 on the `ids`). Leaves are unaffected. */
  canDragFolders: boolean
  /** A row was pressed. The page owns threshold and release (`hooks/usePointerDrag`). */
  onLift: (item: NameDragItem<R>, event: PointerEvent<HTMLElement>) => void
}

/** The deps every builder below reads: the caller's, plus what `nameTreeRows` computes once. */
type BuildDeps<R extends NamedRow> = NameTreeRowDeps<R> & {
  /** Every loaded leaf name, normalised — a folder drag's exact collision test. Empty unless a folder
   *  is in flight (nothing else reads it). */
  allNames: ReadonlySet<string>
}

/* THE DROP KEY: a destination must be ONE DOM ATTRIBUTE VALUE (`hooks/usePointerDrag.ts` resolves it from
 * the element under the pointer). A PREFIX, not a sentinel: group paths are operator text (`top/thing`
 * is legal), and the prefix round-trips anything. ONLY A ROW THAT WILL TAKE THE DROP CARRIES ONE — the
 * org rule is judged against the destination's own subtree, a fact only the outline has, so the page
 * must not re-derive the verdict; the key's existence settles it. */
const GROUP_DROP_PREFIX = 'group:'
export const NAME_TOP_LEVEL_DROP_KEY = 'top-level'

/** The key for a destination: a group's path, or `null` for the top level. */
export function nameDropKey(path: string | null): string {
  return path === null ? NAME_TOP_LEVEL_DROP_KEY : `${GROUP_DROP_PREFIX}${path}`
}

/** The ROOT's own stats key. `nameTree.ts` drops empty segments, so no group path is `''`. */
export const NAME_ROOT_STATS_KEY = ''

/** What a group holds, everywhere beneath it — computed ONCE per tree by `nameSubtreeStats`. */
export type NameSubtreeStats = {
  /** Every leaf below, however deep — the number R3.5 puts on a group row. */
  leaves: number
  /** Every distinct organization below; `null` is a VALUE ("no org"), tested by
   *  `nameTreeMove.ts::nameDropVerdict`. */
  orgs: ReadonlySet<string | null>
  /** Last segment of every leaf DIRECTLY in this folder (collisions are sibling-only, so not
   *  accumulated upward). */
  leafNames: ReadonlySet<string>
  /** May the caller rename EVERY leaf below? A folder drag moves them all or none, so one read-only
   *  leaf makes the folder un-liftable (the server refuses it 403 too). */
  editable: boolean
}


/**
 * WHAT EVERY GROUP HOLDS, by path, in ONE bottom-up walk. A LEAF count, not a child count. One walk,
 * not one per group: per-group is O(rows × depth) with operator-controlled depth, paid per search
 * keystroke (`Credentials.tsx`'s `treeRows` block has the measurement).
 */
export function nameSubtreeStats<R extends NamedRow>(
  nodes: readonly NameTreeNode<R>[],
  canEditRow: (row: R) => boolean = () => true,
): Map<string, NameSubtreeStats> {
  const byPath = new Map<string, NameSubtreeStats>()
  // The root is an entry too, under `''`: the top-level drop row needs its direct leaf names.
  byPath.set(NAME_ROOT_STATS_KEY, collectStats(nodes, byPath, canEditRow))
  return byPath
}

function collectStats<R extends NamedRow>(
  nodes: readonly NameTreeNode<R>[],
  byPath: Map<string, NameSubtreeStats>,
  canEditRow: (row: R) => boolean,
): { leaves: number; orgs: Set<string | null>; leafNames: Set<string>; editable: boolean } {
  let leaves = 0
  let editable = true
  const orgs = new Set<string | null>()
  const leafNames = new Set<string>()
  for (const node of nodes) {
    if (node.kind === 'leaf') {
      leaves += 1
      if (!canEditRow(node.row)) editable = false
      // `?? null` makes "no org" ONE value a folder can be single-tenant in.
      orgs.add(node.row.org_id ?? null)
      // The exact string a rename to top level would write; `null` only for an all-separator name.
      const leaf = movedName(node.row.name, null)
      if (leaf !== null) leafNames.add(leaf)
      continue
    }
    const below = collectStats(node.children, byPath, canEditRow)
    byPath.set(node.path, below)
    leaves += below.leaves
    if (!below.editable) editable = false
    for (const org of below.orgs) orgs.add(org)
  }
  return { leaves, orgs, leafNames, editable }
}

/** Every leaf name beneath `nodes`, normalised — a folder's `names` at lift (one walk, on the press). */
function subtreeNames(nodes: readonly NameTreeNode<NamedRow>[], into: string[] = []): string[] {
  for (const node of nodes) {
    if (node.kind === 'leaf') into.push(nameSegments(node.row.name).join(NAME_PATH_SEPARATOR))
    else subtreeNames(node.children, into)
  }
  return into
}

/** Every row id beneath `nodes`. */
function subtreeIds(nodes: readonly NameTreeNode<NamedRow>[], into: string[] = []): string[] {
  for (const node of nodes) {
    if (node.kind === 'leaf') into.push(node.row.id)
    else subtreeIds(node.children, into)
  }
  return into
}

/** Every distinct org beneath `nodes`, `null` a value. */
function subtreeOrgs(nodes: readonly NameTreeNode<NamedRow>[], into = new Set<string | null>()): Set<string | null> {
  for (const node of nodes) {
    if (node.kind === 'leaf') into.add(node.row.org_id ?? null)
    else subtreeOrgs(node.children, into)
  }
  return into
}

/** The first leaf in arrival order — the template every blanked slot array borrows. `null` only for an
 *  empty outline (every group has a leaf beneath it). */
function firstLeaf<R extends NamedRow>(
  nodes: readonly NameTreeNode<R>[],
): Extract<NameTreeNode<R>, { kind: 'leaf' }> | null {
  for (const node of nodes) {
    if (node.kind === 'leaf') return node
    const found = firstLeaf(node.children)
    if (found) return found
  }
  return null
}

/**
 * The leaf's cluster with every VALUE removed and every DIMENSION kept. `title`, `data` and `testId`
 * go with `node`, since each carries the leaf's value in another form (a tooltip, a `data-org-slug`, a
 * test handle found twice). The rest is alignment.
 */
function blankedSlots(slots: readonly FeedSlot[] | undefined): FeedSlot[] {
  return (slots ?? []).map(({ key, width, gate, align, mono, dim, fontSize }) => ({
    key,
    width,
    gate,
    align,
    mono,
    dim,
    fontSize,
    node: null,
  }))
}

/**
 * A DESTINATION'S `FeedDrag`. `undefined` with no drag in flight. The verdict is
 * `nameTreeMove.ts`'s; this only maps it to accept / over / reject.
 */
function dropDrag<R extends NamedRow>(
  deps: BuildDeps<R>,
  path: string | null,
  below: NameSubtreeStats | undefined,
  lift?: Pick<FeedDrag, 'onLift' | 'lifted'>,
): FeedDrag | undefined {
  const dragged = deps.dragging
  if (!dragged) return undefined
  /* AN UNKNOWN SUBTREE FAILS CLOSED: empty sets would satisfy both the org and collision rules
     vacuously. Unreachable for an outline this module built. */
  if (below === undefined) {
    return { ...lift, dragging: true, drop: 'reject', dropReason: 'This folder’s contents are not loaded' }
  }
  const verdict = dragged.kind === 'leaf'
    ? nameDropVerdict(deps.kind, dragged.row, path, below.orgs, below.leafNames)
    : folderDropVerdict(deps.kind, dragged, path, below.orgs, deps.allNames)
  if (!verdict.ok) {
    /* A refusing row carries NO `dropKey`, so the gesture resolves nothing on it. The folder in flight
       refuses itself ("into itself") and draws as lifted rather than as a red refusal. */
    if (lift?.lifted) return { ...lift, dragging: true }
    return { ...lift, dragging: true, drop: 'reject', dropReason: verdict.reason }
  }
  const key = nameDropKey(path)
  return {
    ...lift,
    dragging: true,
    drop: deps.dragOver === key ? 'over' : 'accept',
    dropKey: key,
  }
}

/** A non-destination row during a drag: stops text selection, draws no wash, carries no `dropKey`. */
function passThroughDrag<R extends NamedRow>(deps: BuildDeps<R>): FeedDrag | undefined {
  return deps.dragging ? { dragging: true } : undefined
}

/**
 * THE OMISSION NODE — R3.4's visible statement that the tree is not the whole filtered set.
 *
 * IT LEADS THE ROOT LIST: as the last of up to 200 root nodes it would sit below the fold. (Contrast
 * `pages/credentials/api.ts::overflowOption`, which trails in a dropdown.) `warning`, not error: nothing
 * failed, and the copy names the way out.
 */
function overflowRow<R extends NamedRow>(
  omitted: number,
  blanks: readonly FeedSlot[],
  spacer: boolean,
  deps: BuildDeps<R>,
): FeedRowSpec {
  const prefix = deps.kind.idPrefix
  /* Two halves: as one string this clipped at 448px and lost the ACTION. The shortfall never shrinks;
     the advice ellipsises. */
  const shortfall = `…and ${omitted} more not shown`
  const advice = ' — narrow the filters'
  /* The whole sentence stays one string for the tooltip and accessible name. */
  const sentence = `${shortfall}${advice}`
  return {
    key: NAME_OVERFLOW_KEY,
    indicator: {
      tone: 'warning',
      glyph: '!',
      label: `not every ${deps.kind.noun.one} is shown`,
    },
    // Takes the spacer beside expandable siblings so the left edge is not ragged.
    disclosure: spacer ? 'spacer' : undefined,
    title: (
      <Box component="span" sx={TITLE_ROW_SX}>
        <Box component="span" sx={SHORTFALL_SX}>
          {shortfall}
        </Box>
        <Box component="span" data-testid={`${prefix}-omitted-advice`} sx={ADVICE_SX}>
          {advice}
        </Box>
      </Box>
    ),
    titleText: sentence,
    /* Silences the `!` disc — the sentence already says it. */
    ariaLabel: sentence,
    slots: [...blanks],
    /* An oracle reads the NUMBER, not the copy. */
    data: { [`${prefix}-omitted`]: omitted },
    // Not a destination (its rows are not loaded), but it still reports the pointer.
    drag: passThroughDrag(deps),
  }
}

/**
 * THE TOP-LEVEL DROP TARGET (see `TOP_LEVEL_KEY`). Not a group: no path, count or
 * disclosure, and gone on release.
 */
function topLevelRow<R extends NamedRow>(
  deps: BuildDeps<R>,
  blanks: readonly FeedSlot[],
  spacer: boolean,
  root: NameSubtreeStats | undefined,
): FeedRowSpec {
  const prefix = deps.kind.idPrefix
  /* Name and advice split like the omission node; "Top level" alone survives the ellipsis. */
  const advice = '— drop here to take it out of its folder'
  const sentence = `Top level ${advice}`
  return {
    key: TOP_LEVEL_KEY,
    indicator: {
      tone: 'neutral',
      glyph: NAME_TOP_LEVEL_GLYPH,
      label: NAME_TOP_LEVEL_LABEL,
    },
    disclosure: spacer ? ('spacer' as const) : undefined,
    title: (
      <Box component="span" sx={TITLE_ROW_SX}>
        <Box component="span" data-testid={`${prefix}-top-level`} sx={SHORTFALL_SX}>
          Top level
        </Box>
        {/* Gap is padding: a leading space collapses against the previous inline box. */}
        <Box component="span" sx={[ADVICE_SX, { pl: 0.5 }]}>
          {advice}
        </Box>
      </Box>
    ),
    titleText: sentence,
    // Silences the inert mark.
    ariaLabel: sentence,
    slots: [...blanks],
    /* The root's own stats: no org test at top level (`nameDropVerdict`), but the collision test
       applies — stripping a prefix is the likeliest way to collide. */
    drag: dropDrag(deps, null, root),
    data: { [`${prefix}-top-level`]: true },
  }
}

/** One group as a row, with its whole subtree already built beneath it. */
function groupRow<R extends NamedRow>(
  node: Extract<NameTreeNode<R>, { kind: 'group' }>,
  deps: BuildDeps<R>,
  blanks: readonly FeedSlot[],
  stats: ReadonlyMap<string, NameSubtreeStats>,
): FeedRowSpec {
  const { kind } = deps
  const prefix = kind.idPrefix
  const groupLabel = nameGroupLabel(kind)
  /* A lookup miss is unreachable: the COUNT falls back to 0 (cosmetic), the DROP fails closed. */
  const below = stats.get(node.path)
  const count = below?.leaves ?? 0
  return {
    key: `${GROUP_KEY_PREFIX}${node.path}`,
    indicator: kind.groupMark === false ? undefined : {
      tone: 'neutral',
      glyph: NAME_GROUP_GLYPH,
      label: groupLabel,
    },
    /* The label is never blank (`nameTree.ts`'s `nameSegments` drops empty segments). The two boxes
       carry test ids so an oracle reads name and count apart without relying on order or a regex. */
    title: (
      <Box component="span" sx={TITLE_ROW_SX}>
        <Box component="span" data-testid={`${prefix}-group-name`} sx={GROUP_LABEL_SX}>
          {node.label}
        </Box>
        <Box component="span" data-testid={`${prefix}-group-count`} sx={COUNT_SX}>
          ({count})
        </Box>
      </Box>
    ),
    /* Label alone: the tooltip and the disclosure's noun ("Expand secrets of jira"). */
    titleText: node.label,
    /* The count reaches a screen reader here, and it names the PATH, not the label: `FeedList` emits no
       `aria-level`/`aria-posinset`, so `jira/prod` and `acme/prod` would otherwise be indistinguishable. */
    ariaLabel: `${groupLabel} ${node.path}, ${count} ${count === 1 ? kind.noun.one : kind.noun.many}`,
    disclosure: {
      expanded: deps.expanded.has(node.path),
      onToggle: () => deps.onToggleGroup(node.path),
      /* "credentials", matching the leaf count beside the name. */
      label: kind.noun.many,
    },
    /* A copy per row, so an in-place edit downstream cannot rewrite every group's cluster at once. */
    slots: [...blanks],
    /* Handed over unconditionally; the primitive shows them only while open. */
    children: mapNodes(node.children, deps, blanks, stats),
    /* Oracle handles: path (the expansion set's identity) and the drawn count. */
    data: { [`${prefix}-group`]: node.path, [`${prefix}-group-count`]: count },
    /* A group is a destination, judged on its OWN subtree's org set — and a source. */
    drag: groupDrag(node, deps, below),
  }
}

/**
 * THE GROUP'S DRAG: always a destination during a drag; a SOURCE when the caller may rename every leaf
 * beneath it and no write is in flight (the leaf's rule, `leafDrag`). The folder's `names`/`orgs` are
 * gathered on the press, not per render.
 */
function groupDrag<R extends NamedRow>(
  node: Extract<NameTreeNode<R>, { kind: 'group' }>,
  deps: BuildDeps<R>,
  below: NameSubtreeStats | undefined,
): FeedDrag | undefined {
  const liftable =
    deps.canDrag && deps.canDragFolders && deps.omitted <= 0 && deps.busyId === null && below?.editable === true
  const onLift = liftable
    ? (event: PointerEvent<HTMLElement>) =>
        deps.onLift(
          {
            kind: 'folder',
            path: node.path,
            ids: subtreeIds(node.children),
            names: subtreeNames(node.children),
            orgs: subtreeOrgs(node.children),
          },
          event,
        )
    : undefined
  if (!deps.dragging) return onLift ? { onLift } : undefined
  const lifted = deps.dragging.kind === 'folder' && deps.dragging.path === node.path
  return dropDrag(deps, node.path, below, { onLift, lifted })
}

/**
 * THE LEAF: the flat row with exactly three overrides. `slots` and `indicator` are untouched, so every
 * value matches Flat mode.
 *
 * `ariaLabel` ENDS WITH `indicator.label`: supplying `ariaLabel` makes `FeedRow` mark the state disc
 * `aria-hidden`, so a bare name would drop the row's secret state from the accessibility tree.
 *
 * THE DRAWN TITLE IS THE LAST SEGMENT (a flagged deviation from "identical title"): at 448px a depth-3
 * leaf's 160px clips `jira/prod/eu-west/webhooks` to `…/webh…`, cutting the one part the group rows above
 * do not already spell. The full name stays in `titleText`, `ariaLabel`, the `href` and the ⋯ menu.
 * Naming semantics do not move. An all-separator name has no segment, so `node.label` is the raw name
 * (R3.3(e)).
 */
function leafRow<R extends NamedRow>(
  node: Extract<NameTreeNode<R>, { kind: 'leaf' }>,
  deps: BuildDeps<R>,
  spacer: boolean,
): FeedRowSpec {
  const busy = deps.busyId === node.row.id
  const flat = deps.leafRow(node.row, busy)
  /* THE FULL NAME'S TOUCH-REACHABLE HOME: a `title` tooltip is unreachable on touch, so the ⋯ menu gets
     a DISABLED info item carrying it (`rowActions` puts `disabledReason` on title and accessible name),
     after "Copy name". Tree mode only — in Flat the title is the full name. A row that has NO ⋯ menu
     gets none (a menu holding only this item would be a control that does nothing); its full name is
     one tap away on the page its title links to. The name is drawn ONCE, as the reason line: `OverflowMenu`
     draws both label and reason, and only the reason wraps (a long label would be clipped). */
  const actions = [...(flat.actions ?? [])]
  const copyNameAt = actions.findIndex((item) => item.key === 'copy-name')
  // `findIndex` -1 would splice at 0; append instead.
  if (actions.length > 0) actions.splice(copyNameAt < 0 ? actions.length : copyNameAt + 1, 0, {
    key: 'full-name',
    icon: '⌥',
    label: FULL_NAME_LABEL,
    ariaLabel: FULL_NAME_LABEL,
    disabled: true,
    disabledReason: node.row.name,
    onClick: () => {},
  })
  return {
    ...flat,
    title: node.label,
    /* The full name, then what the silenced disc and markers said: a marker (System, Built-in) is `aria-hidden`
       on a named row too (`FeedList`), so its label rides here or is lost to AT. */
    ariaLabel: [node.row.name, ...(flat.markers ?? []).map((marker) => marker.label), flat.indicator?.label]
      .filter(Boolean)
      .join(', '),
    actions: actions.length > 0 ? actions : flat.actions,
    disclosure: spacer ? ('spacer' as const) : undefined,
    drag: leafDrag(node.row, deps),
  }
}

/**
 * THE LEAF'S HALF OF THE DRAG: a source, never a destination (dropping onto a credential has no
 * predictable meaning). Liftable only with the edit key, no write in flight, and a last segment to
 * re-hang (`nameTreeMove.ts`).
 */
function leafDrag<R extends NamedRow>(row: R, deps: BuildDeps<R>): FeedDrag | undefined {
  /* NO LIFT WHILE **ANY** WRITE IS IN FLIGHT: `busyId` is one slot shared with Delete, so a second
     gesture's `finally` would clear the busy state of a row whose request is still on the wire. */
  const liftable = deps.canDrag && deps.busyId === null && isNameDraggable(row)
  const onLift = liftable
    ? (event: PointerEvent<HTMLElement>) => deps.onLift({ kind: 'leaf', row }, event)
    : undefined
  if (!deps.dragging) return onLift ? { onLift } : undefined
  const lifted = deps.dragging.kind === 'leaf' && deps.dragging.row.id === row.id
  return { dragging: true, onLift, lifted }
}

/**
 * One sibling list, in `nameTree.ts` order. A leaf beside a group takes a `'spacer'` so titles
 * align; a list of only leaves does not.
 */
function mapNodes<R extends NamedRow>(
  nodes: readonly NameTreeNode<R>[],
  deps: BuildDeps<R>,
  blanks: readonly FeedSlot[],
  stats: ReadonlyMap<string, NameSubtreeStats>,
): FeedRowSpec[] {
  const spacer = nodes.some((node) => node.kind === 'group')
  return nodes.map((node) =>
    node.kind === 'group'
      ? groupRow(node, deps, blanks, stats)
      : /* The one producer's flat row; `leafRow` carries the overrides. */
        leafRow(node, deps, spacer),
  )
}

/**
 * Tree mode's row list: the omission node (when incomplete), the outline, then the top-level drop row
 * during a drag. `/`-free names are top-level leaves beside the groups (R3.3(b)), never an "Other"
 * bucket.
 */
export function nameTreeRows<R extends NamedRow>(
  nodes: readonly NameTreeNode<R>[],
  callerDeps: NameTreeRowDeps<R>,
): FeedRowSpec[] {
  /* Only a folder drag reads `allNames`, so only a folder drag pays the walk. */
  const deps: BuildDeps<R> = {
    ...callerDeps,
    allNames: callerDeps.dragging?.kind === 'folder' ? new Set(subtreeNames(nodes)) : new Set(),
  }
  /* The cluster template, from a real leaf, so group rows follow the leaf's shape by construction. */
  const sample = firstLeaf(nodes)
  /* `busy: false`: only slot dimensions survive `blankedSlots`. */
  const blanks = blankedSlots(sample ? deps.leafRow(sample.row, false).slots : [])

  /* The leaf's edit verdict (the server's `editable`, where the roster sends one, outranks the key). */
  const stats = nameSubtreeStats(nodes, (row) => row.editable ?? deps.canEdit)
  const rows = mapNodes(nodes, deps, blanks, stats)
  const spacer = nodes.some((node) => node.kind === 'group')

  /* THE TOP-LEVEL ROW GOES LAST: appearing first would reflow the tree under a moving pointer
     mid-gesture. Not drawn when it would only refuse. */
  const head = deps.omitted > 0 ? [overflowRow(deps.omitted, blanks, spacer, deps)] : []
  const tail: FeedRowSpec[] = []
  if (deps.dragging) {
    const top = topLevelRow(deps, blanks, spacer, stats.get(NAME_ROOT_STATS_KEY))
    if (top.drag?.drop !== 'reject') tail.push(top)
  }
  return head.length + tail.length > 0 ? [...head, ...rows, ...tail] : rows
}
