// The Task page's static vocabulary: its tab ids and labels, and the layout `sx` its shell and sections share.
import type { TabDef } from '../../components/Tabs'
import { FONT_SIZE, LINE_HEIGHT, RADIUS, SIZE, grid } from '../../theme'
import { ABOVE_PHONE, BOTTOM_NAV_HEIGHT, PHONE, TASK_TABS_ROW_CONTAINER, TASK_TABS_ROW_NARROW, TASK_TRAIL_CONTAINER } from '../../ui/listStyles'

// `subtasks` only with children; `artifacts` only once a runner handed one back; `qa` only at a gate or once
// the task has a Q&A trail; `agent` only with an ACP session. `task`, `comments`, `routing` and `details` are
// unconditional (`routing` off the Live Cast viewer).
export type TaskTab =
  'task' | 'subtasks' | 'artifacts' | 'qa' | 'comments' | 'agent' | 'routing' | 'details'

/* Retired `?tab=` ids, so old links (notifications, bookmarks) still land: the gate's Answer merged into Q&A (once
   Decisions), Discuss became Comments, Audit became Details, and Metrics, Activity, Outcome and Review findings are
   Details' subtabs (`LEGACY_TAB_SUBTABS` lands the last two on theirs). */
export const TASK_TAB_ALIASES: Readonly<Record<string, TaskTab>> = {
  answer: 'qa',
  decisions: 'qa',
  discuss: 'comments',
  audit: 'details',
  metrics: 'details',
  activity: 'details',
  outcome: 'details',
  findings: 'details',
}

export const TASK_TAB: TabDef<TaskTab> = { id: 'task', label: 'Task' }
export const SUBTASKS_TAB: TabDef<TaskTab> = { id: 'subtasks', label: 'Subtasks' }
export const ARTIFACTS_TAB: TabDef<TaskTab> = { id: 'artifacts', label: 'Artifacts' }
/* What was asked and how it was settled: the open gate's form (answerable or read-only) over the Q&A trail. */
export const QA_TAB: TabDef<TaskTab> = { id: 'qa', label: 'Q&A' }
/* Unconditional: it is a place to WRITE, so hiding it would block the first comment. The count rides
   in the label. */
export function commentsTab(count: number): TabDef<TaskTab> {
  return { id: 'comments', label: count > 0 ? `Comments (${count})` : 'Comments' }
}
/* The ACP run's live event stream, its own tab: it sat under Details' field grid, a second scroll
   inside a tab about something else. Only once the task has an ACP session, and only for a reader of its events
   (`sessions:view` + `sessions:edit`, `TaskActivity`). */
export const AGENT_TAB: TabDef<TaskTab> = { id: 'agent', label: 'Agent' }
/* What the agent router decided for this task's dispatches (`components/routing/RoutingPanel`): every task, every
   mode, an empty state before the first decision, while the org's `intelligent_routing` switch is on (the operator
   always). Not on the Live Cast viewer: the public API serves no routing. */
export const ROUTING_TAB: TabDef<TaskTab> = { id: 'routing', label: 'Routing' }
export const DETAILS_TAB: TabDef<TaskTab> = { id: 'details', label: 'Details' }

/* The Details tab's inner strip (`?details=`): everything ABOUT the run, one subtab each. Only the subtabs with something
   to show are drawn (Summary always); the first drawn is the default. */
export type DetailsSubtab = 'summary' | 'outcome' | 'metrics' | 'timeline' | 'versions' | 'review'

export const DETAILS_SUBTABS = [
  { id: 'summary', label: 'Summary' },
  // The outcome ledger's rows for the task (`TaskOutcomePanel`): only once it has one.
  { id: 'outcome', label: 'Outcome' },
  // Run figures (tier 3, runs) split out of Summary, so Summary leads with what the task IS and how it ended.
  { id: 'metrics', label: 'Metrics' },
  { id: 'timeline', label: 'Timeline' },
  { id: 'versions', label: 'Versions' },
  // What review lanes found in the task's change (`TaskReviewFindingsPanel`): only once there is one.
  { id: 'review', label: 'Review' },
] as const satisfies readonly TabDef<DetailsSubtab>[]

/* Retired top-level tabs that are now Details' subtabs: `?tab=outcome` lands on `?tab=details&details=outcome`. */
export const LEGACY_TAB_SUBTABS: Readonly<Record<string, DetailsSubtab>> = { outcome: 'outcome', findings: 'review' }

/* SANCTIONED SCROLLPORT 3: a history feed (`data-testid="task-page-feed"`), unbounded by construction — Details' Timeline
   and Versions share one, and Q&A's trail is the same kind of feed. No `overscroll-behavior: contain`: at
   phone width this grows to its content and the shell is what scrolls, and a contained non-scroller swallows that
   gesture (`PAGE_SX`'s "WHY NO SCROLLPORT ON THIS PAGE CONTAINS ITS OVERSCROLL"). */
export const FEED_SX = {
  display: 'flex',
  flexDirection: 'column',
  gap: 1.5,
  flex: '1 1 auto',
  minHeight: grid(21),
  overflowY: 'auto',
} as const

// AD-13: create mode shows only the `task` panel (the others would render empty). Same id, and the word
// follows the `<h1>` ("Chat" under "New Chat").
export const CREATE_TABS: readonly TabDef<TaskTab>[] = [{ id: 'task', label: 'Task' }]
export const CREATE_CHAT_TABS: readonly TabDef<TaskTab>[] = [{ id: 'task', label: 'Chat' }]

/* ── THE TAB-STRIP ROW, and the rating's slot on its right ────────────────────
   `& > :first-of-type` is the `Tabs` root: `flex: '1 1 auto'` + `minWidth: 0` let the scrollable strip
   shrink (scrolling tabs, not clipping). Positional because `Tabs` exposes only a `className`. The row is a
   size container (`TASK_TABS_ROW_CONTAINER`): the rating's place follows the ROW's width, so the page in the
   list's split pane at a wide viewport lays out as a narrow one does. */
export const TABS_ROW_SX = {
  display: 'flex',
  alignItems: 'stretch',
  // Load-bearing in a narrow row (see `RATING_SLOT_SX`'s `flexBasis: '100%'`).
  flexWrap: 'wrap',
  containerType: 'inline-size',
  containerName: TASK_TABS_ROW_CONTAINER,
  '& > :first-of-type': { flex: '1 1 auto', minWidth: 0 },
  /* A narrow row (the split pane, a phone) drops MUI's 90px tab floor and its 16px gutters, so more tabs fit
     before the strip's More (`overflowMenu`). */
  [TASK_TABS_ROW_NARROW]: {
    '& .MuiTab-root': { minWidth: 0, px: 1 },
  },
} as const

/* Whether the rating shares the strip's row: the tabs at their compact width plus the label-less stars must fit
   the ROW (measured, `useElementWidth`), else it folds into the header's ⋯ rather than taking a row
   of its own. Per-tab and star widths measured at 1440/1024 split and 390. */
export const TAB_COMPACT_PX = 78
export const RATING_INLINE_PX = 110

export const RATING_SLOT_SX = {
  flex: '0 0 auto',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'flex-end',
  // Should a long tab set still push it down, it lands right-aligned, not under the first tab.
  ml: 'auto',
  pl: 1,
  /* The strip's hairline is inside `Tabs`' root, which now spans only the tabs; this continues it
     under the rating with the same token. */
  borderBottom: 1,
  borderColor: 'divider',
} as const

/* The loading title crumb: about a title's width, inline in the trail, capped to the header on a phone. */
export const TITLE_SKELETON_SX = { display: 'inline-block', width: grid(42.5), maxWidth: '60vw', verticalAlign: 'middle' } as const

/* The create surface's loading trail and, beside it, the phone's org-scope chip: where the loaded create header draws
   it, so it does not move when the form lands. */
export const LOADING_TRAIL_SX = {
  display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1.5, rowGap: 0.5, minWidth: 0,
} as const

/* ── THE LOADING SHELL: the loaded page's geometry, drawn before its read lands ───────────────────────
   The header over a tab strip and a body, each in the box the loaded page puts there (the same `PAGE_SX`, whose
   `HEADER_SLOT` / `TABS_SLOT` style the shell's own `task-page-header-loading` / `task-page-tabs-loading` too — own
   ids, so a wait on the loaded page's never lands on the skeleton), so nothing shifts when the row lands. The strip is the shared
   `Tabs`' height (36px; the 44px tap floor on the phone) and hairline, its tabs bars of a typical label's width.
   The cluster's skeleton holds the header row at the ⋯'s height (32px; 44px on the phone, `PAGE_OVERFLOW_SX`). */
export const LOADING_TABS_SX = {
  display: 'flex',
  alignItems: 'center',
  gap: 3,
  px: 2,
  minHeight: { xs: 44, sm: 36 },
  borderBottom: 1,
  borderColor: 'divider',
  overflow: 'hidden',
} as const
export const LOADING_TAB_WIDTHS = [grid(6.5), grid(12), grid(9), grid(10.5)] as const
export const LOADING_ACTIONS_SX = {
  width: grid(16),
  height: SIZE.thumb,
  [PHONE]: { height: SIZE.tapTarget },
} as const

/* The header's ⋯, sized to sit beside the state badge rather than
   in a table cell; the 44px touch floor on the phone. */
export const PAGE_OVERFLOW_SX = {
  width: SIZE.thumb,
  minWidth: SIZE.thumb,
  height: SIZE.thumb,
  fontSize: FONT_SIZE.lg,
  lineHeight: LINE_HEIGHT.tight,
  borderRadius: RADIUS.sm,
  [PHONE]: { width: SIZE.tapTarget, minWidth: SIZE.tapTarget, height: SIZE.tapTarget },
} as const

// Details' visible subtab panel (and the shared Timeline · Versions feed section), a column like a top-level panel.
const DETAILS_SUBPANEL = '& > #task-panel-details > [role="tabpanel"]:not([hidden]), & > #task-panel-details > [data-details-feed]:not([hidden])'

/* The header and tab-strip slots, loaded or loading (the shell above): the loading id first, so the flattened rule
   still ends in the loaded id (`TaskDetailPage.header.test.tsx` reads it). */
const HEADER_SLOT = '& > [data-testid="task-page-header-loading"], & > [data-testid="task-page-header"]'
const TABS_SLOT = '& > [data-testid="task-page-tabs-loading"], & > [data-testid="task-page-tabs"]'

/* ── The page shell, as `sx` ──────────────────────────────────────────────────────────────────────
   Rides on `DetailPage measure="fill"`, whose shell declarations are single-homed in
   `ui/detail/DetailPage.tsx` and merged AHEAD of this object, so the phone block overrides them. What is
   left are selectors naming this box's children (`& > …`), which must ride on THIS box, plus the ≤480px
   override of the measure (full bleed, `height: auto`, the visual-viewport guard). */
export const PAGE_SX = {
  /* 8px between the bands (the measure's 16px halved), at every width: the header sits right over its tabs. */
  gap: 1,
  // The header and the tab strip are fixed chrome; only the active panel flexes.
  /* The status pill stays TOP-RIGHT whatever the title's length: the header never wraps, the trail takes the
     rest of the line (basis 0, so a long title wraps inside it instead of pushing the pill to a line below). */
  [HEADER_SLOT]: {
    flex: '0 0 auto',
    mb: 0,
    flexWrap: 'nowrap',
    alignItems: 'flex-start',
    // The trail's width as a query container (`TaskBreadcrumb.tsx`'s System chip): basis 0, so it never sizes by content.
    '& > [data-slot="lead"]': { flex: '1 1 0%', containerType: 'inline-size', containerName: TASK_TRAIL_CONTAINER },
    /* Above the phone the one-line trail ellipsises beside the pill, so the cluster keeps its width and its one row
       there: the trail gives way, never the pill. The phone's wrapping trail lets a wide cluster wrap. */
    [ABOVE_PHONE]: { '& > [data-slot="actions"]': { flex: '0 0 auto' } },
    /* ON A PHONE THE TITLE TAKES ITS OWN LINE once the pill's cluster would leave it under ~10rem: a
       30-character title wrapped to three lines in the sliver between the crumbs and the pill. The header wraps, the
       lead keeps a 10rem basis (full width when wrapped), and the cluster lands right-aligned on the line below. */
    [PHONE]: {
      flexWrap: 'wrap',
      '& > [data-slot="lead"]': { flex: `1 1 ${grid(26.5)}` },
      '& > [data-slot="actions"]': { ml: 'auto' },
    },
  },
  [TABS_SLOT]: { flex: '0 0 auto', '& .MuiTabs-root': { mb: 0 } },
  /* `display` only on the visible panel — unconditional flex would override `[hidden]`. Direct
     children only; Details' subtab panels take the same column through their own rule below. */
  '& > [role="tabpanel"]:not([hidden])': {
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
    flex: '1 1 auto',
    minHeight: 0,
  },
  [DETAILS_SUBPANEL]: {
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
    flex: '1 1 auto',
    minHeight: 0,
  },

  /* SANCTIONED SCROLLPORT 1 — the Description, inside the edit form. No `minHeight` here on purpose:
     `TaskEditForm` owns both floors on its own element; a page-scoped descendant rule (0,2,0) would
     out-specify it and shrink the phone floor. One floor, one owner. */
  /* ── WHY NO SCROLLPORT ON THIS PAGE CONTAINS ITS OVERSCROLL ─────────────────────────────────────
     At <=480px the page is `height: auto` and `.app-main` is the only real scrollport, yet
     `overscroll-behavior: contain` on a box with nothing to scroll still stops a gesture chaining out
     in Chromium — so drags over the Description, editor pane or feed moved nothing (phone "cannot
     scroll"). Where these boxes do scroll (desktop, keyboard-up) there is nothing above to chain to, and
     Android pull-to-refresh does not apply. So `touch-action` stays and containment goes — here, on the
     feed, on `TaskEditForm`, and on `styles.css` `.rte-content`, each pointing back at this note.
     Releasing any ONE `contain` alone does nothing — the chain stops at the next one up; all go together. */

  '& [data-testid="task-page-description"]': {
    flex: '1 1 auto',
    overflowY: 'auto',
    // Read-only prose sizes to its content, still a scrollport when long (`TaskSpecFields`).
    '&[data-readonly="true"]': { flex: '0 0 auto' },
  },

  /* SANCTIONED SCROLLPORT 2 — the Subtasks rows (`components/tasks/SubtaskList.tsx`). Its phase headings are
     sticky, so they stay in view while their rows scroll under them. 12rem floors ~6 rows (30px each). */
  '& > #task-panel-subtasks [data-testid="subtask-list"]': { flex: '1 1 auto', minHeight: grid(32), overflowY: 'auto' },

  [PHONE]: {
    /* A1d — FULL-BLEED: cancel `.app-main`'s 1rem phone inset with the -1rem idiom (no fixed positioning,
       no viewport units); the gutter goes back to the header and tab strip. */
    marginLeft: '-1rem',
    marginRight: '-1rem',


    /* AC1d.2 — the software keyboard. `100dvh` ignores the on-screen keyboard, so the `visualViewport`
       effect publishes the height available below this element's top; absent the API the `var()` falls
       back to 100%. A MINIMUM, not a fixed height — a phone may scroll. `min()` with 100% keeps the page
       clear of `.app-main`'s 76px bottom-nav inset, which the visual viewport knows nothing about. */
    height: 'auto',
    minHeight: 'min(var(--task-page-vv-height, 100%), 100%)',

    /* …AND, WITH THE KEYBOARD UP ON A FORM SURFACE, the same guard as a CEILING. The minimum alone lets
       the page grow to its content, leaving Save below the visible band where nothing scrolls to it
       (`.app-main` sizes from the layout viewport). Ceilinged, the form overflows into its own scrollport
       with a sticky action row (`TaskEditForm`). Both conditions are load-bearing:
       · `[data-keyboard="up"]` — without it the ceiling applies at rest, where the page must absorb a
         tall form (AC-6(a)'s ≤1.15 outer-scroll allowance).
       · `:has([data-testid="task-form-card"])` — other tabs need no ceiling. */
    '&[data-keyboard="up"]:has([data-testid="task-form-card"])': {
      maxHeight: 'var(--task-page-vv-height, 100%)',
      // The ceiling needs `overflow: hidden` or the overflow simply spills.
      overflow: 'hidden',

      /* …and the one state where the form and Description ARE scrollports (the exception to ONE
         SCROLLPORT PER PHONE below). Declared here because only the page sees `[data-keyboard]`; the
         page-scoped rule (0,2,0) out-specifies the form's own (0,1,0). */
      '& [data-testid="task-form-card"], & [data-testid="task-page-description"]': { overflowY: 'auto' },

      /* …AND THE PAGE CHROME STANDS DOWN WHILE THE OPERATOR IS TYPING. With the keyboard up, pure
         orientation chrome (the header, the tab strip, a closed Advanced disclosure) gives its height to
         the editor — the form's only `flex: 1` child — so the visible band holds usable lines
         (`e2e/tasks-mobile.spec.ts`, the editor's keyboard-up case). Keyed on `[data-keyboard="up"]`; nothing is
         removed from the DOM or a11y tree in any other state. */
      [`${HEADER_SLOT}, ${TABS_SLOT}`]: { display: 'none' },
      /* `:not([open])` is the safety: an open Advanced disclosure holds real inputs and may be why the
         keyboard is up. A closed summary is not an input. */
      '& [data-testid="form-advanced"]:not([open])': { display: 'none' },
    },

    /* ── …AND WITH THE KEYBOARD DOWN, THE ACTION BAR IS PINNED TO THE VIEWPORT ───────────
       At <=480px the form is `overflowY: visible` and the scrollport is `main[data-testid="app-main"]`, so
       the row's `sticky; bottom: 0` resolves against the form's box, past the fold. Pinned `fixed` above
       the nav instead: no transform/filter/contain ancestor (measured), `bottom` = the nav's height
       (`BOTTOM_NAV_HEIGHT`), `zIndex: 5` under the nav's 40. Keyboard-down only: `fixed` uses the layout
       viewport, so with the keyboard up it would ride behind it (the ceiling block handles that state). */
    '&:not([data-keyboard="up"])': {
      '& [data-testid="task-form-actions"]': {
        position: 'fixed',
        left: 0,
        right: 0,
        bottom: BOTTOM_NAV_HEIGHT,
        zIndex: 5,
        paddingLeft: 2.5,
        paddingRight: 2.5,
        paddingBottom: 1,
      },
      /* Reserve the bar's height below the form so its last control scrolls clear. 11 units (66px) = the bar's
         53px + 6px padding, rounded up. On the PANEL, not the card: padding inside the card inflates
         `scrollHeight`, which `tasks-mobile.spec.ts`'s AC-4 chrome-density guard bounds. */
      '& > [role="tabpanel"]:not([hidden]):has([data-testid="task-form-card"])': {
        paddingBottom: 11,
      },
    },

    // The full-bleed gutter is handed back to the two pieces of page chrome.
    [`${HEADER_SLOT}, ${TABS_SLOT}`]: {
      paddingLeft: 2.5,
      paddingRight: 2.5,
    },

    /* AC1e.4 — the tab strip at 390px: 44px touch floor, scoped to this strip. Tabs GROW to fill a short strip
       but never SHRINK below their label: with seven tabs a shrinkable tab clipped into its
       neighbour ("TaskSubtasksArtifacts…"); the strip scrolls behind its edge fade instead
       (`components/Tabs.tsx`). */
    '& > [data-testid="task-page-tabs"] .MuiTab-root': {
      minHeight: SIZE.tapTarget,
      py: 1,
      px: 1.5,
      flex: '1 0 auto',
      textAlign: 'center',
      whiteSpace: 'nowrap',
      minWidth: 0,
    },

    /* A panel takes its CONTENT's height on a phone (a flex split would squeeze the shortest card), and
       carries the 12px phone gutter the full bleed removed (`tasks-mobile.spec.ts` floors flush-edge
       content). In ONE rule with the flex: a duplicate
       key would replace it. */
    '& > [role="tabpanel"]:not([hidden])': {
      flex: '0 0 auto',
      paddingLeft: 2,
      paddingRight: 2,
    },
    [DETAILS_SUBPANEL]: { flex: '0 0 auto' },
    /* …and the same gutter on the page's banners (`TaskDetailBanners`, and the acting-for notice's wrapper), which
       sit outside the panels: full-bleed, they ran into the screen edge. The failure banner's dense Details fold
       gets the 44px touch floor its `AdvancedSection` leaves off for table cells. */
    '& > .MuiAlert-root, & > :has([data-testid="task-page-acting-for"])': { marginLeft: 2, marginRight: 2 },
    '& [data-testid="task-page-terminal-cause-details-summary"]': { minHeight: SIZE.tapTarget, minWidth: SIZE.tapTarget },
    /* …except the panel hosting the edit form, whose Description editor is `flex: 1` and must grow.
       Keyed on the form, which is what changes the requirement. */
    '& > [role="tabpanel"]:not([hidden]):has([data-testid="task-form-card"])': {
      flex: '1 1 auto',
      minHeight: 0,
    },

    /* Desktop floors would force a scroll on a phone with the keyboard up. The Description is absent on
       purpose: `TaskEditForm` owns its larger phone floor, and a page-scoped rule here would out-specify
       and clamp it. */
    '& [data-testid="task-page-feed"]': { minHeight: grid(16) },

    /* ── ONE SCROLLPORT PER PHONE ────────────────────────────────────────────────────────────────
       At <=480px the page grows, so these boxes can never scroll themselves; `visible` stops them being
       scroll containers in the chain (see the overscroll note above). The exception is keyboard-up,
       restored inside the ceiling block; the feed needs none (that block is form-gated). */
    '& [data-testid="task-page-description"], & [data-testid="task-page-feed"], & [data-testid="task-form-card"]':
      { overflowY: 'visible' },
  },
} as const
