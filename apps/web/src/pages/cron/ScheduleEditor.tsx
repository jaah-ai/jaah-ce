import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import type { Dispatch, ReactNode, SetStateAction } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import { useTheme } from '@mui/material/styles'
import SchedulerRoot from 'material-ui-cron'
import { cardSpelling } from './cardSpelling'
import { useApiRead } from '../../hooks/useApiRead'
import { useDebouncedCallback } from '../../hooks/useDebouncedCallback'
import { formatZoned, formatZonedSpan } from '../../lib/zonedTime'
import Banner from '../../ui/Banner'
import FormTextField from '../../ui/FormTextField'
import { previewSchedule, type SchedulePreview } from './api'
import {
  INTERVAL_TIMEZONE_NAME,
  SCHEDULE_KIND_CRON,
  SCHEDULE_KIND_INTERVAL,
  intervalUnits,
  type IntervalUnits,
  type ScheduleDraft,
} from './schedule'

/* The schedule editor: the Settings tab's one control for when a cron fires.
 * The expression builder is third-party; hand-building one is forbidden (AC5.1). `material-ui-cron`
 * (MUI-native; `react-js-cron` needs `antd`), pinned exactly: the mount handshake depends on the order
 * of its internal effects and the calendar suppression keys on its `form-col` class and `data-layout`,
 * and both break silently. Bump deliberately and run the suite. Its `@mui/*` deps dedupe onto ours.
 * Not delegated to it: when it fires. The card's calendar uses `cron-parser`, a second parser; the engine
 * fires on `app/cron_schedule.py` (DBOS's croniter, `second_at_beginning=True`, control-plane zone), so
 * next fires come from `POST /api/crons/preview` and the calendar is suppressed (`CARD_WRAP_SX`).
 * Its grammar is narrower (`utils.ts::hasValidCronParts`: exactly five digit-only fields), refusing
 * seconds, `0 0 * * 7`, `30 6 * * MON`, `5#3`, which the engine runs; that verdict is a note, not an error.
 * ── THE STORED EXPRESSION IS AUTHORITATIVE; ONLY AN EXPLICIT EDIT MAY CHANGE IT ──
 * The card re-emits its own derivation through `setCron` ~500ms after mount (`SchedulerHeader`'s
 * debounce), so a host treating every `setCron` as an edit would rewrite a saved schedule on open.
 * `touchedRef` latches on a real interaction; until then output goes to `builderCron`, never the draft,
 * and an unedited save sends no schedule field. A latch alone is not enough: touching the card before its
 * emission lands wrote `0,1,2 * * * *` as `0-2 * * * *`. So the card is inert until it has parsed
 * (`HANDSHAKE_PARSING`, exited on the re-spelling its parse emits, known in advance via `cardSpelling.ts`).
 * Interval mode is a toggle beside the card; `pages/cron/schedule.ts` owns the number↔unit split.
 */

/** Keystroke debounce before a preview POST and its starvation cap (`ui/AsyncLookup`'s pair, widened).
 *  300ms stays under the threshold where a preview feels detached. */
const PREVIEW_DEBOUNCE_MS = 300
const PREVIEW_MAX_WAIT_MS = 1200

/* The card as mounted: denser (padding only, via `CARD_HEADER_SX`); `overflowX` on the wrapper keeps
 * rows reachable at 390px without the page scrolling sideways.
 * Its "Next runs" calendar is hidden: a second, browser-side next-fire implementation that disagreed
 * (for `30 6 * * 1` it said "No upcoming runs" above the server's five Mondays) and swallows failures.
 * No prop drops it, so one selector on the wrapper does: `layout="stacked"` collapses the grid, and the
 * panel is the next sibling of the stable `form-col` class, one level more specific than the library's
 * rules (no `!important`). Its `cron-parser` code still runs unrendered.
 * Its own sentence (`CronReader`, cronstrue, the column's first child) is hidden too: one schedule gets one
 * description, the server's in the summary row, which worded the same schedule differently beside it.
 * A builder refusal still says so (`builderError`'s banner). */
const CARD_WRAP_SX = {
  overflowX: 'auto',
  maxWidth: '100%',
  '& .form-col + *': { display: 'none' },
  '& .form-col > :first-child': { display: 'none' },
  '& .form-col > *': { paddingTop: '6px', paddingBottom: '6px' },
} as const

const CARD_HEADER_SX = { padding: '6px 12px', gap: 1 } as const

/* The card is a light surface in both themes. It paints its chrome from `theme.palette.*`, which under
 * `cssVariables` holds the default (light) scheme's literals, while MUI controls inside it read
 * `theme.vars` and repaint dark — dark-theme text on a white body measured 1.10:1.
 * Fix: scope the card's own colour scheme to the wrapper. `theme.ts`'s
 * `colorSchemeSelector: '[data-theme="%s"]'` emits a bare attribute selector, so the attribute on any
 * element redeclares the palette for its subtree, covering every MUI control at once.
 * The scheme is `theme.defaultColorScheme` (where the frozen literals came from), never a hardcoded
 * `'light'`.
 * Not reached: Autocomplete listboxes portal to `document.body` (legible, console palette), and
 * `applyStyles('dark', …)` rules (descendant selectors still match `<html>`); code inside the card must
 * read `theme.vars`. `auth.tsx` stays the sole writer of the document-root `[data-theme]`. */

/** The card's own default (`material-ui-cron`'s `cronExpInputState` atom), handed back deliberately by
 *  the mount handshake. */
const LIBRARY_DEFAULT_CRON = '0 0 * * *'

/* The mount handshake's four phases. `pending` means "the card has not been told yet"; `settling` is the
 * single commit in which it holds the sentinel; `parsing` is the window in which it is deriving the
 * expression into its own fields and may emit; `done` is every other moment of its life. The card is
 * inert in the first three. */
const HANDSHAKE_PENDING = 'pending'
const HANDSHAKE_SETTLING = 'settling'
const HANDSHAKE_PARSING = 'parsing'
const HANDSHAKE_DONE = 'done'

/** For an expression the card spells as stored (it will emit nothing): how long the card stays hidden for its
 *  parse, above `SchedulerHeader`'s 500ms debounce. Only the reveal waits on it, never what is saved. */
const LIBRARY_QUIET_MS = 600

/** Whether a stored expression needs the handshake: not for an empty draft or the card's own default. */
function handshakeStateFor(expression: string): string {
  return expression && expression !== LIBRARY_DEFAULT_CRON ? HANDSHAKE_PENDING : HANDSHAKE_DONE
}

export type ScheduleEditorProps = {
  /** The draft; the page owns it, this proposes changes. */
  draft: ScheduleDraft
  onChange: (next: ScheduleDraft) => void
  /** The zone the schedule is evaluated in, passed so the card reads the server's clock. */
  timezone: string
  /** Frozen while this tab's save is in flight — the page's rule, not this component's. */
  disabled: boolean
  /** Set for a disabled cron: the row's next-fire segment reads `— <note>` instead of a time. */
  disabledNote?: string
}

/* COLLAPSED BY DEFAULT to one summary row; `Edit` opens the kind select and the builder. The builder
 * MOUNTS only when open, so its handshake runs afresh on every expand. Opens by itself when the draft
 * is not a schedule yet or the server refuses it — both need fixing, not reading. */
export default function ScheduleEditor({
  draft,
  onChange,
  timezone,
  disabled,
  disabledNote,
}: ScheduleEditorProps) {
  const preview = useSchedulePreview(draft)
  const [expanded, setExpanded] = useState(() => previewBody(draft) === null)
  useEffect(() => {
    if (preview.error) setExpanded(true)
  }, [preview.error])
  const bodyId = useId()
  const editRef = useRef<HTMLButtonElement>(null)
  /* The bottom Done unmounts itself with the body: focus goes back to the row's toggle, not to <body>. */
  const close = () => {
    setExpanded(false)
    editRef.current?.focus()
  }

  return (
    <Stack spacing={1}>
      <ScheduleSummary
        draft={draft}
        preview={preview}
        disabledNote={disabledNote}
        action={
          <Button
            ref={editRef}
            size="small"
            variant="text"
            data-testid="cron-schedule-edit"
            aria-expanded={expanded}
            aria-controls={expanded ? bodyId : undefined}
            onClick={() => setExpanded(open => !open)}
            sx={{ minWidth: 0, px: 1, py: 0, ml: 'auto', minHeight: { xs: 44, sm: 0 } }}
          >
            {expanded ? 'Done' : 'Edit'}
          </Button>
        }
      />
      {expanded && (
        <Stack spacing={1.5} id={bodyId} data-testid="cron-schedule-editor">
          <FormTextField
            id="cron-schedule-kind"
            label="Schedule as"
            help={SCHEDULE_KIND_HELP[draft.kind]}
            testId="cron-schedule-kind"
            select
            slotProps={{ inputLabel: { shrink: true } }}
            value={draft.kind}
            disabled={disabled}
            onChange={kind => onChange({ ...draft, kind })}
          >
            <option value={SCHEDULE_KIND_CRON}>A cron expression</option>
            <option value={SCHEDULE_KIND_INTERVAL}>A fixed interval</option>
            {/* An unknown kind renders as itself — a native select with no matching option shows the
                first one, and a blind Save would then rewrite the row to it. */}
            {draft.kind !== SCHEDULE_KIND_CRON && draft.kind !== SCHEDULE_KIND_INTERVAL && (
              <option value={draft.kind}>{draft.kind}</option>
            )}
          </FormTextField>

          {draft.kind === SCHEDULE_KIND_CRON ? (
            <CronBuilder draft={draft} onChange={onChange} timezone={timezone} disabled={disabled} />
          ) : (
            <IntervalField
              seconds={draft.intervalSeconds}
              disabled={disabled}
              onChange={next => onChange({ ...draft, intervalSeconds: next })}
            />
          )}
          {/* The open editor's own edge closes it too: the summary row's Done sits at the far right, past the
              card, where a phone has scrolled away from it. */}
          <Box>
            <Button
              size="small"
              variant="outlined"
              data-testid="cron-schedule-done"
              aria-controls={bodyId}
              onClick={close}
              sx={{ minHeight: { xs: 44, sm: 0 } }}
            >
              Done
            </Button>
          </Box>
        </Stack>
      )}
    </Stack>
  )
}

/* THE THIRD-PARTY CARD plus everything that keeps it from rewriting the stored expression: the reading
 * it is controlled by, the mount handshake, the touch latch. Mounted only while the editor is open. */
function CronBuilder({
  draft,
  onChange,
  timezone,
  disabled,
}: {
  draft: ScheduleDraft
  onChange: (next: ScheduleDraft) => void
  timezone: string
  disabled: boolean
}) {
  /* The card's own reading, separate from the draft: it tracks whatever the card derives. Divergence is
  legitimate; nothing is written behind the operator's back. */
  const [builderCron, setBuilderCron] = useState(draft.expression)
  const [builderError, setBuilderError] = useState('')
  /* The scheme the card's frozen literals came from — see the colour-scheme comment. */
  const cardColorScheme = useTheme().defaultColorScheme
  const touchedRef = useRef(false)

  /* Refs mirroring props and state so `setCron` is created once: the card's prop bridge lists it as an
  effect dependency, and a fresh identity each render ping-pongs a controlled cron (the library records
  that failure). */
  const draftRef = useRef(draft)
  draftRef.current = draft
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const builderRef = useRef(builderCron)
  const disabledRef = useRef(disabled)
  disabledRef.current = disabled

  /* A real interaction — the only way to tell a user edit from the card's own re-derivation, since
  `setCron` fires for both. Capture-phase pointer, key and `input`/`change` events: only people dispatch
  them (React's value assignment does not). It cannot latch before the card is ready (see the header). */
  const markTouched = useCallback(() => {
    if (handshakeRef.current !== HANDSHAKE_DONE) return
    touchedRef.current = true
  }, [])

  /* ── The mount handshake ──
  The card silently loses the expression it is handed on mount (`30 6 * * 1` settles as `0 0 * * *`),
  in a real browser only; the guard is `e2e/cron-schedule-editor.spec.ts`.
  Why: `SchedulerHeader` seeds its debounce with the atom's default and parses it immediately, writing
  it over the ingested expression; the bridge's `prevSync` then sees no change.
  Fix: make the prop change — hand the card the library default for one commit, then the stored
  expression, which lands as a genuine parent change. Re-armed when the stored expression changes from
  outside (a landed save, a switch back from interval mode). */
  const [handshake, setHandshake] = useState<string>(() => handshakeStateFor(draft.expression))
  const handshakeRef = useRef(handshake)
  handshakeRef.current = handshake
  /* The spelling the card's parse will emit (`cardSpelling.ts`), set at the hand-over; equal to the stored
  expression when the parse emits nothing. */
  const parsedRef = useRef('')

  /* Not inside a state updater: updaters must be pure (React may call them twice), and notifying the
  parent is this handler's contract. */
  const setCron = useCallback<Dispatch<SetStateAction<string>>>(update => {
    const previous = builderRef.current
    const next = typeof update === 'function' ? update(previous) : update
    if (next === previous) return

    /* The card's reading is always recorded: it is what the card displays (and `ReadingNote` compares).
    During handshake commits it echoes the sentinel, and the wrapper is hidden then. */
    builderRef.current = next
    setBuilderCron(next)

    /* The handshake gate precedes the touch gate: a touched card's mid-handshake derivation must not be
    written as an edit. The parse's own re-spelling is the card's signal that it has parsed: it closes the
    window, never a clock, so a starved host cannot open the touch latch ahead of it. */
    if (handshakeRef.current !== HANDSHAKE_DONE) {
      if (handshakeRef.current === HANDSHAKE_PARSING && next === parsedRef.current) setHandshake(HANDSHAKE_DONE)
      return
    }

    /* A frozen tab (save in flight) accepts nothing. `SchedulerProps` has no `disabled`, so the card is
    inert via `pointerEvents` and its emissions are refused here. */
    if (disabledRef.current) return

    // The touch gate: an untouched card reports its own reading, not a decision.
    if (touchedRef.current) {
      onChangeRef.current({ ...draftRef.current, expression: next })
      return
    }
    /* An untouched card's reading is shown, never written: it re-spells expressions in its grammar
    (`selector.ts`: `0,1,2` → `0-2`, `0-59` → `*`), and `ReadingNote` says when it disagrees. */
  }, [])

  /* A layout effect: `useEffect` runs after paint and would flash the sentinel commit for a frame. */
  useLayoutEffect(() => {
    if (handshake === HANDSHAKE_PENDING) {
      builderRef.current = LIBRARY_DEFAULT_CRON
      setBuilderCron(LIBRARY_DEFAULT_CRON)
      setHandshake(HANDSHAKE_SETTLING)
      return
    }
    if (handshake !== HANDSHAKE_SETTLING) return
    parsedRef.current = cardSpelling(draftRef.current.expression)
    builderRef.current = draftRef.current.expression
    setBuilderCron(draftRef.current.expression)
    setHandshake(HANDSHAKE_PARSING)
  }, [handshake])

  /* A parse that emits nothing (the card already spells it so, or refuses it) gives no signal: the window
  closes on quiet. Nothing can be written by then or later — the card has nothing to emit — so the clock
  decides only when the card shows. */
  useEffect(() => {
    if (handshake !== HANDSHAKE_PARSING || parsedRef.current !== draftRef.current.expression) return
    const timer = setTimeout(() => setHandshake(HANDSHAKE_DONE), LIBRARY_QUIET_MS)
    return () => clearTimeout(timer)
  }, [handshake])

  /* The draft pushed back down for outside changes (a landed save, a kind switch), gated on the draft
  having actually moved, so card-originated changes are not echoed back into a feedback loop. It does
  not clear `touchedRef`. */
  const lastDraftRef = useRef(draft.expression)
  useEffect(() => {
    if (draft.expression === lastDraftRef.current) return
    lastDraftRef.current = draft.expression
    /* The origin guard runs first: every card keystroke reaches the draft, and re-arming the handshake then
    would hand the card the sentinel mid-word, leaving draft and card on the library default. */
    if (draft.expression === builderRef.current) return
    // A new stored expression earns a fresh handshake.
    setHandshake(handshakeStateFor(draft.expression))
    builderRef.current = draft.expression
    setBuilderCron(draft.expression)
  }, [draft.expression])

  return (
    <>
      {/* Above the card, beside its expression field: the field shows the builder's reading, and this says when
          that is not what is saved. */}
      <ReadingNote stored={draft.expression} reading={builderCron} />
      <Box
        /* Hidden for the handshake commits, inert through the parse window, frozen during a save. */
        sx={[
          CARD_WRAP_SX,
          handshake !== HANDSHAKE_DONE && { visibility: 'hidden' },
          (handshake !== HANDSHAKE_DONE || disabled) && { pointerEvents: 'none' },
          disabled && { opacity: 0.6 },
        ]}
        onPointerDownCapture={markTouched}
        onKeyDownCapture={markTouched}
        onInputCapture={markTouched}
        onChangeCapture={markTouched}
        // The card's palette, scoped to the card — see the colour-scheme comment.
        data-theme={cardColorScheme}
        data-testid="cron-expression-builder"
      >
        <SchedulerRoot
          cron={builderCron}
          setCron={setCron}
          setCronError={setBuilderError}
          // Sub-daily periods: the page and every `/api/crons` route are superadmin-only already.
          isAdmin
          timezone={timezone}
          // One column, asked for rather than overridden — see `CARD_WRAP_SX`.
          layout="stacked"
          // The header's field is the card's reading, never the saved value (`ReadingNote`).
          title="Builder’s reading"
          slotProps={{ header: { sx: CARD_HEADER_SX } }}
        />
      </Box>
      {builderError && (
        /* Info, not error: the card refuses expressions the engine runs (six fields, Sunday as 7, names). */
        <Banner severity="info">Builder can’t show this; the scheduler accepts it.</Banner>
      )}
    </>
  )
}

/* The card re-spells some schedules (`0,1,2` → `0-2`, `0-59` → `*`); the draft keeps the stored one.
 * One line, at body size right over the card's expression field, says which is saved. */
function ReadingNote({ stored, reading }: { stored: string; reading: string }) {
  if (!stored || !reading || reading === stored) return null
  return (
    <Typography
      variant="body2"
      role="status"
      data-testid="cron-reading-note"
      sx={{ display: 'block', color: 'text.secondary', overflowWrap: 'anywhere' }}
    >
      {'Builder spells it '}
      <Box component="code" sx={CODE_SX}>{reading}</Box>
      {'; saved '}
      <Box component="code" sx={CODE_SX}>{stored}</Box>
      {' is unchanged.'}
    </Typography>
  )
}

const CODE_SX = {
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  fontSize: '0.8125rem',
  overflowWrap: 'anywhere',
} as const

const SCHEDULE_KIND_HELP: Record<string, string> = {
  [SCHEDULE_KIND_CRON]: 'Fires at the named times, in the control plane’s zone.',
  [SCHEDULE_KIND_INTERVAL]: `Fires at a fixed interval, in ${INTERVAL_TIMEZONE_NAME}.`,
}

/* The interval as a number and unit. Any whole number: the server fires it on a fixed UTC grid
 * (`app/cron_schedule.py`) and refuses only a value out of range, by name. This form is not the validator. */
function IntervalField({
  seconds,
  disabled,
  onChange,
}: {
  seconds: number | null
  disabled: boolean
  onChange: (next: number | null) => void
}) {
  /* The unit is state, not derived from the value: derived, clearing the count would flip it to seconds,
  and typing 60 in minutes would flip to hours and show "1". Seeded once; re-seeded only when the stored
  value moves from outside. */
  const [units, setUnits] = useState<IntervalUnits>(() => intervalUnits(seconds))
  const storedRef = useRef(seconds)
  useEffect(() => {
    if (seconds === storedRef.current) return
    storedRef.current = seconds
    setUnits(intervalUnits(seconds))
  }, [seconds])

  const change = (next: IntervalUnits) => {
    setUnits(next)
    // The value about to be reported is not an outside change; the re-seed must not undo it.
    storedRef.current = next.count === null ? null : next.count * next.unit
    onChange(storedRef.current)
  }

  return (
    <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-start', flexWrap: 'wrap' }}>
      <Box sx={{ flex: '1 1 8rem', minWidth: '7rem' }}>
        <FormTextField
          id="cron-interval-count"
          label="Every"
          help={INTERVAL_HELP}
          type="number"
          testId="cron-interval-count"
          /* `min`/`step` are keyboard affordances, not validation; the server holds the rule. */
          slotProps={{ htmlInput: { min: 1, step: 1, inputMode: 'numeric' }, inputLabel: { shrink: true } }}
          value={units.count === null ? '' : String(units.count)}
          disabled={disabled}
          onChange={v => change({ count: countFrom(v), unit: units.unit })}
        />
      </Box>
      <Box sx={{ flex: '1 1 9rem', minWidth: '8rem' }}>
        <FormTextField
          id="cron-interval-unit"
          label="Unit"
          testId="cron-interval-unit"
          slotProps={{ inputLabel: { shrink: true } }}
          value={units.unit}
          disabled={disabled}
          onChange={v => change({ count: units.count, unit: Number(v) })}
          options={[
            { value: '1', label: 'seconds' },
            { value: '60', label: 'minutes' },
            { value: '3600', label: 'hours' },
            { value: '86400', label: 'days' },
          ]}
        />
      </Box>
    </Box>
  )
}

/** The typed count, or null for an emptied field (refused by name on save; not zero). */
function countFrom(raw: string): number | null {
  const trimmed = raw.trim()
  if (!trimmed) return null
  const count = Number(trimmed)
  return Number.isFinite(count) ? Math.trunc(count) : null
}

/** Why "every 3 hours" lands where it does: the grid, not the moment of saving. */
const INTERVAL_HELP = `Any whole number; from an hour up, whole minutes. Runs on a fixed ${INTERVAL_TIMEZONE_NAME} grid: every 3 hours is 00:00, 03:00, 06:00…`

/* The authoritative preview: `POST /api/crons/preview` walks `app/cron_schedule.py::next_fire_for_row`,
 * the function the engine registers with. A 422 is a result: the refusal Save would earn. */
type PreviewState = { preview: SchedulePreview | null; error: string; loading: boolean }

function useSchedulePreview(draft: ScheduleDraft): PreviewState {
  /* The request the draft settled on (debounced, with a starvation cap); `null` asks nothing. `useApiRead` keys
     on it, so a superseded answer is DISCARDED, never rendered out of order. */
  const [body, setBody] = useState(() => previewBody(draft))
  const read = useApiRead(
    ['schedule-preview', body],
    () => previewSchedule(body as NonNullable<typeof body>),
    'This schedule could not be previewed.',
    { enabled: body !== null },
  )
  const run = useCallback(() => setBody(previewBody(draft)), [draft])
  const { trigger } = useDebouncedCallback(run, PREVIEW_DEBOUNCE_MS, PREVIEW_MAX_WAIT_MS)

  /* NO CLEANUP, and NOT `trigger` in the deps — both deliberate. Returning `cancel` would clear the
     max-wait timer on every keystroke (the starvation cap would bound nothing); the hook cancels its own
     timers on unmount. `trigger` is re-minted every render, so listing it re-POSTs forever. */
  useEffect(() => {
    trigger()
  }, [draft.kind, draft.expression, draft.intervalSeconds])

  // A refusal stays up until the next answer lands, as the preview it replaced did; a draft that asks nothing clears it.
  const landedError = useRef(read.error)
  if (body === null || !read.loading) landedError.current = body === null ? '' : read.error
  if (body === null) return { preview: null, error: '', loading: false }
  const error = read.loading ? landedError.current : read.error
  // Loading from the first frame when there is something to ask, so the row never flashes "Not set".
  return { preview: error ? null : read.data, error, loading: read.loading }
}

/* THE COLLAPSED ROW: label, the server's sentence, the raw expression muted, and the next fire — whose
 * hover lists the next five, each naming the control plane's zone (`lib/zonedTime`). */
function ScheduleSummary({
  draft,
  preview: { preview, error, loading },
  disabledNote,
  action,
}: {
  draft: ScheduleDraft
  preview: PreviewState
  disabledNote?: string
  action: ReactNode
}) {
  // Seconds only when the answer needs them — five ten-second firings print as one minute otherwise.
  const subMinute = hasSubMinuteGap(preview?.next_fire_at ?? [])
  const fmt = (iso: string) => formatZoned(iso, preview?.timezone, { seconds: subMinute })
  /* The next fire as a fact ("next run Oct 21, 09:00 UTC"): this year's date drops its year; a sub-minute
     schedule keeps the seconds `formatZonedSpan` has no room for. */
  const nextFact = (iso: string) => `next run ${subMinute ? fmt(iso) : formatZonedSpan(iso, null, preview?.timezone).start}`
  const raw = draft.kind === SCHEDULE_KIND_CRON ? draft.expression.trim() : ''

  let body: ReactNode
  if (error) {
    body = (
      <Typography variant="body2" role="alert" sx={{ color: 'error.main', overflowWrap: 'anywhere' }}
        data-testid="cron-preview-error">
        {error}
      </Typography>
    )
  } else if (preview === null) {
    body = (
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        {loading ? 'Working out…' : 'Not set'}
      </Typography>
    )
  } else {
    body = (
      <>
        <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }} data-testid="cron-preview-sentence">
          {preview.schedule_human}
        </Typography>
        {raw && <Box component="code" sx={{ ...CODE_SX, color: 'text.secondary' }}>{raw}</Box>}
        {/* A disabled cron still hovers its would-be fires — what enabling it would start. */}
        <Tooltip
          describeChild
          enterTouchDelay={0}
          leaveTouchDelay={6000}
          title={
            <Box component="ol" sx={PREVIEW_LIST_SX} data-testid="cron-preview-list">
              {preview.next_fire_at.map(iso => <li key={iso}>{fmt(iso)}</li>)}
            </Box>
          }
        >
          <Typography
            variant="body2"
            component="span"
            tabIndex={0}
            data-testid="cron-preview-next"
            sx={{
              color: 'text.secondary',
              textDecoration: 'underline dotted',
              textUnderlineOffset: '3px',
              cursor: 'help',
              // Inline-block: the separator keeps out of the dotted underline that marks the hover.
              '&::before': { content: disabledNote ? '"—"' : '"·"', display: 'inline-block', mr: '0.35em' },
              // Too narrow for one line: its own line last, as a sentence ("Next run …"), not an orphaned "· next".
              [`@container ${SUMMARY_CONTAINER} (width < 30rem)`]: {
                flexBasis: '100%', order: 1, '&::before': { content: 'none' }, '&::first-letter': { textTransform: 'uppercase' },
              },
            }}
          >
            {disabledNote
              ?? (preview.next_fire_at.length === 0 ? 'never fires' : nextFact(preview.next_fire_at[0]))}
          </Typography>
        </Tooltip>
      </>
    )
  }

  return (
    <Box
      data-testid="cron-schedule-preview"
      sx={{
        display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', columnGap: 1, rowGap: 0.5,
        containerType: 'inline-size', containerName: SUMMARY_CONTAINER,
      }}
    >
      <Typography variant="caption" component="span" sx={{ color: 'text.secondary', fontWeight: 600 }}>
        Schedule
      </Typography>
      {body}
      {action}
    </Box>
  )
}

const SUMMARY_CONTAINER = 'schedule-summary'

const PREVIEW_LIST_SX = {
  m: 0,
  pl: 2.5,
  fontSize: '0.8125rem',
  '& li': { mb: 0.25 },
} as const

/** True when any two consecutive firings are less than a minute apart. */
function hasSubMinuteGap(isos: string[]): boolean {
  for (let i = 1; i < isos.length; i += 1) {
    if (Date.parse(isos[i]) - Date.parse(isos[i - 1]) < 60_000) return true
  }
  return false
}

/** The preview request, or null when the draft is not yet a schedule (asking would 422 mid-typing). */
function previewBody(draft: ScheduleDraft) {
  if (draft.kind === SCHEDULE_KIND_CRON) {
    const expression = draft.expression.trim()
    return expression ? { schedule_kind: draft.kind, cron_expression: expression } : null
  }
  if (draft.kind === SCHEDULE_KIND_INTERVAL) {
    return draft.intervalSeconds === null
      ? null
      : { schedule_kind: draft.kind, interval_seconds: draft.intervalSeconds }
  }
  return null
}
