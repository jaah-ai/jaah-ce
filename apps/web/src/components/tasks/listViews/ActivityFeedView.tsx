/* The "Activity feed" — the `/tasks` list read as a stream of agent activity, organised by WHEN a task last
 * moved, so "what is my fleet doing now" is the top of the page. The board's one list.
 * AN ADAPTER that owns no pixels (`ui/feed/FeedList.tsx` draws, `ui/feed/geometry.ts` holds the numbers):
 * this file decides which rows exist; how a state becomes an indicator, which verbs a row offers, and what
 * its accessible name says is `./feedRow.tsx`.
 * A WORKING BOARD:
 *   · EVERY FUNCTION WORKS, none reimplemented: verbs from `../rowActions`, create writes from
 *     `../useInlineTaskCreate`, expand state from the host.
 *   · THE TASK ID LEADS THE TITLE at desktop only (`FeedRowSpec.lead`, gated at `FEED_NO_ID`).
 *   · NO STATE WORD: the rail node is the state's own GLYPH (○ draft, » queued, ● running, ? asking,
 *     ✕ failed, ■ paused, ✓ done) on a tone-tinted disc, so colour is not the only signal.
 *   · RECENCY IS A REAL GROUP-BY OPTION and the default; `sectionsFor` is the one layout policy here.
 *   · Fixed slots ride DERIVED title-floor gates (`BOARD_NO_*`/`FEED_NO_ID` in `ui/listStyles.ts`), never
 *     `PHONE` alone (which left a 0px title at 481-640px). This file picks each slot's gate and order only.
 *     The epic DISCLOSURE is inline at every width (the ⋯ item stays as its second home), and the ⚙ system /
 *     🗄 archived markers survive (no gate, no width).
 * THE NODE PRESSES, AND SO DOES THE ROW (operator decisions):
 *   · the node offers exactly what the task page's pill offers — `taskIndicatorAction` in `../shared`;
 *   · the disc stays (no chip bringing the word back);
 *   · the ROW BODY CARRIES THE CONSOLE ONLY — never Start (a stray click must not launch a runner);
 *   · a pressable node KEEPS ITS STATE GLYPH (no ▶ swap: drafts would lose "draft");
 *   · the node is under the tap floor (18px / 15px) — on a console row the whole ROW is the same target.
 * MOBILE (<480px) is a tighter set of the same numbers: the whole trade is the PHONE BAND block in
 * `ui/feed/geometry.ts`; the create row below prices its own phone metrics. Bands above 480px stand.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ActAsToCreate from '../ActAsToCreate'
import ProjectMenu from '../ProjectMenu'
import ButtonBase from '@mui/material/ButtonBase'
import { OrgQualifier, orgQualifierFor, spokenProjectName } from '../../../lib/projectLabel'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import InputBase from '@mui/material/InputBase'
import Typography from '@mui/material/Typography'
import { useNavigate } from 'react-router-dom'
import { useOfficialDayStart, useOfficialZone } from '../../../lib/officialZone'
import { taskRowElementId } from '../../../lib/taskDeepLink'
import {
  GROUP_BY_STATE,
  isFeatureGroupBy,
  isProjectGroupBy,
  isRecencyGroupBy,
  isTagGroupBy,
} from '../../../lib/taskGrouping'
import { PHONE } from '../../../ui/listStyles'
import { useOrgVisible } from '../../../ui/OrgIndicator'
import FeedList from '../../../ui/feed/FeedList'
import { RAIL_X, RAIL_X_PHONE, ROW_PAD_L, ROW_PAD_L_PHONE, rowPadLeft } from '../../../ui/feed/geometry'
import type { FeedRowSpec, FeedSection } from '../../../ui/feed/types'
import { CONTROLS } from '../controls'
import { epicChildGroups, isFocusRow, orgWaitPillLabel, prepareChildTitle } from '../shared'
import { sectionTotal, splitBoardSections } from '../taskTree'
import ChatGlyph from '../ChatGlyph'
import InlineTaskPrefixChip, { INLINE_PREFIX_HINT_ID } from '../InlineTaskPrefixChip'
import { EXPAND_GESTURE_OFF, EXPAND_GESTURE_SX, useInlineExpandGesture } from '../useInlineExpandGesture'
import { EXPAND_LABEL, EXPAND_TITLE, useInlineTaskCreate } from '../useInlineTaskCreate'
import { buildFeedTaskRow } from './feedRow'
import { bucketByRecency, toViewRows } from './rows'
import type { ViewRow } from './rows'
import type { TaskListCreate, TaskListGrouping, TaskListStateGrouping, TaskListViewProps } from './types'
import type { TaskRowActionHandlers } from '../rowActions'
import { boardSectionNote, boardSectionOf, boardSectionTotal, featureBuckets, SECTION_LABEL, SECTION_ORDER, sortBySection } from './boardSections'
import type { TaskTree } from '../../../types'
import { featureIconFor } from '../../../lib/featureIcon'
import { MaximizeIcon } from '../../../ui/icons'

/* The inline create row, handed to `FeedList` as `leading`. Its WRITES live in `useInlineTaskCreate`. Not a
   feed row, so it prices its own metrics off `ui/feed/geometry.ts`.
   Not mounted without `tasks:create` — the host withholds the `create` bundle (`TaskListViewProps`). */
const CREATE_ROW_SX = {
  display: 'flex',
  alignItems: 'center',
  gap: 1,
  minHeight: 34,
  pl: `${RAIL_X + ROW_PAD_L}px`,
  pr: 0.5,
  mb: 0.5,
  borderBottom: '1px solid',
  borderColor: 'divider',
  // Not down to 24px: a real text input with a 16px iOS-zoom floor (`CREATE_INPUT_SX`).
  /* `flex-end`: when a long target chip wraps onto its own line (`CREATE_FORM_SX`), the buttons sit beside the input
     they act on, not between the two lines. Bottom-aligning only CENTRES them on that line when every item is the
     line's height, so the phone spells one: 28px for the input (`CREATE_INPUT_SX`), the caption
     (`CREATE_INVOLVEMENT_SX`) and the buttons. The input was 23px (16px × MUI's 1.4375em) and the caption 11px, so
     the caption's text sat ~6px under the input's and "Unattended ▶ ⤢" read as a line below it. */
  [PHONE]: { minHeight: 30, pl: `${RAIL_X_PHONE + ROW_PAD_L_PHONE}px`, gap: 0.5, mb: 0.25, alignItems: 'flex-end' },
} as const

/* The form's own flex line. On the phone it WRAPS ONLY WHEN IT MUST: the chip stays inline beside the input while
   both fit, and a name too long for that takes a line of its own, drawn whole — never a whole row for
   a short one like "acme▾". The input's phone `flexBasis` (`CREATE_INPUT_SX`) decides which. */
const CREATE_FORM_SX = {
  flex: '1 1 auto',
  minWidth: 0,
  display: 'flex',
  alignItems: 'center',
  gap: 0.5,
  [PHONE]: { flexWrap: 'wrap', rowGap: 0.5 },
} as const

const CREATE_INPUT_SX = {
  flex: '1 1 auto',
  minWidth: 0,
  fontSize: '0.8125rem',
  /* The input line's height on the phone, the buttons' 28px: the root is inline-flex `center`, so the text centres in it.
     `flexBasis`: the narrowest the input goes beside the chip ("New task…" whole); below it the form wraps. */
  [PHONE]: { minHeight: 28, flexBasis: '7rem' },
  '& .MuiInputBase-input': {
    p: 0,
  // The placeholder is the only label, so not MUI's faint ghost.
    '&::placeholder': { opacity: 0.75 },
    // Under 16px, Safari zooms the whole page on focus.
    [PHONE]: { fontSize: '16px' },
  },
} as const

const CREATE_BUTTON_SX = {
  flex: '0 0 auto',
  width: 28,
  minWidth: 28,
  height: 26,
  p: 0,
  fontSize: '0.875rem',
  lineHeight: 1,
  borderRadius: 1,
  /* 28, not 24: a create button has NO second home, and the 30px row can carry it. */
  [PHONE]: { width: 28, minWidth: 28, height: 28, minHeight: 0 },
} as const

/* The involvement caption beside Start: one short word, never wrapping, never squeezing the buttons. */
const CREATE_INVOLVEMENT_SX = {
  flex: '0 0 auto',
  whiteSpace: 'nowrap',
  color: 'text.secondary',
  fontSize: '0.6875rem',
  lineHeight: 1,
  // A 28px box with its text centred, so the row's `flex-end` centres it on the input line (`CREATE_ROW_SX`).
  [PHONE]: { display: 'inline-flex', alignItems: 'center', height: 28 },
} as const

const INLINE_INVOLVEMENT_ID = 'inline-new-task-involvement'

/* THE TARGET CHIP: where ▶ files, named before the press, and the way to change it. Muted like the
   involvement caption, so the words stay the row's subject; a hairline border marks it pressable. On the phone
   a name too long to sit beside the input wraps onto a line of its own (`CREATE_FORM_SX`); only a name wider than
   the row ellipsises there (the full name is the tooltip and the accessible name). */
const CREATE_TARGET_SX = {
  // Never squeezed below its name by the input; only a name wider than `maxWidth` ellipsises.
  flex: '0 0 auto',
  minWidth: 0,
  maxWidth: '40%',
  display: 'inline-flex',
  alignItems: 'center',
  gap: 0.25,
  px: 0.75,
  height: 22,
  borderRadius: 1,
  border: '1px solid',
  borderColor: 'divider',
  color: 'text.secondary',
  fontSize: '0.75rem',
  lineHeight: 1,
  whiteSpace: 'nowrap',
  '&:hover': { color: 'text.primary', borderColor: 'text.secondary' },
  '&.Mui-disabled': { opacity: 0.6 },
  // A long name wraps to its own line on the phone (`CREATE_FORM_SX`): the whole width is its cap there.
  [PHONE]: { height: 28, fontSize: '0.75rem', maxWidth: '100%' },
} as const

/* The create form's caption (its error, the filing scope): a form's feedback, not a row's prose. Spells the depth-0 inset. The `[PHONE]` key is
   load-bearing, matching `CREATE_ROW_SX`'s phone row. */
const CREATE_CAPTION_SX = {
  pl: `${RAIL_X + ROW_PAD_L}px`,
  pb: 0.5,
  whiteSpace: 'normal',
  [PHONE]: { pl: `${RAIL_X_PHONE + ROW_PAD_L_PHONE}px` },
} as const

export function FeedNewTaskRow({ create }: { create: TaskListCreate }) {
  const {
    text, setText, submitting, error, refusedOrg, noProject, offered, canAct, intent, startTyped,
    targetProject, pickerProjects, pickProject, armHeldCreate,
    expandTyped, chatOffered, chatTyped, chatLabel, chatTitle,
    prefix, launchIntent, clearPrefix, onInputKeyDown,
    placeholder, inputLabel, clearHint, startLabel, startTitle, involvement, pushIdentityDialog,
  } = useInlineTaskCreate(create)
  /* THE DOUBLE-PRESS THAT OPENS THE FULL EDITOR — wired a second time here (the two create rows share a hook,
     not a component). Its rules are single-homed on `../useInlineExpandGesture`. It fires the hook's
     `expandTyped` (which discards the draft), never `create.onExpand` directly. */
  const expandGesture = useInlineExpandGesture(expandTyped, submitting)
  const [pickerAnchor, setPickerAnchor] = useState<HTMLElement | null>(null)
  // Nothing creatable: no row (`offered`, on the hook).
  if (!offered) return null
  const qualifierOf = orgQualifierFor(pickerProjects)
  const targetOrg = targetProject ? qualifierOf(targetProject) : null
  const targetName = targetProject ? spokenProjectName(targetProject.name, targetOrg) : ''
  return (
    <>
      <Box
        data-testid="feed-row-new"
        /* Row metrics plus the phone's double-tap-zoom opt-out (`EXPAND_GESTURE_SX`). */
        sx={{ ...CREATE_ROW_SX, ...EXPAND_GESTURE_SX }}
        onMouseDown={expandGesture.onRowMouseDown}
        onDoubleClick={expandGesture.onRowDoubleClick}
        onTouchEnd={expandGesture.onRowTouchEnd}
      >
        {/* A real `<form>`: Enter submits by platform rule, and AT announces a field in a form. */}
        <Box
          component="form"
          onSubmit={e => void startTyped(e)}
          aria-label="Create a task inline"
          sx={CREATE_FORM_SX}
        >
          {targetProject && (
            <ButtonBase
              data-testid="inline-new-task-project"
              aria-label={`Files into ${targetName}. Change project`}
              aria-haspopup="menu"
              title={`Files into ${targetName}`}
              disabled={submitting}
              onClick={e => setPickerAnchor(e.currentTarget)}
              /* Opted out of the row's double-press: picking is not opening the editor. */
              {...EXPAND_GESTURE_OFF}
              sx={CREATE_TARGET_SX}
            >
              <Box component="span" sx={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {targetProject.name}
              </Box>
              <OrgQualifier org={targetOrg} />
              <Box component="span" aria-hidden="true" sx={{ flex: '0 0 auto' }}>▾</Box>
            </ButtonBase>
          )}
          {/* THE TYPED-PREFIX CHIP (`../InlineTaskPrefixChip`). */}
          {prefix !== null && (
            <InlineTaskPrefixChip
              prefix={prefix}
              onClear={clearPrefix}
              disabled={submitting}
              sx={{ flex: '0 0 auto', height: 20, fontSize: '0.6875rem' }}
            />
          )}
          <InputBase
            value={text}
            onChange={e => setText(e.target.value)}
            onKeyDown={onInputKeyDown}
            disabled={noProject || submitting}
            placeholder={placeholder}
            /* Name and hint from the hook. */
            inputProps={{
              'aria-label': inputLabel,
              'aria-describedby': clearHint === undefined ? undefined : INLINE_PREFIX_HINT_ID,
              'data-testid': 'inline-new-task-input',
            }}
            sx={CREATE_INPUT_SX}
          />
        </Box>
        {/* The mode Start will create in (org default `task_default_autonomous`), off the hook's payload. A chat
            chip already says Chat. Described-by, so AT hears it with Start. */}
        {involvement.value !== 'chat' && (
          <Typography
            id={INLINE_INVOLVEMENT_ID}
            variant="caption"
            data-testid="inline-new-task-involvement"
            sx={CREATE_INVOLVEMENT_SX}
          >
            {involvement.label}
          </Typography>
        )}
        {/* Names avoid "new task" (the toolbar's button). */}
        <IconButton
          color="primary"
          data-testid="inline-new-task-start"
          /* Name, tooltip and spinner follow the armed launch. */
          aria-label={startLabel}
          aria-describedby={involvement.value !== 'chat' ? INLINE_INVOLVEMENT_ID : undefined}
          title={startTitle}
          disabled={!canAct}
          onClick={() => void startTyped()}
          sx={CREATE_BUTTON_SX}
        >
          <Box component="span" aria-hidden="true">
            {intent === launchIntent ? '…' : CONTROLS.start.icon}
          </Box>
        </IconButton>
        {/* THE CHAT LAUNCH. */}
        {chatOffered && (
          <IconButton
            color="primary"
            data-testid="inline-new-task-chat"
            aria-label={chatLabel}
            title={chatTitle}
            disabled={submitting}
            onClick={() => void chatTyped()}
            sx={CREATE_BUTTON_SX}
          >
            {intent === 'chat' ? <Box component="span" aria-hidden="true">…</Box> : <ChatGlyph />}
          </IconButton>
        )}
        {/* Enabled with an EMPTY box: the full editor is what a multi-line writer reaches for first. */}
        <IconButton
          data-testid="inline-new-task-expand"
          aria-label={EXPAND_LABEL}
          title={EXPAND_TITLE}
          disabled={submitting}
          onClick={expandTyped}
          sx={CREATE_BUTTON_SX}
        >
          <MaximizeIcon />
        </IconButton>
      </Box>
      {/* Only projects this viewer may create in (`pickerProjects`): a refused row would be noise here. */}
      <ProjectMenu
        anchorEl={pickerAnchor}
        onClose={() => setPickerAnchor(null)}
        projects={pickerProjects}
        projectId={targetProject?.id ?? null}
        onPick={id => { if (id !== null) pickProject(id) }}
        testId="inline-new-task-project-menu"
        ariaLabel="File into project"
      />
      {/* A failed create never fails silently: the form says why, where the typing was. */}
      {error && (
        <Typography
          variant="caption"
          component="div"
          data-testid="inline-new-task-error"
          role="alert"
          /* Opted out: reading the error must not navigate away from it. */
          {...EXPAND_GESTURE_OFF}
          sx={{ ...CREATE_CAPTION_SX, color: 'error.main' }}
        >
          {error}
          <ActAsToCreate org={refusedOrg} onActAs={armHeldCreate} />
        </Typography>
      )}
      {/* Outside the row's Box: a portal's events still bubble through the React tree to its expand gesture. */}
      {pushIdentityDialog}
    </>
  )
}


/** `tagId` only for a FOLDABLE section (tag or project), `onToggle` for State's Done; a recency or feature
 *  bucket does not fold. */
export type ViewSection = {
  key: string
  label: string
  rows: ViewRow[]
  /** The server's total for a tag or state section, `null` until landed; absent for a bucket (exact by
   *  construction). */
  total?: number | null
  /** The total's make-up (`boardSectionNote`): State's Stopped only. */
  countNote?: string
  tagId?: string
  collapsed?: boolean
  onToggle?: () => void
}

/* THE STATE AXIS — the board's default Group by. Membership is READ (`boardSectionOf` = `task.section`, a
   person-waiting Stopped row lifted to Waiting), order, labels and the split totals are
   `./boardSections`', as is the rank of rows inside a section. A section with
   no loaded row and no server total is omitted; one whose rows lie beyond the loaded window stays, its header
   carrying the total. Done folds on `task_done_expanded` — force-opened while it holds the deep-linked row. */
function stateSections(rows: ViewRow[], state: TaskListStateGrouping | undefined, deepLinkedId: number | null): ViewSection[] {
  const byId = new Map(rows.map(row => [row.id, row]))
  const ordered = sortBySection(rows.map(row => row.task)).map(task => byId.get(task.id)!)
  return SECTION_ORDER.flatMap(section => {
    const members = ordered.filter(row => boardSectionOf(row.task) === section)
    const total = state?.counts ? boardSectionTotal(state.counts, section) : null
    if (members.length === 0 && (total ?? 0) === 0) return []
    const onToggle = section === 'done' ? state?.onToggleDone : undefined
    const held = section === 'queued' ? queuedHold(members, total) : null
    return [{
      key: `state:${section}`,
      label: held?.label ?? SECTION_LABEL[section],
      rows: members,
      total,
      countNote: held?.note ?? (state?.counts ? boardSectionNote(state.counts, section) : undefined),
      collapsed:
        onToggle !== undefined &&
        state?.doneExpanded !== true &&
        !(deepLinkedId !== null && members.some(member => member.id === deepLinkedId)),
      onToggle,
    }]
  })
}

/* Queued's header where its org holds rows back (`wait_reason`, e.g. no node yet), so it matches what those rows say:
   every row held for one reason, and all of the section loaded → that reason's pill ("Waiting for your
   node"); otherwise "Queued" with "N waiting" beside the total ("Waiting" alone is the person-waiting section's name).
   The server counts no hold, so the note counts the loaded rows. */
function queuedHold(members: ViewRow[], total: number | null): { label?: string; note?: string } | null {
  const reasons = members.flatMap(row => (row.task.state === 'ready' && row.task.wait_reason ? [row.task.wait_reason] : []))
  if (reasons.length === 0) return null
  const pills = new Set(reasons.map(orgWaitPillLabel))
  if (pills.size === 1 && reasons.length === members.length && (total || members.length) === members.length) {
    return { label: [...pills][0] }
  }
  return { note: `${reasons.length} waiting` }
}

/* THE FEATURE AXIS — `featureBuckets`: the tree's feature order, then each project's no-feature tasks. */
function featureSections(rows: ViewRow[], tree: TaskTree | undefined): ViewSection[] {
  const byId = new Map(rows.map(row => [row.id, row]))
  return featureBuckets(tree, rows.map(row => row.task)).map(bucket => ({
    key: `feature:${bucket.id}`,
    label: bucket.label,
    rows: bucket.tasks.map(task => byId.get(task.id)!),
  }))
}

/* WHICH SECTIONS THIS VIEW DRAWS, from `groupBy`. State and Feature read the loaded rows (above). A SECTIONED axis (tag group or project) only when the grouping
   payload is present (no payload, no section order — as `pages/Tasks.tsx`); everything else is ONE UNLABELLED
   SECTION, identical to the flat list. Both sectioned axes share one arm via `splitBoardSections`/`sectionTotal`
   (`../taskTree`). */
export function sectionsFor(
  rows: ViewRow[],
  groupBy: string | undefined,
  grouping: TaskListGrouping | undefined,
  deepLinkedId: number | null,
  /** Buckets cut on the control plane's calendar midnight; `bucketByRecency` is pure, so the zone is passed. */
  zone: string,
  /** Recency only: pins the `Queued` bucket last (`bucketByRecency` says why). Tag/project sections do not —
   *  lifting queued rows out would misfile them, and they keep the server's queued-last order anyway. */
  focus: boolean,
  state: TaskListStateGrouping | undefined,
  tree: TaskTree | undefined,
): ViewSection[] {
  if (groupBy === GROUP_BY_STATE) return stateSections(rows, state, deepLinkedId)
  if (isFeatureGroupBy(groupBy ?? '')) return featureSections(rows, tree)
  if (isRecencyGroupBy(groupBy ?? '')) {
    return bucketByRecency(rows, zone, undefined, focus).map(bucket => ({
      key: bucket.label,
      label: bucket.label,
      rows: bucket.rows,
    }))
  }
  if (
    groupBy !== undefined &&
    (isTagGroupBy(groupBy) || isProjectGroupBy(groupBy)) &&
    grouping !== undefined
  ) {
    /* `splitBoardSections` (`../taskTree`) is the SHARED membership rule with the table. A tag section renders
       EMPTY too ("zero rows" is a claim the loaded window cannot back), hence `FeedList` counts SECTIONS. Empty
       PROJECT sections are dropped per `TaskTreeGrouping`. */
    return splitBoardSections(grouping, rows, viewRow => viewRow.task).map(section => ({
      key: `tag:${section.tagId}`,
      label: section.label,
      rows: section.members,
      // `sectionTotal` (`../taskTree`) is the one home: same number as the table's header.
      total: sectionTotal(grouping, section.tagId),
      tagId: section.tagId,
      // A section holding the DEEP-LINKED row is FORCE-OPENED (as `../taskTree` does): collapsed, the target is
      // absent from the DOM and the link dead-ends.
      collapsed:
        grouping.isCollapsed(section.tagId) &&
        !(deepLinkedId !== null && section.members.some(member => member.id === deepLinkedId)),
    }))
  }
  return rows.length === 0 ? [] : [{ key: 'all', label: '', rows }]
}

/* ── A POLL MUST NOT STALL INPUT ─────────────────────────────────────────────────────────────────
   A poll or an SSE push re-rendered this view every 0.5-3s, and with it every row (75 on a busy board), so a
   keystroke in the create row queued behind a full redraw. Two halves make an unchanged row cost nothing:
     · `ui/feed/FeedList.tsx` memoizes each row on its spec's IDENTITY;
     · this view hands back the SAME spec for a row whose inputs did not change — cached per section and task,
       keyed by `sameInputs` over everything the builder reads (the task object, which the query keeps by
       identity while unchanged, the age figure that ticks without it, and the row's context flags).
   A HANDLER THE HOST REBUILDS EACH RENDER MUST NOT DEFEAT IT: the verbs (`on*`), `navigate`, the expand toggle
   and the pick's toggle are called through a LATEST-VALUE REF, so a built spec stays valid and keeps its
   identity. What the builder ASKS at build time (`canEditRow`, `consoleWorktreeFor`, … — they decide which
   verbs exist) stays an input by identity: a changed answer must rebuild the row, never be read stale. */
function sameInputs(a: readonly unknown[], b: readonly unknown[]): boolean {
  return a.length === b.length && a.every((value, index) => Object.is(value, b[index]))
}

/* A verb is a handler the builder only CALLS (`on*`); every other member is read while building. */
const isVerbKey = (key: string, value: unknown) => key.startsWith('on') && typeof value === 'function'

export default function ActivityFeedView({
  tasks,
  loading,
  deepLinkedId,
  groupBy,
  grouping,
  actions,
  submitting,
  expanded,
  fetching,
  focus,
  onToggleTask,
  onToggleSection,
  stateGrouping,
  tree,
  create,
  filtered,
  selection,
}: TaskListViewProps) {
  const navigate = useNavigate()
  /* Read once and threaded onto every `RowContext` (`buildFeedTaskRow` cannot call hooks);
     `ui/OrgIndicator.tsx::useOrgVisible` is the single home for the verdict. */
  const orgVisible = useOrgVisible()
  const rows = useMemo(() => toViewRows(tasks), [tasks])
  // The CP's configured zone, never the browser's, so operators in two zones read the same headings
  // (`lib/officialZone.ts`).
  const zone = useOfficialZone()
  /* A memo DEPENDENCY only: it invalidates the buckets when the zone's midnight passes on an idle board.
     `lib/officialZone.ts` owns the timer. */
  const dayStart = useOfficialDayStart(zone)
  /* `bucketByRecency` reads `Date.now()`; the memo stops buckets re-cutting per render. `zone`/`dayStart` cover
     a setting change and the calendar moving. */
  const sections = useMemo(
    () =>
      sectionsFor(rows, groupBy, grouping, deepLinkedId ?? null, zone, focus === true, stateGrouping, tree),
    [rows, groupBy, grouping, deepLinkedId, zone, dayStart, focus, stateGrouping, tree],
  )

  /* THE PICKABLE ROWS THIS FEED DRAWS, for `selection.onDrawn`: the TOP-LEVEL rows of open sections,
     once each (a multi-tagged row repeats). A pick under a folded section is dropped, as the Table drops one. */
  const drawnSelectable = useMemo(() => {
    if (!selection) return []
    const drawn = new Map(
      sections
        .filter(section => section.collapsed !== true)
        .flatMap(section => section.rows)
        .filter(row => selection.isSelectable(row.task))
        .map(row => [row.id, row.task]),
    )
    return [...drawn.values()]
  }, [sections, selection])
  useEffect(() => {
    selection?.onDrawn(drawnSelectable)
  }, [drawnSelectable])

  /* THE LATEST-VALUE REFS (`sameInputs`' block): assigned each render, read only when a row's control is pressed. */
  const actionsRef = useRef<TaskRowActionHandlers>({})
  actionsRef.current = actions ?? {}
  const navigateRef = useRef(navigate)
  navigateRef.current = navigate
  const toggleTaskRef = useRef(onToggleTask)
  toggleTaskRef.current = onToggleTask
  const selectionRef = useRef(selection)
  selectionRef.current = selection
  const stableNavigate = useCallback((to: string) => navigateRef.current(to), [])

  /* An ABSENT handler withholds a verb, so no `actions` bundle renders the read-only feed. The bundle the builder
     sees is rebuilt only when a verb comes or goes or a build-time member changes identity; each verb in it
     forwards to the host's current one. */
  const handlersCache = useRef<{ inputs: unknown[]; handlers: TaskRowActionHandlers } | null>(null)
  const latestActions = actionsRef.current
  const handlerInputs = Object.entries(latestActions)
    .sort(([a], [b]) => a.localeCompare(b))
    .flatMap(([key, value]) => [key, isVerbKey(key, value) ? 'verb' : value])
  if (handlersCache.current === null || !sameInputs(handlersCache.current.inputs, handlerInputs)) {
    const stable: Record<string, unknown> = { ...latestActions }
    for (const [key, value] of Object.entries(latestActions)) {
      if (!isVerbKey(key, value)) continue
      stable[key] = (...args: unknown[]) =>
        (actionsRef.current[key as keyof TaskRowActionHandlers] as ((...a: unknown[]) => void) | undefined)?.(...args)
    }
    handlersCache.current = { inputs: handlerInputs, handlers: stable as TaskRowActionHandlers }
  }
  const handlers = handlersCache.current.handlers
  const isBusy = (id: number) => submitting?.has(id) ?? false

  /* THE SPEC CACHE (`sameInputs`' block): this render's entries replace the last's, so a row that left drops out. */
  const specCache = useRef(new Map<string, { inputs: unknown[]; spec: FeedRowSpec }>())
  const nextSpecs = new Map<string, { inputs: unknown[]; spec: FeedRowSpec }>()
  function cachedSpec(cacheKey: string, inputs: unknown[], build: () => FeedRowSpec): FeedRowSpec {
    const hit = specCache.current.get(cacheKey)
    const spec = hit !== undefined && sameInputs(hit.inputs, inputs) ? hit.spec : build()
    nextSpecs.set(cacheKey, { inputs, spec })
    return spec
  }

  /* An expanded epic's children IN THE TABLE'S ORDER — `epicChildGroups` (`../shared`) flattened: Analyze → Design →
     Plan, the Build phases, Finish, Other. Server order is by id, an accident a re-run stage breaks. A failed fetch
     is a real state, not an empty epic. */
  function childrenOf(row: ViewRow): { rows: ViewRow[]; error?: string; focusHidAll?: boolean } {
    const entry = expanded?.[row.id]
    if (entry === undefined) return { rows: [] }
    if ('error' in entry) return { rows: [], error: entry.error }
    const children = entry.children ?? []
  /* THE FOCUS TOGGLE'S SECOND HALF, applied here because this view builds its own child rows; narrowed BEFORE
     `toViewRows`. The error arm is untouched: a failed fetch is not all-hidden children. */
    const kept = focus === true ? children.filter(isFocusRow) : children
    const ordered = epicChildGroups(kept).flatMap(group => group.tasks)
    /* A Prepare stage is titled `<Stage>: <epic title>` as in the table (`prepareChildTitle`): its stored title is
       the epic's own, and on a phone, where the persona slot is gated off, three rows would read alike. */
    const rows = toViewRows(ordered).map(child => {
      const title = prepareChildTitle(child.task, row.task.title)
      return title === child.title ? child : { ...child, title }
    })
    // Said out loud: an epic whose subtasks are all hidden must not look broken.
    return { rows, focusHidAll: kept.length === 0 && children.length > 0 }
  }

  /* WHICH APPEARANCE CARRIES THE DEEP-LINK DOM ID: the FIRST rendered copy. A plain set, consumed within one render pass; state would outlive it. */
  const idClaimed = new Set<number>()
  function claimElementId(taskId: number): string | undefined {
    if (idClaimed.has(taskId)) return undefined
    idClaimed.add(taskId)
    return taskRowElementId(taskId)
  }

  /* Skeleton on `rows.length`, not sections: a tag grouping emits sections before any row lands. */
  const skeletons = loading === true && rows.length === 0

  const feedSections: FeedSection[] = skeletons
    ? []
    : sections.map(section => ({
        key: section.key,
        // The flat section has no heading, matching the ungrouped list.
        label: section.label === '' ? undefined : section.label,
        /* ABSENT vs `null`: a bucket's exact loaded count vs a tag/project `total: null` meaning UNKNOWN —
           `?? rows.length` would print the loaded window as the total. The table prints nothing then, too. */
        count: section.total === undefined ? section.rows.length : section.total,
        countNote: section.countNote,
        collapsed: section.collapsed,
        onToggle:
          section.onToggle ??
          (section.tagId !== undefined && onToggleSection !== undefined
            ? () => onToggleSection(section.tagId!, section.collapsed === true)
            : undefined),
        /* A COLLAPSED SECTION BUILDS NO ROWS: `claimElementId` is consumed in render order. */
        rows:
          section.collapsed === true
            ? []
            : section.rows.map(row => {
                /* An epic gets a disclosure; no `onToggleTask` leaves every row a leaf (read-only feed). */
                const canExpand = row.epic && onToggleTask !== undefined
                const isOpen = expanded?.[row.id] !== undefined
                const children = canExpand && isOpen ? childrenOf(row) : { rows: [] as ViewRow[] }
                // A subtask moves with its epic, so it wears the epic's feature icon (as the pane's stage rows do).
                const featureIcon = featureIconFor(tree, row.task.feature_id)
                /* Claimed in render order, before the cache: the id is one of the row's inputs. */
                const elementId = claimElementId(row.id)
                const childSpecs = children.rows.map(child => {
                  const childElementId = claimElementId(child.id)
                  const childBusy = isBusy(child.id)
                  return cachedSpec(
                    `${section.key}:${row.id}>${child.id}`,
                    [child.task, child.title, child.age, child.ageTitle, deepLinkedId === child.id, childElementId,
                      handlers, childBusy, orgVisible, featureIcon],
                    () =>
                      buildFeedTaskRow({
                        row: child,
                        depth: 1,
                        deepLinked: deepLinkedId === child.id,
                        /* `revealTask` expands the top-level ancestor, so a deep-linked SUBTASK is the scroll target. */
                        elementId: childElementId,
                        handlers,
                        busy: childBusy,
                        fetching: false,
                        navigate: stableNavigate,
                        orgVisible,
                        featureIcon,
                        // Subtasks are never picked (the bulk verbs act on top-level rows): no `select`, no tick.
                      }),
                  )
                })
                const selectable = selection ? selection.isSelectable(row.task) : null
                const checked = selectable === true ? selection!.store.get().has(row.id) : false
                const busy = isBusy(row.id)
                const rowFetching = fetching?.has(row.id) ?? false
                return cachedSpec(
                  `${section.key}:${row.id}`,
                  [row.task, row.age, row.ageTitle, featureIcon, deepLinkedId === row.id, elementId, handlers, busy,
                    canExpand, isOpen, rowFetching, children.error, children.focusHidAll, orgVisible,
                    selectable, checked, selection?.picking, selection?.store, childSpecs.length, ...childSpecs],
                  () => {
                    /* WHY THE OPEN EPIC SHOWS NO SUBTASKS, in the open disclosure at the children's inset: only
                       an expanded row asks (`childrenOf`), so the words are the expansion's. */
                    const detail = children.error !== undefined || children.focusHidAll === true ? (
                      <Box sx={{ ...rowPadLeft(1), py: 0.25 }}>
                        {children.error !== undefined && (
                          <Typography variant="caption" component="div" color="error" data-testid="feed-children-error">
                            {children.error}
                          </Typography>
                        )}
                        {children.focusHidAll === true && (
                          <Typography variant="caption" component="div" sx={{ color: 'text.secondary' }} data-testid="feed-children-focus-hidden">
                            Every subtask is hidden by focus.
                          </Typography>
                        )}
                      </Box>
                    ) : undefined
                    return buildFeedTaskRow({
                      row,
                      depth: 0,
                      featureIcon,
                      deepLinked: deepLinkedId === row.id,
                      elementId,
                      handlers,
                      busy,
                      expanded: canExpand ? isOpen : undefined,
                      fetching: rowFetching,
                      onToggle: canExpand ? () => toggleTaskRef.current?.(row.id) : undefined,
                      detail,
                      select: selection && (
                        selectable
                          ? {
                              checked,
                              picking: selection.picking,
                              store: selection.store,
                              onToggle: () => selectionRef.current?.onToggle(row.task),
                            }
                          : null
                      ),
                      children: childSpecs,
                      navigate: stableNavigate,
                      orgVisible,
                    })
                  },
                )
              }),
      }))
  specCache.current = nextSpecs

  return (
    <FeedList
      testId="tasks-view-feed"
      ariaLabel="Activity feed"
      sections={feedSections}
      loading={loading}
      emptyMessage={filtered ? 'No tasks match these filters.' : 'No activity.'}
      /* THE CREATE ROW LEADS THE WHOLE FEED, outside every bucket and section; rendered even when empty. */
      leading={create ? <FeedNewTaskRow create={create} /> : undefined}
    />
  )
}
