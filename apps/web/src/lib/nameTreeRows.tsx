/* A NAMED ROSTER'S TREE MODE, AS `ui/feed` ROWS — the read-only tree of the rosters that never re-file
 * (Automations, Playbooks).
 *
 * `lib/nameTree.ts` owns the SHAPE; the roster owns the LEAF (`deps.leafRow`), the ONE producer of its row
 * in either mode. This file owns the GROUP row and the mapping from outline to `FeedRowSpec`s. Pure and
 * hook-free: anything viewport-dependent is already inside `leafRow`, because the CALLER owns the
 * media-query hooks. What differs per roster — the noun, the DOM handles — is one `NameTreeKind`.
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
import type { FeedRowSpec, FeedSlot } from '../ui/feed/types'
import type { NamedRow, NameTreeKind, NameTreeNode } from './nameTree'
import { ELLIPSIS_SX } from '../ui/listStyles'

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

/** The ⋯ full-name item's label; the name itself is its reason line (`leafRow`). */
export const FULL_NAME_LABEL = 'Full name'

/* THE GROUP TITLE IS A FLEX ROW OF TWO BOXES: the label shrinks and ellipsises, the count never shrinks.
 * Inline children inside `FeedList`'s clipping `TITLE_SX` would cut the trailing count FIRST, and it has
 * no second home (no slot, no ⋯, no `href`). A clipped name is still recognisable and whole in the
 * tooltip; a partial count is useless. */
const TITLE_ROW_SX = { display: 'flex', alignItems: 'baseline', minWidth: 0 } as const

/** The group's name — the box that yields. Carries the ellipsis itself because the flex parent
 *  suppresses `TITLE_SX`'s. */
const GROUP_LABEL_SX = ELLIPSIS_SX

/** The dim leaf count beside a group's name; `flex: '0 0 auto'` keeps it whole (`TITLE_ROW_SX`). */
const COUNT_SX = {
  flex: '0 0 auto',
  color: 'text.disabled',
  ml: 0.5,
  fontVariantNumeric: 'tabular-nums',
} as const

export type NameTreeRowDeps<R extends NamedRow> = {
  /** Which roster this is: its noun and DOM handles. */
  kind: NameTreeKind
  /** The roster's own FLAT row for `row` — the one producer of a leaf in either mode. */
  leafRow: (row: R) => FeedRowSpec
  /** The group PATHS currently open (persisted by the roster's view store). */
  expanded: ReadonlySet<string>
  /** Open or shut ONE group, by path. The page owns the set. */
  onToggleGroup: (path: string) => void
}

/**
 * EVERY GROUP'S LEAF COUNT, by path, in ONE bottom-up walk. A LEAF count, not a child count. One walk,
 * not one per group: per-group is O(rows × depth) with operator-controlled depth.
 */
function leafCounts(nodes: readonly NameTreeNode<NamedRow>[]): Map<string, number> {
  const byPath = new Map<string, number>()
  const walk = (list: readonly NameTreeNode<NamedRow>[]): number => {
    let leaves = 0
    for (const node of list) {
      if (node.kind === 'leaf') {
        leaves += 1
        continue
      }
      const below = walk(node.children)
      byPath.set(node.path, below)
      leaves += below
    }
    return leaves
  }
  walk(nodes)
  return byPath
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

/** One group as a row, with its whole subtree already built beneath it. */
function groupRow<R extends NamedRow>(
  node: Extract<NameTreeNode<R>, { kind: 'group' }>,
  deps: NameTreeRowDeps<R>,
  blanks: readonly FeedSlot[],
  counts: ReadonlyMap<string, number>,
): FeedRowSpec {
  const { kind } = deps
  const prefix = kind.idPrefix
  const groupLabel = nameGroupLabel(kind)
  /* A lookup miss is unreachable; the count falls back to 0. */
  const count = counts.get(node.path) ?? 0
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
    href: null,
    /* The count reaches a screen reader here, and it names the PATH, not the label: `FeedList` emits no
       `aria-level`/`aria-posinset`, so `jira/prod` and `acme/prod` would otherwise be indistinguishable. */
    ariaLabel: `${groupLabel} ${node.path}, ${count} ${count === 1 ? kind.noun.one : kind.noun.many}`,
    disclosure: {
      expanded: deps.expanded.has(node.path),
      onToggle: () => deps.onToggleGroup(node.path),
      /* "credentials", matching the leaf count beside the name. */
      label: kind.noun.many,
      /* A mark-less folder's caret takes the disc's place, so it starts in its siblings' disc column. */
      onRail: kind.groupMark === false,
    },
    /* A copy per row, so an in-place edit downstream cannot rewrite every group's cluster at once. */
    slots: [...blanks],
    /* Handed over unconditionally; the primitive shows them only while open. */
    children: mapNodes(node.children, deps, blanks, counts, kind.groupMark === false),
    /* Oracle handles: path (the expansion set's identity) and the drawn count. */
    data: { [`${prefix}-group`]: node.path, [`${prefix}-group-count`]: count },
  }
}

/**
 * THE LEAF: the flat row with its title, accessible name, ⋯ menu and disclosure overridden, and no drag.
 * `slots` and `indicator` are untouched, so every value matches Flat mode.
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
  deps: NameTreeRowDeps<R>,
  spacer: boolean,
): FeedRowSpec {
  const flat = deps.leafRow(node.row)
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
    // Tree mode offers no drag, whatever the flat row carries.
    drag: undefined,
  }
}

/**
 * One sibling list, in `nameTree.ts` order. A leaf beside a group takes a `'spacer'` so titles
 * align; a list of only leaves does not, unless it sits in a mark-less folder (`inRailFolder`): that folder's
 * title starts after a spacer (its caret is on the rail), so its leaves keep one too and read as nested under it.
 */
function mapNodes<R extends NamedRow>(
  nodes: readonly NameTreeNode<R>[],
  deps: NameTreeRowDeps<R>,
  blanks: readonly FeedSlot[],
  counts: ReadonlyMap<string, number>,
  inRailFolder = false,
): FeedRowSpec[] {
  const spacer = inRailFolder || nodes.some((node) => node.kind === 'group')
  return nodes.map((node) =>
    node.kind === 'group'
      ? groupRow(node, deps, blanks, counts)
      : /* The one producer's flat row; `leafRow` carries the overrides. */
        leafRow(node, deps, spacer),
  )
}

/**
 * Tree mode's row list: the outline, in `nameTree.ts` order. `/`-free names are top-level leaves beside
 * the groups (R3.3(b)), never an "Other" bucket.
 */
export function nameTreeRows<R extends NamedRow>(
  nodes: readonly NameTreeNode<R>[],
  deps: NameTreeRowDeps<R>,
): FeedRowSpec[] {
  /* The cluster template, from a real leaf, so group rows follow the leaf's shape by construction. */
  const sample = firstLeaf(nodes)
  const blanks = blankedSlots(sample ? deps.leafRow(sample.row).slots : [])
  return mapNodes(nodes, deps, blanks, leafCounts(nodes))
}
