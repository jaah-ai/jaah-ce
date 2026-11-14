/* ONE TASK AS A FEED ROW — the Activity style's row, as facts (`ui/feed/FeedList.tsx` draws it). Shared by the
 * Activity feed (`./ActivityFeedView.tsx`) and a task's subtask list (`../SubtaskList.tsx`), so both draw
 * literally the same row. */
import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import Checkbox from '@mui/material/Checkbox'
import type { PipelineTask } from '../../../types'
import { useIsPicked, type PickStore } from '../../../lib/pickStore'
import { countOf } from '../../../lib/humanize'
import { disclosureGlyph } from '../../ExpandToggleGlyph'
import TaskPeople from '../TaskPeople'
import SuggestionActions from '../SuggestionActions'
import PinnedMark from '../PinnedMark'
import { startIntentFor } from '../controls'
import CostBadge from '../CostBadge'
import RatingBadge from '../RatingBadge'
import LaneRunFacts from '../LaneRunFacts'
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
import { FONT_SIZE } from '../../../theme'
import type { FeedMarker, FeedNote, FeedRowSpec, FeedSlot } from '../../../ui/feed/types'
import type { ViewRow } from './rows'
import FeatureIcon from '../FeatureIcon'
import { parseFeatureIcon } from '../../../lib/featureIcon'
import Tip from '../../../ui/Tip'

/* The task-shaped slots: `ui/feed` owns the cluster's shape; this board decides each width and the type
   properties a `FeedSlot` cannot express. */
/* The persona rides `boardPersona` (`BOARD_NO_PERSONA`, ≤860px of board) — FIRST to go. MEASURED at a
   1440px window with the board narrowed: the title is 397px just above the gate and 477px just below. */
const PERSONA_SLOT = 78

/* THE RATING SLOT — 36px. A fixed slot's cost comes out of the title.
   Content at this cluster's 11px (`RATING_SX`): `4.5` + star ≈ 3 x ~6.6 + 1.5 + ~9.9 ≈ 31px, +5px tolerance
   (`../RatingBadge` quotes ~37px at its 13px default).
   Rides `boardRating` (`BOARD_NO_RATING`, ≤680px of board), the LAST of the four to go — the operator's order
   is persona, tokens, cost, rating. MEASURED at a 1440px window with the board
   narrowed, before the tokens slot: the title is 371px just above the gate and 409px just below (the tokens
   slot is already gone there). */
const RATING_SLOT = 36

/* THE ID LEAD — 34px: a five-digit id at the lead's 0.6875rem mono (~33px), so 10000+ never ellipsises. */
export const ID_SLOT = 34

/* `RatingBadge` sets `fontSize: '0.8125rem'` before composing `sx`, so the 11px the 36px rests on must be
   passed. Mono, weight and colour come from the badge. The reserved blank is the SLOT's (the badge is null
   at count 0); reservation beats collapse because rated rows are RARE and would step the columns 42px. */
const RATING_SX = { fontSize: FONT_SIZE.xs } as const

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
const TOKENS_SX = { fontSize: FONT_SIZE.xs, display: 'inline-block', color: 'text.secondary' } as const

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
   the rare costless one; reserving keeps people/age/⋯ one straight line. The blank is the badge's own null. */
const COST_SX = { fontSize: FONT_SIZE.xs } as const

/* THE CREATOR → ASSIGNEE SLOT — 44px: `RZ➜JS` at the slot's 10px mono, two badges of ~13px, the heavy arrow
   with its padding, and their gaps measure ≈ 41px, +3px tolerance. Rides the author's old gate (`boardAuthor`: off ≤767px, phone included —
   the row's name says who). Drawn by `../TaskPeople.tsx`. */
const PEOPLE_SLOT = 44

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

/* THE BULK PICK'S TICK RIDES THE STATE DISC (`FeedRowSpec.pick`, which owns the reveal and the geometry): the
   age slot stays the age, and the tick costs no width. ONLY A DONE ROW'S DISC turns into the tick, while the disc is
   hovered or the tick holds keyboard focus, and while it is ticked; picking one row never reveals the others'. Any other
   pickable row keeps its disc (its own press, e.g. a draft's Start) and is picked from the ⋯ menu's Select; once
   ticked it shows the tick too, so the pick stays visible. A hover-less (touch) device picks a done row by a
   long-press. The 24px box (`SELECT_BOX_SX`) overhangs the 18px disc evenly. */
/* THE TICK READS ITS OWN ROW from the pick store: a click re-renders this tick, never the board, so
   rapid clicks never queue behind a full re-render. Its reveal follows the live answer too. */
function FeedPickTick({ store, taskId, title, onToggle }: {
  store: PickStore<PipelineTask>
  taskId: number
  title: string
  onToggle: () => void
}) {
  const checked = useIsPicked(store, taskId)
  return (
    <Checkbox
      size="small"
      className="feed-pick"
      data-testid="feed-task-select"
      data-revealed={checked ? 'true' : undefined}
      checked={checked}
      onChange={onToggle}
      slotProps={{ input: { 'aria-label': `Select ${title}` } }}
      sx={SELECT_BOX_SX}
    />
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
   word says all there is. The single home for the row's hover note and its accessible name, so the
   two never word it apart. Held has one cause (its epic's hold mode), which its bare
   word does not say. */
function waitSentence(row: ViewRow): string | null {
  const held = row.stateLabel === TASK_STATUS.held.label
  if (held && row.stateTitle === row.stateLabel) return `${row.stateLabel}. Epic on hold`
  const waiting = held || row.stateLabel === TASK_STATUS.waiting.label || row.stateLabel === TASK_STATUS.queued.label
  return waiting && row.stateTitle !== row.stateLabel ? row.stateTitle : null
}

/* The row's accessible name. Carries the state WORD (the glyph is only the sighted reader's indicator). ITS
   PRESENCE SILENCES THE DISC AND MARKERS (`FeedRowSpec.ariaLabel`). */
/** What follows the state word on a finished row its viewer has not opened (`ViewRow.unseen`). */
export const UNSEEN = ', not yet seen'

export function ariaLabelFor(row: ViewRow): string {
  const progress = row.epic ? `, ${row.doneChildCount} of ${row.childCount} subtasks done` : ''
  const author =
    row.origin.kind !== 'human' ? `, filed by ${row.origin.label}` : row.createdBy ? `, created by ${row.createdBy}` : ''
  const assignee = row.assignee ? `, assigned to ${row.assignee}` : ''
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
  const state = `${waitSentence(row) ?? row.stateLabel}${row.unseen ? UNSEEN : ''}`
  return `Task ${row.id}: ${row.title} — ${state}${priority}${progress}${author}${assignee}${rating}`
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
  /** The bulk pick's tick, riding the state disc (`FeedRowSpec.pick`): absent or `null` = no tick on this row (the board
   *  offers no selection, or nobody may pick the row). `checked` is the pick when the board last rendered — the ⋯ menu's word; the tick reads
   *  `store` live. Also the ⋯ menu's Select, the hover-less device's way in. */
  select?: { checked: boolean; store: PickStore<PipelineTask>; onToggle: () => void } | null
  /** The columns this viewer hid (`lib/taskColumns.ts` keys: slot keys plus `id`, the lead). Absent = none. */
  hiddenColumns?: ReadonlySet<string>
  /** On a release's list, how many of this epic's parts it ships (`TaskListViewProps.parts`). An unfinished epic reads
   *  "Part" for "Epic", with the count on hover. */
  shipsParts?: number
}

/** A release's unfinished epic: its persona slot's word and hover (`RowContext.shipsParts`); null on any other row. */
export function partOf(row: ViewRow, shipsParts: number | undefined): { label: string; title: string } | null {
  if (shipsParts === undefined || !row.epic || row.task.state === 'done') return null
  return { label: 'Part', title: `Ships ${countOf(shipsParts, 'part')} of an unfinished epic` }
}

/* ONE TASK ROW, AS FACTS — no pixels. */
export function buildFeedTaskRow(ctx: RowContext): FeedRowSpec {
  const { row, depth, handlers, busy, navigate } = ctx
  const leaf = ctx.leaf ?? depth > 0

  /* Verbs derived and listed by `../rowActions` (shared with the table). */
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
      ? () => handlers.onStart!(row.task, startIntentFor(row.task))
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
  const stateTitle = `${row.stateTitle}${row.unseen ? UNSEEN : ''}`
  const nodeTitle = indicatorHover(
    rollupClause === null ? stateTitle : `${stateTitle}. ${rollupClause}`,
    indicator.kind,
    flags.consoleSlot,
  )
  const nodeName = indicator.verb
    ? indicatorName(row.stateLabel, indicator.verb, row.title)
    : `${row.stateLabel}${row.unseen ? UNSEEN : ''}`

  const select = ctx.select
  const wait = waitSentence(row)
  const overflowItems: OverflowItem[] = [
    ...taskOverflowItems(row.task, row.title, handlers, flags),
    // The tick's second home (a hover-less device never reveals it): see `FeedRowSpec.pick`.
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
  /* THE WAIT REASON IS NOT A ⋯ ITEM: a disabled line heading the menu was a reason nobody could act on there. It is
     the row's own: its name, and its disc's hover — and its TAP (`tapTitle`), the phone's way to it — with the fix,
     where the viewer can make one, on the task page (`TaskDetailHeader`, from `TaskOut.wait_fix_url`). */

  /* THE TRAILING CLUSTER — persona, rating, tokens, cost, people (767, phone included), ORG (`wide`), age ungated (see each
     `*_SLOT`). On a board narrower still (an editor and the console beside it) the org, then the people cell, follow
     the board too (`BOARD_NO_ORG` / `BOARD_NO_AUTHOR`).
     Persona, tokens, cost and rating follow the BOARD's width and drop in the operator's order — persona, then
     tokens, then cost, then rating (`ui/listStyles.ts`'s `BOARD_NO_*`) — so the
     rating can outlive the cost to its right; each column still lines up because every row drops the same
     slots. The org cannot join the order; left of the ungated tail is `SlotGate`'s position for it. The rating
     branch tests the data only: `ratingCount` is already gated to done-and-ratable rows in `./rows`. The tokens
     branch is `tokensLabel`'s verdict, nothing re-spelled. The cost has no branch — see its slot. */
  const tokens = tokensLabel(row.tokens)
  const part = partOf(row, ctx.shipsParts)
  const slots: FeedSlot[] = [
    {
      key: 'persona', testId: 'col-persona', width: PERSONA_SLOT, gate: 'boardPersona', align: 'right',
      title: part?.title ?? row.persona, node: part?.label ?? row.persona,
    },
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
          <Tip title={tokensTitle(row.tokens)}><Box
            component="span"
            data-testid="feed-tokens"
            sx={theme => ({ ...monoSx(theme), ...TOKENS_SX })}
          >
            {tokens}
            <Box component="span" sx={SR_ONLY_SX}> tokens</Box>
          </Box></Tip>
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
      key: 'people',
      testId: 'col-people',
      width: PEOPLE_SLOT,
      // Off ≤767px, phone included: the title needs the width; the row's name says who (`ui/feed/geometry.ts`).
      gate: 'boardAuthor',
      align: 'center',
      mono: true,
      fontSize: FONT_SIZE.xxs,
      node: (
        <TaskPeople
          creator={row.createdBy}
          origin={row.origin}
          assignee={row.assignee}
          assigneeId={row.assigneeId}
          projectId={row.task.project_id ?? null}
          taskTitle={row.title}
          onAssign={
            handlers.onAssign !== undefined && (handlers.canEditRow?.(row.task) ?? true)
              ? id => handlers.onAssign!(row.task, id)
              : undefined
          }
        />
      ),
    },
    /* WHICH TENANT OWNS THE ROW — placement and derivation in `ORG_SLOT_FEED`. Composed by
       `ui/OrgIndicator.tsx`'s `orgSlot`; reads `row.org` (the shared derivation), never `row.task.org`. */
    /* WITHHELD FROM EVERY NON-OPERATOR: `orgSlots` composes nothing for a non-operator, so a tenant
       gets the width back at every band. */
    ...orgSlots(ctx.orgVisible, row.org, { width: ORG_SLOT_FEED, gate: 'boardOrg' }),
    {
      key: 'age',
      width: AGE_SLOT,
      gate: 'boardAge',
      align: 'right',
      mono: true,
      dim: true,
      // The exact moment the figure counts from (`ViewRow.ageTitle`), on every state.
      title: row.ageTitle ?? undefined,
      node: row.age,
    },
  ]

  /* THE VIEWER'S HIDDEN COLUMNS (Filter & view > Columns). */
  const hidden = ctx.hiddenColumns
  const shownSlots = hidden && hidden.size > 0 ? slots.filter(slot => !hidden.has(slot.key)) : slots

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
      tapTitle: wait !== null && !indicator.onPress,
      onPress: indicator.onPress,
      pressLabel: nodeName,
      pressGlyph: indicatorPress(indicator.kind)?.glyph,
      data: { press: indicator.kind },
      live: row.live,
      ring: row.epic && row.pct !== null ? { pct: row.pct } : null,
      attention: row.unseen,
    },
    // Only a done row's disc yields to the tick, or a row already ticked (from the ⋯): see `FeedPickTick`.
    pick: select && (row.done || select.checked) ? (
      <FeedPickTick
        store={select.store}
        taskId={row.id}
        title={row.title}
        onToggle={select.onToggle}
      />
    ) : undefined,
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
    /* A JAAH SUGGESTION'S ACCEPT AND DISCARD, beside its name at every width (`../SuggestionActions`). Only a root row:
       a suggestion is a top-level filing. */
    titleAside: !leaf ? (
      <>
        {/* A PINNED ROW SAYS SO, and the mark is its one-click Unpin (`lib/taskPin.ts`); Pin itself is in the ⋯. */}
        {flags.pinned && <PinnedMark title={row.title} onUnpin={() => handlers.onTogglePin!(row.task)} />}
        <SuggestionActions task={row.task} title={row.title} handlers={handlers} busy={busy} dense />
        {/* A LANE RUN'S GATES AND WALL TIME, and its Slow mark, without opening it (`../LaneRunFacts`). */}
        <LaneRunFacts run={row.task.lane_run} />
      </>
    ) : undefined,
    /* A SPLIT AT ITS FLOOR KEEPS A READABLE NAME: below the rating's gate (`BOARD_NO_RATING`, ≤680px of
       board — a 1366px window with an editor open) the trailing cluster still costs ~250px, and the one-line name
       fell to a few words. There it wraps to two lines instead. Above the phone only: a phone row stays one line
       (its own band already gives the name every slot it can). */
    titleWrapAt: [ABOVE_PHONE, BOARD_NO_RATING],
    // A release's part rides the name too: the persona slot is gated off on a phone.
    ariaLabel: part ? `${ariaLabelFor(row)}, ${part.title.toLowerCase()}` : ariaLabelFor(row),
    href: row.href,
    // 34px: a five-digit id at the lead's 0.6875rem mono (~33px), so 10000+ never ellipsises; right-aligned,
    // so ids share a right edge. Gone on the narrowest board too (`boardId`). Every row's id, an epic's included,
    // copies the task's absolute URL on click, at any status: a task is cited elsewhere by its link.
    lead: hidden?.has('id') ? undefined : {
      text: String(row.id),
      width: ID_SLOT,
      gate: 'boardId',
      copy: { what: `link to task ${row.id}`, value: () => `${window.location.origin}${row.href}` },
    },
    /* WHO FILED IT, at every width (`lib/taskOrigin.ts`): a machine's row recedes; the people cell's glyph names it. */
    quiet: row.origin.kind !== 'human',
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
    slots: shownSlots,
    actions: verbs,
    busy,
    onRowClick: openConsole,
    rowClickLabel: 'Open the web console for',
    /* THE PHONE'S PICK: a hover-less device never reveals the tick, so holding a done row picks it — the
       same toggle as the tick and the ⋯'s Select. Other rows pick from the ⋯ only, like their tick. */
    onLongPress: select && row.done ? select.onToggle : undefined,
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
