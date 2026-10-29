/* ONE TASK AS A FEED ROW — the Activity style's row, as facts (`ui/feed/FeedList.tsx` draws it). Shared by the
 * Activity feed (`./ActivityFeedView.tsx`) and a task's subtask list (`../SubtaskList.tsx`), so both draw
 * literally the same row. */
import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import Checkbox from '@mui/material/Checkbox'
import type { PipelineTask } from '../../../types'
import { useIsPicked, type PickStore } from '../../../lib/pickStore'
import { disclosureGlyph } from '../../ExpandToggleGlyph'
import AuthorBadge from '../AuthorBadge'
import { startIntentFor } from '../controls'
import CostBadge from '../CostBadge'
import RatingBadge from '../RatingBadge'
import { TaskPrSummary, taskPrs } from '../TaskPrList'
import { taskOverflowItems, taskRowActionFlags } from '../rowActions'
import type { OverflowItem, TaskRowActionHandlers } from '../rowActions'
import { indicatorHover, indicatorName, indicatorPress, SYSTEM_ROW_TITLE, TASK_STATUS, taskIndicatorAction, taskStatus } from '../shared'
import { DEFAULT_PRIORITY, PRIORITY_LEVELS, priorityLabel } from '../taskPriority'
import { TASK_DRAG_TYPE } from '../rail/TaskTreeRow'
// One home for the rating wording, shared with the badge's `title`.
import { ratingSummaryTitle } from '../../../lib/taskRating'
// The token figure and its exact-count sentence, from their single home (the Table's Tokens cell reads the same).
import { tokensLabel, tokensTitle } from '../../../lib/taskTokens'
import { ABOVE_PHONE, BOARD_NO_RATING, monoSx, SELECT_BOX_SX, SR_ONLY_SX } from '../../../ui/listStyles'
import { orgSlots } from '../../../ui/OrgIndicator'
import { AGE_SLOT } from '../../../ui/feed/geometry'
import type { FeedMarker, FeedNote, FeedRowSpec, FeedSlot } from '../../../ui/feed/types'
import type { ViewRow } from './rows'
import FeatureIcon from '../FeatureIcon'
import { parseFeatureIcon } from '../../../lib/featureIcon'

/* The task-shaped slots: `ui/feed` owns the cluster's shape; this board decides each width and the type
   properties a `FeedSlot` cannot express. */
/* The persona rides `boardPersona` (`BOARD_NO_PERSONA`, ≤860px of board) — FIRST to go. MEASURED at a
   1440px window with the board narrowed: the title is 397px just above the gate and 477px just below. */
const PERSONA_SLOT = 78

/* THE PR SLOT — 132px: the first PR as `owner/name #123` (the repository ellipsises, the number never does), a
   state dot and "+N" for the rest (`../TaskPrList.tsx`). Rides `boardPr` (`BOARD_NO_PR`, ≤980px of board), the
   FIRST to go, priced so the title keeps its width just above the persona's gate. Reserved, not collapsed, as
   the cost is: most finished rows have a PR. Left-aligned so the repository names start in one column. */
const PR_SLOT = 132

/* THE RATING SLOT — 36px. A fixed slot's cost comes out of the title.
   Content at this cluster's 11px (`RATING_SX`): `4.5` + star ≈ 3 x ~6.6 + 1.5 + ~9.9 ≈ 31px, +5px tolerance
   (`../RatingBadge` quotes ~37px at its 13px default).
   Rides `boardRating` (`BOARD_NO_RATING`, ≤680px of board), the LAST of the four to go — the operator's order
   is persona, tokens, cost, rating. MEASURED at a 1440px window with the board
   narrowed, before the tokens slot: the title is 371px just above the gate and 409px just below (the tokens
   slot is already gone there). */
const RATING_SLOT = 36

/* `RatingBadge` sets `fontSize: '0.8125rem'` before composing `sx`, so the 11px the 36px rests on must be
   passed. Mono, weight and colour come from the badge. The reserved blank is the SLOT's (the badge is null
   at count 0); reservation beats collapse because rated rows are RARE and would step the columns 42px. */
const RATING_SX = { fontSize: '0.6875rem' } as const

/* THE TOKENS SLOT — 48px, priced like `RATING_SLOT`.
   The widest `lib/taskTokens.ts` figures are six characters (`999.9K`, `999.9M`): 6 x ~6.6 ≈ 40px mono at
   11px, +8px tolerance (the compact rule never widens past a decimal and a unit letter).
   Rides `boardTokens` (`BOARD_NO_TOKENS`, ≤810px of board): second to go, after the persona and BEFORE the
   cost — tokens are the secondary figure beside the dollars, as the Table reads them. Not
   re-measured: the persona's and cost's title figures below predate it; above its gate the title is 48px
   narrower than they say. */
const TOKENS_SLOT = 48

/* Host geometry for the token figure: the cluster's 11px (`COST_SX`'s reason), the mono face so the last digits
   share a column with the cost's, and `text.secondary` so the dollars stay the lead figure (the Table's
   reading). RESERVED, NOT COLLAPSED, for the cost's reason: nearly every dispatched row has a figure, so
   collapsing would step the columns on the rare uncaptured one. The blank is the slot's (the node is
   `undefined` when `tokensLabel` is empty — never 0). */
const TOKENS_SX = { fontSize: '0.6875rem', display: 'inline-block', color: 'text.secondary' } as const

/* THE COST SLOT — 64px, priced like `RATING_SLOT`.
   Sized for nine characters (`$1,204.50`): 9 x ~6.6 ≈ 59px mono at 11px, +5px tolerance. MEASURED at 900px:
   59.42px inside 64, no ellipsis. The figure is now whole dollars (`costLabel`), so `$999,999` still fits.
   Rides `boardCost` (`BOARD_NO_COST`, ≤760px of board): third to go, after the persona and the tokens.
   MEASURED at a 1440px window with the board narrowed, before the tokens slot:
   the title is 381px just above the gate and 447px just below (the tokens slot is already gone there). */
const COST_SLOT = 64

/* `CostBadge` sets `fontSize: '0.8125rem'` in its layout defaults, so the 11px must be passed as host
   geometry. Face, weight, colour and `textAlign: 'right'` come from the badge.
   RESERVED, NOT COLLAPSED: nearly every dispatched row has a cost, so collapsing would step the columns on
   the rare costless one; reserving keeps author/age/⋯ one straight line. The blank is the badge's own null. */
const COST_SX = { fontSize: '0.6875rem' } as const

const AUTHOR_SLOT = 24

/* `../AuthorBadge` owns initials, hue and the name on `title` (`lib/authorBadge.ts` says why the hue: two
   letters collide). The slot adds weight and tracking — a 400-weight `RZ` at 10px is a smudge. */
const AUTHOR_SX = { fontWeight: 700, letterSpacing: '0.02em' } as const

/* THE ORG SLOT — 72px, riding the `wide` GATE (`ui/feed/geometry.ts`'s `SlotGate`; derivation in
   `ui/listStyles.ts`'s `ORG_HIDDEN_ON_MOBILE_SX`, the single home of the boundary and title curve).
   WHY `wide`, NOT `squeeze` (owner decision): mobile web must not show the org name at all.
   Absent ≤1023px (phone band included), present ≥1024 (the supported desktop
   floor) — a single monotonic gate, like `meta`/`id` just at a wider boundary.
   WHY 128: the token draws the org NAME (`ui/OrgIndicator.tsx`), priced there on `Northwind Trading`; longer
   names ellipsise, with name and slug on `title`.
   MEASURED title with/without the slot at the old 72px (floor from `e2e/tasks-feed.spec.ts`; 56px more now):
       448px   312            floor  40                   ABSENT — mobile never pays for it
       481px    90            floor  40                   UNPAID — hidden through this band
       900px   258.5          floor 200                   UNPAID
      1024px   382.5 -> 304.5 floor 200   104.5px clear   PAID (desktop floor)
      1440px   798.5 -> 720.5 floor 200   520.5px clear   PAID
   Row height unmoved (24px at 448, 30px above; 44px at 481 is the touch floor). The e2e oracle reads the
   TOKEN's box (it ellipsises internally) with `northwind` in the fixture.
   PLACEMENT: immediately left of the ungated tail (`SlotGate`'s rule — kept where `squeeze` sat, since a
   monotonic `wide` gate could equally be ordered by strength but moving it earns no width back).
   `orgSlot` owns the `title`, `data-*` and the null em dash, so no presence branch here. */
const ORG_SLOT_FEED = 128

/* THE BULK PICK'S TICK RIDES THE AGE SLOT (as its own 26px column after the age,
   the tick narrowed every title at a 1024px split). The age shows at rest; the tick takes its place while the row is
   hovered or holds keyboard focus (a Tab reaches the tick, so `:focus-visible` reveals it; not `:focus-within`,
   or the focus a click leaves on the box pins an unticked tick after the pointer goes), while it is
   ticked, and on every pickable row once anything is picked (`picking`), so a pick in progress shows all its boxes. The 24px box
   (`SELECT_BOX_SX`) fits the 28px slot. A hover-less (touch) device never reveals by hover: the ⋯ menu's Select
   is its way in, after which `picking` shows the rest. Hidden is `opacity` plus `pointer-events`, never
   `display`, so the tick stays in the tab order and hover never reflows the row. */
const PICK_HOST_SX = { position: 'relative', display: 'block' } as const
const PICK_REVEALED = {
  '& .feed-pick': { opacity: 1, pointerEvents: 'auto' },
  '& .feed-age': { visibility: 'hidden' },
} as const
const PICK_TICK_SX = {
  ...SELECT_BOX_SX,
  position: 'absolute',
  top: '50%',
  right: 0,
  transform: 'translateY(-50%)',
  opacity: 0,
  pointerEvents: 'none',
} as const
const pickHostSx = (revealed: boolean) => [
  PICK_HOST_SX,
  {
    '@media (hover: hover)': { '[data-testid="feed-row"]:hover &': PICK_REVEALED },
    '[data-testid="feed-row"]:has(:focus-visible) &': PICK_REVEALED,
  },
  revealed ? PICK_REVEALED : null,
]

/* THE TICK READS ITS OWN ROW from the pick store: a click re-renders this tick, never the board, so
   rapid clicks never queue behind a full re-render. Its reveal follows the live answer too. */
function FeedPickTick({ store, taskId, picking, title, age, onToggle }: {
  store: PickStore<PipelineTask>
  taskId: number
  picking: boolean
  title: string
  age: string
  onToggle: () => void
}) {
  const checked = useIsPicked(store, taskId)
  return (
    <Box component="span" data-testid="feed-pick-host" sx={pickHostSx(checked || picking)}>
      <span className="feed-age">{age}</span>
      <Checkbox
        size="small"
        className="feed-pick"
        data-testid="feed-task-select"
        checked={checked}
        onChange={onToggle}
        slotProps={{ input: { 'aria-label': `Select ${title}` } }}
        sx={PICK_TICK_SX}
      />
    </Box>
  )
}

/* A QUEUED ROW'S PRIORITY (the deleted Queue style showed it on every queued row) — a mark hugging the
   name, only off the default: Normal is nearly every row, and a mark on each would be noise that hides the few
   bumped ones. Hugged, it costs no column (`FeedMarker.hug`), so the phone keeps it too. Only on `ready`: a
   running or finished row's priority no longer orders anything. `null` = no mark. */
function queuedPriority(row: ViewRow): number | null {
  if (row.state !== 'ready') return null
  const value = row.task.priority ?? DEFAULT_PRIORITY
  return value === DEFAULT_PRIORITY ? null : value
}

/* NOTICEABLE, NOT JUST PRESENT: a grey 10px arrow read as noise. The bump wears a TONE — Urgent the
   warning colour, High the info colour, Low plain `text.secondary` (a demotion is a fact, not an alarm) — and its
   level as a WORD ("High") beside the arrow wherever the board keeps its rating column (`boardRating`, so a split
   at its floor and the phone keep the arrow alone and the name keeps its width). Colour is never the only signal:
   the glyph differs per level, and the word and the row's name say it. */
function priorityMarker(value: number): FeedMarker {
  const urgent = value >= PRIORITY_LEVELS[0].value
  const glyph = urgent ? '⇈' : value > DEFAULT_PRIORITY ? '↑' : '↓'
  return {
    key: 'priority',
    glyph,
    label: `${priorityLabel(value)} priority`,
    hug: true,
    tone: urgent ? 'warning' : value > DEFAULT_PRIORITY ? 'info' : 'neutral',
    word: { text: priorityLabel(value), gate: 'boardRating' },
    testId: 'feed-priority',
    data: { priority: value },
  }
}

/* WHY A NOT-YET-RUNNING ROW WAITS, as one sentence (`Waiting. Organization runner limit (1/1)`), or `null` when its state
   word says all there is. The single home for the row's hover note, its accessible name and the ⋯
   menu's reason line, so the three never word it apart. Held has one cause (its epic's hold mode), which its bare
   word does not say. */
function waitSentence(row: ViewRow): string | null {
  const held = row.stateLabel === TASK_STATUS.held.label
  if (held && row.stateTitle === row.stateLabel) return `${row.stateLabel}. Epic on hold`
  const waiting = held || row.stateLabel === TASK_STATUS.waiting.label || row.stateLabel === TASK_STATUS.queued.label
  return waiting && row.stateTitle !== row.stateLabel ? row.stateTitle : null
}

/* The row's accessible name. Carries the state WORD (the glyph is only the sighted reader's indicator). ITS
   PRESENCE SILENCES THE DISC AND MARKERS (`FeedRowSpec.ariaLabel`). */
export function ariaLabelFor(row: ViewRow): string {
  const progress = row.epic ? `, ${row.doneChildCount} of ${row.childCount} subtasks done` : ''
  const author = row.createdBy ? `, created by ${row.createdBy}` : ''
  /* THE RATING IS IN THE NAME: the badge sits inside the row's `Link`, whose `aria-label` REPLACES its contents.
     `ratingSummaryTitle` is the wording's single home; unrated rows add nothing. */
  const rating = row.ratingCount > 0 ? `, rated ${ratingSummaryTitle(row.ratingCount, row.ratingAverage)}` : ''
  // The priority mark is silenced with the markers (`FeedRowSpec.ariaLabel`), so its words ride here.
  const queued = queuedPriority(row)
  const priority = queued === null ? '' : `, ${priorityLabel(queued)} priority`
  /* THE COST AND THE TOKENS ARE DELIBERATELY NOT HERE: slots are `role="cell"` SIBLINGS of the naming cell (not
     inside the `Link`), and neither `CostBadge` nor the token figure (with its screen-reader " tokens" unit) is
     `aria-hidden`, so each cell names itself; appending either would repeat it. */
  /* THE WAIT REASON IS IN THE NAME: the disc's hover is ~20px of target and no hover at all on a touch
     device, so `Waiting. Organization runner limit (1/1)` replaces the bare word wherever it adds a fact. */
  const state = waitSentence(row) ?? row.stateLabel
  return `Task ${row.id}: ${row.title} — ${state}${priority}${progress}${author}${rating}`
}

/** Everything about one row that the host knows and `./rows` does not. */
export type RowContext = {
  row: ViewRow
  /** The row's indent: 0 on the rail. The feed nests one level. */
  depth: number
  /** A subtask (its Start means Run, and it is no drag source). Defaults to `depth > 0` — the feed's reading;
   *  a host whose rows are all subtasks at depth 0 passes it (`../SubtaskList.tsx`). */
  leaf?: boolean
  deepLinked: boolean
  /** THE DOM ID `pages/Tasks.tsx` SCROLLS TO (`getElementById(taskRowElementId(id))`) for a deep link
   *  (`/tasks?task=<id>`). ONE ELEMENT MAY CARRY IT: a multi-tagged row appears under several sections; the
   *  host picks which. */
  elementId?: string
  handlers: TaskRowActionHandlers
  busy: boolean
  /** `undefined` for a row that cannot expand. */
  expanded?: boolean
  fetching: boolean
  onToggle?: () => void
  /** What the open disclosure shows above the children (why an expansion produced nothing). */
  detail?: ReactNode
  children?: FeedRowSpec[]
  /** The router's handle: the `?` press navigates, but the disc is ONE element (button or link by state), and
   *  swapping element types would fork its geometry, focus ring and `data-press`. Threaded: this is a builder. */
  navigate: (to: string) => void
  /** Tenant visibility (`ui/OrgIndicator.tsx::useOrgVisible`, the single home), threaded like `navigate`. */
  orgVisible: boolean
  /** The row's feature icon (`featureIconFor`), drawn before the title text; absent/null draws nothing. */
  featureIcon?: string | null
  /** The bulk pick's tick, riding the age slot (`PICK_HOST_SX`): absent or `null` = no tick on this row (the board
   *  offers no selection, or nobody may pick the row). `picking`: something on the board is picked, so every
   *  tick shows at rest. `checked` is the pick when the board last rendered — the ⋯ menu's word; the tick reads
   *  `store` live. Also the ⋯ menu's Select, the hover-less device's way in. */
  select?: { checked: boolean; picking: boolean; store: PickStore<PipelineTask>; onToggle: () => void } | null
}

/* ONE TASK ROW, AS FACTS — no pixels. */
export function buildFeedTaskRow(ctx: RowContext): FeedRowSpec {
  const { row, depth, handlers, busy, navigate } = ctx
  const leaf = ctx.leaf ?? depth > 0

  /* Verbs derived and listed by `../rowActions` (shared with the table); `variant` makes a subtask's Start mean
     Run rather than Queue. */
  const variant = leaf ? 'leaf' : 'root'
  const flags = taskRowActionFlags(row.task, variant, handlers)
  /* THE NODE IS A CONTROL, AND SO IS THE ROW BEHIND IT. Which verb the node offers is `taskIndicatorAction`
     (`../shared`) — the table pill's rule: console on an in-flight slot row, Start on draft/paused, the ANSWER
     SCREEN when asking, else nothing. This view supplies only the three closures; `onAnswer` is omitted when
     `answerHref` (from `./rows`) is null, keeping the asking rule single-homed.
     THE ROW BODY CARRIES THE CONSOLE ONLY: never Start and never the answer screen — a stray click on a 30px row
     must not launch a runner or leave the board. */
  const openConsole =
    flags.showConsole && handlers.onOpenConsole !== undefined
      ? () => handlers.onOpenConsole!(flags.consoleSlot!)
      : undefined
  /* A blocked Start (`flags.startBlockedReason`) is no press here: the ⋯ carries it disabled with the reason. */
  const start =
    handlers.onStart !== undefined && !flags.startBlockedReason
      ? () => handlers.onStart!(row.task, startIntentFor(row.task, variant))
      : undefined
  /* From `row.answerHref`, not `row.id`: a container's destination is the gated CHILD. Null → no closure at all,
     which `taskIndicatorAction` reads as "no answer navigation". */
  const answer = row.answerHref !== null ? () => navigate(row.answerHref!) : undefined
  /* `taskStatus` gives the verb KEY; the reader is told `row.stateLabel` (`toViewRow`'s refinements), and the
     glyph comes from the same vocabulary.
     NARROWING: the parent's gating (`toViewRow`'s `parent`) is not passed here — it turns Queued into
     Held/Waiting, neither of which offers a verb, so the verb is the same either way. */
  const indicator = taskIndicatorAction(taskStatus(row.task), row.title, {
    onStart: start,
    onOpenConsole: openConsole,
    onAnswer: answer,
  })
  /* The rollup's exact `17/18` survives here (hover) and in `ariaLabelFor` (the ring is only an angle). It is
     one of the row's facts, so it sits before what a click does. */
  const rollupClause =
    row.epic && row.pct !== null ? `${row.doneChildCount}/${row.childCount} subtasks done` : null
  /* THE HOVER SENTENCE: the state and its facts, where its runner works, then what a click does
     (`indicatorHover`). From `row.stateTitle`, not `indicator.title`, so the hover never says "Starting" under a
     node reading "Running". */
  const nodeTitle = indicatorHover(
    rollupClause === null ? row.stateTitle : `${row.stateTitle}. ${rollupClause}`,
    indicator.kind,
    flags.consoleSlot,
  )
  const nodeName = indicator.verb
    ? indicatorName(row.stateLabel, indicator.verb, row.title)
    : row.stateLabel

  const select = ctx.select
  const wait = waitSentence(row)
  const overflowItems: OverflowItem[] = [
    ...taskOverflowItems(row.task, row.title, variant, handlers, flags),
    // The tick's second home (a hover-less device never reveals it): see `PICK_HOST_SX`.
    ...(select
      ? [{
          key: 'pick',
          icon: select.checked ? '☑' : '☐',
          label: select.checked ? 'Deselect' : 'Select',
          ariaLabel: `${select.checked ? 'Deselect' : 'Select'} ${row.title}`,
          onClick: select.onToggle,
        }]
      : []),
  ]
  /* THE DISCLOSURE IS ALSO THE ⋯'S FIRST ITEM — the table's pattern (every inline control is also a menu item):
     the menu lists everything, and the item's name carries the row's title. A menu that would hold ONLY the
     disclosure (a row with no verb, e.g. the Live Cast viewer's) is no menu: the row's own caret already is it. */
  const verbs: OverflowItem[] =
    ctx.expanded === undefined || overflowItems.length === 0
      ? overflowItems
      : [
          {
            key: 'disclose',
            icon: disclosureGlyph(ctx.expanded),
            label: ctx.expanded ? 'Collapse subtasks' : 'Expand subtasks',
            ariaLabel: `${ctx.expanded ? 'Collapse' : 'Expand'} subtasks of ${row.title}`,
            onClick: () => ctx.onToggle?.(),
          },
          ...overflowItems,
        ]
  /* THE WAIT REASON LEADS THE ⋯ — a disabled line, pressing nothing: on a phone the ⋯ (and the
     long-press beside it) is where a reader goes for "why is this not running", and the disc's hover never shows
     there. Its words are `waitSentence`'s, split at the state word so the menu reads "Waiting" over its cause.
     Only on a row that already has a menu: a verbless row's reason is its name and its hover. */
  const items: OverflowItem[] =
    wait === null || verbs.length === 0
      ? verbs
      : [
          {
            key: 'wait-reason',
            icon: row.glyph,
            label: row.stateLabel,
            ariaLabel: row.stateLabel,
            disabled: true,
            disabledReason: wait.startsWith(`${row.stateLabel}. `) ? wait.slice(row.stateLabel.length + 2) : wait,
            testId: 'feed-wait-reason',
            onClick: () => {},
          },
          ...verbs,
        ]

  /* THE TRAILING CLUSTER — PR, persona, rating, tokens, cost, author (767, phone included), ORG (`wide`), age ungated (see each
     `*_SLOT`). On a board narrower still (an editor and the console beside it) the org, then the author, follow
     the board too (`BOARD_NO_ORG` / `BOARD_NO_AUTHOR`).
     Persona, tokens, cost and rating follow the BOARD's width and drop in the operator's order — persona, then
     tokens, then cost, then rating (`ui/listStyles.ts`'s `BOARD_NO_*`) — so the
     rating can outlive the cost to its right; each column still lines up because every row drops the same
     slots. The org cannot join the order; left of the ungated tail is `SlotGate`'s position for it. The rating
     branch tests the data only: `ratingCount` is already gated to done-and-ratable rows in `./rows`. The tokens
     branch is `tokensLabel`'s verdict, nothing re-spelled. The cost has no branch — see its slot. */
  const tokens = tokensLabel(row.tokens)
  const slots: FeedSlot[] = [
    /* Its own cell, outside the row's link, so its links are valid; `TaskPrSummary` keeps its clicks off the row. */
    {
      key: 'pr',
      testId: 'col-pr',
      width: PR_SLOT,
      gate: 'boardPr',
      align: 'left',
      node: taskPrs(row.task).length > 0 ? <TaskPrSummary prs={taskPrs(row.task)} taskTitle={row.title} /> : undefined,
    },
    { key: 'persona', testId: 'col-persona', width: PERSONA_SLOT, gate: 'boardPersona', align: 'right', title: row.persona, node: row.persona },
    {
      key: 'rating',
      testId: 'col-rating',
      width: RATING_SLOT,
      gate: 'boardRating',
      align: 'right',
      node:
        row.ratingCount > 0 ? (
          <RatingBadge average={row.ratingAverage} count={row.ratingCount} sx={RATING_SX} testId="feed-rating" />
        ) : undefined,
    },
    /* THE TOKENS, between the verdict and the dollars they were priced from: the Table's order too, so the
       operator reads "12.4K · $0.42" the same in both styles. The branch is `tokensLabel`'s (`''` = nothing
       captured, never 0), so the slot's blank is reserved like the cost's. The figure is NOT `aria-hidden` and
       names its own unit to a screen reader (the `SR_ONLY_SX` suffix — there is no header to borrow it from), so
       the cell names itself and `ariaLabelFor` omits it; the `title` carries the exact count. */
    {
      key: 'tokens',
      testId: 'col-tokens',
      width: TOKENS_SLOT,
      gate: 'boardTokens',
      // Right-aligned so the last digits share a column with the cost's (hence mono).
      align: 'right',
      node:
        tokens !== '' ? (
          <Box
            component="span"
            data-testid="feed-tokens"
            title={tokensTitle(row.tokens)}
            sx={theme => ({ ...monoSx(theme), ...TOKENS_SX })}
          >
            {tokens}
            <Box component="span" sx={SR_ONLY_SX}> tokens</Box>
          </Box>
        ) : undefined,
    },
    /* THE COST, right after the tokens: persona, verdict and spend are the WORK group; author and age the
       PROVENANCE pair, which must not be split. NO PRESENCE TEST: `CostBadge` returns `null` itself (the
       table's `:not(:empty)` Cost column relies on that), and re-spelling `hasCost` here would get its three
       absent cases wrong. No state gate either: a cost measures what happened (`ViewRow.cost`). The cell
       names itself, so `ariaLabelFor` omits it. */
    {
      key: 'cost',
      testId: 'col-cost',
      width: COST_SLOT,
      gate: 'boardCost',
      // Money right-aligns so last digits share a column (hence mono).
      align: 'right',
      node: <CostBadge task={row.cost} sx={COST_SX} testId="feed-cost" />,
    },
    {
      key: 'author',
      width: AUTHOR_SLOT,
      // Off ≤767px, phone included: the title needs the width; the row's name says who (`ui/feed/geometry.ts`).
      gate: 'boardAuthor',
      align: 'center',
      mono: true,
      fontSize: '0.625rem',
      node: row.createdBy ? <AuthorBadge name={row.createdBy} sx={AUTHOR_SX} /> : undefined,
    },
    /* WHICH TENANT OWNS THE ROW — placement and derivation in `ORG_SLOT_FEED`. Composed by
       `ui/OrgIndicator.tsx`'s `orgSlot`; reads `row.org` (the shared derivation), never `row.task.org`. */
    /* WITHHELD FROM EVERY NON-OPERATOR: `orgSlots` composes nothing for a non-operator, so a tenant
       gets the width back at every band. */
    ...orgSlots(ctx.orgVisible, row.org, { width: ORG_SLOT_FEED, gate: 'boardOrg' }),
    {
      key: 'age',
      width: AGE_SLOT,
      /* Off a split at its floor (`boardAge`), but never while a pick is in progress: the tick rides this slot. There
         the ⋯ menu's Select is the way in, as on a hover-less device. */
      gate: ctx.select?.picking || ctx.select?.checked ? undefined : 'boardAge',
      align: 'right',
      mono: true,
      dim: true,
      // A run-aged row says what its figure counts (`ViewRow.ageTitle`).
      title: row.ageTitle ?? undefined,
      // The tick's tap-floor pullback (`FeedSlot.compact`): the row's ⋯ holds the same verbs at full size.
      compact: Boolean(ctx.select),
      node: ctx.select ? (
        <FeedPickTick
          store={ctx.select.store}
          taskId={row.id}
          picking={ctx.select.picking}
          title={row.title}
          age={row.age}
          onToggle={ctx.select.onToggle}
        />
      ) : row.age,
    },
  ]

  /* WHY A HELD OR WAITING ROW IS NOT RUNNING, on hover only: the whole sentence, list after
     the count included. A Queued row's is already the disc's hover sentence. */
  const isHold = row.stateLabel === TASK_STATUS.held.label || row.stateLabel === TASK_STATUS.waiting.label
  const hold = isHold ? waitSentence(row) : null
  const notes: FeedNote[] = hold !== null ? [{ key: 'hold', text: hold, tone: 'muted' }] : []
  const priority = queuedPriority(row)

  return {
    key: String(row.id),
    id: ctx.elementId,
    depth,
    indicator: {
      tone: row.tone,
      glyph: row.glyph,
      label: nodeName,
      title: nodeTitle,
      onPress: indicator.onPress,
      pressLabel: nodeName,
      pressGlyph: indicatorPress(indicator.kind)?.glyph,
      data: { press: indicator.kind },
      live: row.live,
      ring: row.epic && row.pct !== null ? { pct: row.pct } : null,
    },
    disclosure:
      ctx.expanded === undefined
        ? 'spacer'
        : { expanded: ctx.expanded, busy: ctx.fetching, onToggle: () => ctx.onToggle?.(), label: 'subtasks' },
    title: parseFeatureIcon(ctx.featureIcon) ? (
      <>
        <FeatureIcon icon={ctx.featureIcon} inline />
        {row.title}
      </>
    ) : row.title,
    titleText: row.title,
    /* A SPLIT AT ITS FLOOR KEEPS A READABLE NAME: below the rating's gate (`BOARD_NO_RATING`, ≤680px of
       board — a 1366px window with an editor open) the trailing cluster still costs ~250px, and the one-line name
       fell to a few words. There it wraps to two lines instead. Above the phone only: a phone row stays one line
       (its own band already gives the name every slot it can). */
    titleWrapAt: [ABOVE_PHONE, BOARD_NO_RATING],
    ariaLabel: ariaLabelFor(row),
    href: row.href,
    // 34px: a five-digit id at the lead's 0.6875rem mono (~33px), so 10000+ never ellipsises; right-aligned,
    // so ids share a right edge. Gone on the narrowest board too (`boardId`).
    lead: { text: String(row.id), width: 34, gate: 'boardId' },
    markers: [
      ...(row.system
        ? [{
            key: 'system',
            glyph: '⚙',
            label: SYSTEM_ROW_TITLE,
          }]
        : []),
      ...(priority !== null ? [priorityMarker(priority)] : []),
      /* On `archived_at` only (see `ViewRow.archived`). */
      ...(row.archived ? [{ key: 'archived', glyph: '🗄', label: 'Archived' }] : []),
    ],
    slots,
    actions: items,
    busy,
    onRowClick: openConsole,
    rowClickLabel: 'Open the web console for',
    /* THE PHONE'S PICK: a hover-less device never reveals the tick, so holding the row picks it — the
       same toggle as the tick and the ⋯'s Select. */
    onLongPress: select ? select.onToggle : undefined,
    deepLinked: ctx.deepLinked,
    notes: notes.length > 0 ? notes : undefined,
    detail: ctx.detail,
    /* `deep-linked` marks the revealed row for the DOM (e2e reads it); the wash is `ROW_MARKED_SX`'s. */
    data: { level: depth, 'deep-linked': ctx.deepLinked ? 'true' : undefined },
    // A top-level row can be dropped onto a rail feature or project (`tasks/rail/TaskTree.tsx`), which files or
    // moves it; the API files and moves top-level rows only.
    dragOut: !leaf ? { type: TASK_DRAG_TYPE, data: String(row.id) } : undefined,
    children: ctx.children,
  }
}
