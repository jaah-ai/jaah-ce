import { useEffect, useId, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import ClickAwayListener from '@mui/material/ClickAwayListener'
import IconButton from '@mui/material/IconButton'
import Paper from '@mui/material/Paper'
import Popper from '@mui/material/Popper'
import Typography from '@mui/material/Typography'
import type { QAEntry, TaskQuestion } from '../../types'
import { absoluteTime, relativeAge } from '../../lib/relativeTime'
import { DISMISSED_OUTCOME_TEXT, resolvedByLine } from '../../lib/resolvedBy'
import { StatusChip } from '../../ui/StatusChip'
import { Banner } from '../../ui/Banner'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT, RADIUS, grid } from '../../theme'
import {
  nonBlank,
  optionDescription,
  OptionDescription,
  OptionPreview,
  RecommendedBadge,
  RoundHeaderChip,
  showsRecommended,
} from './questionParts'
import Tip from '../../ui/Tip'

/* Spec R6 — the persisted Q&A trail for a Task from `qa_entries` (oldest-first on the wire; shown NEWEST
   FIRST — see the reversal below). All plain text (never dangerouslySetInnerHTML): agent-authored content.
   ONE TRAIL, TWO PLACEMENTS, both on the Q&A tab: the trail alone, and the gate form's `History`. It is
   the UNION of two former renderers; neither surface may grow a private field. The only difference is a
   filter on the ENTRIES: the gate form passes only settled ones (`GateForm`, via `settledEntries`); the
   trail alone passes all.
   A CONVERSATION, NOT A FORM: each entry draws two lines — `Jaah: <question>` and
   `<answerer>: <answer>` with a ✎ Revise beside it. Every other fact (ordinal, header, status, options
   with their prose, comment, byline, delivery) is the entry's DETAILS card, opened on hover, focus or tap
   (`EntryDetails`). The details stay MOUNTED while closed, so a screen reader reaches them through
   `aria-describedby` and nothing is lost when the card is shut.
   The option prose is here because this is the COLD PATH (`routes/tasks/answers.py::_record_answered_round`: "a
   header missing from it is a header lost forever on the cold path"); the runner prompt reads
   `entry.header`/`entry.option_details` for the other half. */
export default function QATrail({ entries, questions, submitting, error, onRevise }: {
  entries: QAEntry[]
  /* The live rounds: an entry is a SNAPSHOT, and only the row knows whether that settlement still stands
     (a just-reopened round would otherwise show its old answer and a Revise that 409s). Matched on
     `QAEntry.question_id` (question text is not a key). Also the source of the ordinal (`round`), current
     status and `comment`; no live row → none of the three. */
  questions: TaskQuestion[]
  /* The page's ONE gate-mutation busy flag, shared with the answer form: Revise and answers act on the
     same rounds, and two flags would allow two mutations on one row. `GateSteps`' Send takes it too. */
  submitting: boolean
  /** A failed Revise, said where it was clicked (a different tab or verb from `error`'s owner). */
  error: string
  /* WITHHELD (undefined), not disabled, when the operator may not revise (as `GateForm`'s `readOnly`); also
     on a chat, whose rounds are answered in its own pane (revise 409s). */
  onRevise?: (questionId: number) => void
}) {
  /* NEWEST FIRST: oldest-first buried the round just settled or still waiting.
     Only the trailing legacy `awaiting_response_body` entry (APPENDED by `_task_qa_entries`) is lifted to
     the top; a round reopened via `/revise-question` keeps its chronological seat
     (`_clear_reopened_resolutions` strips it in place). The gate form lists open rounds OLDEST-FIRST — the
     RECORD vs the WORKING QUEUE, deliberately opposite. Do not "fix" one to match the other.
     Reversed at RENDER only: `_task_qa_entries` also feeds `_task_qa_trail` (the runner prompt), which must
     stay chronological. Keyed by the ORIGINAL index so new rounds do not re-key every row.
     No header grouping: the header is a detail, so it lives in each entry's card. */
  const newestFirst = entries.map((entry, i) => ({ entry, i })).reverse()
  return (
    /* No heading: the host owns it (the subtab is the tab; `GateForm` has `History (n)`). */
    <Box data-testid="qa-trail" sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      {error && <Banner severity="error" testId="qa-revise-error">{error}</Banner>}
      <Box component="ul" sx={{ listStyle: 'none', m: 0, p: 0, display: 'flex', flexDirection: 'column', gap: 1 }}>
        {newestFirst.map(({ entry, i }) => (
          <QAEntryCard
            key={i}
            entry={entry}
            round={liveRound(entry, questions)}
            anyOpen={questions.some(q => q.status === 'open')}
            submitting={submitting}
            onRevise={onRevise}
          />
        ))}
      </Box>
    </Box>
  )
}

/* Who asked: every round is the runner's, and the console calls it by the product's name. */
const ASKER = 'Jaah'
/* The answer's speaker when the round records nobody (an unresolved ask, a deleted account). */
const UNKNOWN_ANSWERER = 'Operator'
/* The question line's ceiling; a longer question is read whole in the details card. */
const QUESTION_LINES = 4
const LONG_QUESTION_CHARS = 280
/* Judged against the VIEWPORT, not the scrollport: an entry low in the Q&A tab's history flips its card
   above itself instead of opening off-screen. Module constants: new objects per render rebuild the popper. */
const POPPER_OPTIONS = { strategy: 'fixed' } as const
const POPPER_MODIFIERS = [
  { name: 'offset', options: { offset: [0, 4] } },
  { name: 'flip', options: { boundary: document.documentElement, fallbackPlacements: ['top-start'] } },
  { name: 'preventOverflow', options: { boundary: document.documentElement, padding: 8 } },
]

/* The speaker label before a line (`Jaah:`) — one spelling for both lines. */
const SPEAKER_SX = { fontWeight: FONT_WEIGHT.semibold, color: 'text.secondary', mr: 0.5, flexShrink: 0 } as const

function QAEntryCard({ entry, round, anyOpen, submitting, onRevise }: {
  entry: QAEntry
  round: TaskQuestion | undefined
  anyOpen: boolean
  submitting: boolean
  onRevise?: (questionId: number) => void
}) {
  const detailsId = useId()
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const [open, setOpen] = useState(false)
  /* Escape dismisses an open card wherever focus is (a hover-only card must be dismissible too). */
  useEffect(() => {
    if (!open) return undefined
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open])
  /* Whether the line actually cut the question, measured as the card opens (a narrow phone clamps a
     question a wide pane shows whole); the length rule covers a layout nobody measured. */
  const questionRef = useRef<HTMLElement | null>(null)
  const [clamped, setClamped] = useState(false)
  const show = () => {
    const line = questionRef.current
    setClamped(line !== null && line.scrollHeight > line.clientHeight + 1)
    setOpen(true)
  }
  const hide = () => setOpen(false)
  const question = entry.question ?? ''
  const longQuestion = question.length > LONG_QUESTION_CHARS || question.split('\n').length > QUESTION_LINES

  /* The outcome from `status`, inferred from the resolution when absent: `status` is additive and an
     older API pod (rolling deploy) omits it, so older trails render as before (`unresolvedAsk`). */
  const dismissed = entry.status === 'dismissed'
  /* Blank is not an answer: one predicate decides both the reply and its colour. */
  const resolution = nonBlank(entry.resolution)
  const answered = !dismissed && resolution !== null
  /* Already reopened (here or in another tab): the snapshot is superseded; no second Revise. */
  const reopened = round?.status === 'open'
  const resolved = (answered || dismissed) && !reopened
  /* The status chip: the LIVE row's where present (only it knows about a reopen), else the snapshot's —
     never composed here, so unknown API values render verbatim in the neutral tone. */
  const status = round?.status || entry.status || null
  /* Where the answer went: the CP hands every answer over at once, when no round is open.
     Unknown (an older API pod, no live row) says nothing. */
  const delivery = !resolved || round?.delivered_at === undefined
    ? null
    : round.delivered_at
      ? { text: `Sent to the runner · ${relativeAge(round.delivered_at)}`, title: absoluteTime(round.delivered_at) }
      : anyOpen
        ? { text: 'Held — delivered when all rounds are answered', title: undefined }
        : null
  /* Who settled it and when, under the entry's own verb (`resolvedVerb`; a dismissal is
     not "Answered by"). Dropped on a reopened round. */
  const byline = resolved
    ? resolvedByLine({
      by: entry.resolved_by,
      at: entry.resolved_at,
      // `|| 'answered'` for the rolling-deploy reason above.
      status: entry.status || 'answered',
    })
    : null
  /* The reply, five claims. An answer is its own words; every other outcome is an ICON, never a sentence
     (the operator's call), named for a screen reader and on hover:
       · reopened → superseded; a new answer is owed (amber, like open);
       · dismissed → nothing was decided (`DISMISSED_OUTCOME_TEXT`), muted;
       · answered → the resolution, never truncated (a multi-select joins every label; it wraps);
       · still ASKED → ⏳ (the trailing entry a parked task adds);
       · anything else → —, muted (an unknown settled word, or a blank resolution; the chip names it). */
  const outcome = reopened
    ? { tone: 'warning.main', icon: '↩', label: 'Reopened — awaiting a new answer' }
    : dismissed
      ? { tone: 'text.secondary', icon: '⊘', label: DISMISSED_OUTCOME_TEXT }
      : answered
        ? null
        : unresolvedAsk(entry, round)
          ? { tone: 'warning.main', icon: '⏳', label: 'Awaiting answer' }
          : { tone: 'text.secondary', icon: '—', label: 'Not answered' }
  const answerer = nonBlank(entry.resolved_by) ?? UNKNOWN_ANSWERER
  /* REVISE — the one write on an audit surface: an answer need not be final. On BOTH surfaces: the Answer
     tab is where a mind changes; the Q&A subtab survives the gate (`GateForm` disappears once all rounds
     settle). Withheld when: `onRevise` absent (no permission, or a chat — as `GateForm`'s `readOnly`);
     `round` undefined (no live row; the server would 409, never guess); `reopened` (already open).
     One click, no confirm (the operator's call); `disabled={submitting}` is the only brake. */
  const canRevise = onRevise !== undefined && round !== undefined && !reopened

  return (
    <Box
      component="li"
      ref={setAnchor}
      data-testid="qa-entry"
      data-open={open ? 'true' : 'false'}
      aria-describedby={detailsId}
      /* One tab stop per entry: the pencil's when there is one (its focus bubbles here and opens the card),
         else the entry's own. */
      tabIndex={canRevise ? undefined : 0}
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
      /* A tap opens it (touch has no hover). */
      onClick={show}
      sx={{
        px: 1,
        py: 0.5,
        bgcolor: 'background.paper',
        border: 1,
        borderColor: open ? 'secondary.main' : 'divider',
        borderLeft: 3,
        borderLeftColor: 'secondary.main',
        borderRadius: RADIUS.sm,
        display: 'flex',
        flexDirection: 'column',
        gap: 0.5,
        cursor: 'default',
        outline: 'none',
        transition: 'border-color 120ms',
        '&:focus-visible': { outline: '2px solid', outlineColor: 'secondary.main' },
      }}
    >
      {/* The question is raw multi-line 🛑 FLEET BLOCKED markdown — preserve its newlines so the
          "**Options:**" lines do not collapse onto one. */}
      <Typography
        variant="body2"
        data-testid="qa-question"
        sx={{ display: 'flex', alignItems: 'baseline', color: 'text.primary', fontWeight: FONT_WEIGHT.semibold, minWidth: 0 }}
      >
        <Box component="span" sx={SPEAKER_SX}>{ASKER}:{' '}</Box>
        {/* Clamped: a long blocked-question body is read whole in the card (`longQuestion`). */}
        <Box
          component="span"
          ref={questionRef}
          sx={{
            overflowWrap: 'anywhere',
            whiteSpace: 'pre-wrap',
            minWidth: 0,
            display: '-webkit-box',
            WebkitBoxOrient: 'vertical',
            WebkitLineClamp: QUESTION_LINES,
            overflow: 'hidden',
          }}
        >
          {question}
        </Box>
      </Typography>
      <Typography
        variant="body2"
        component="div"
        sx={{ display: 'flex', alignItems: 'baseline', gap: 0.5, minWidth: 0 }}
      >
        <Box component="span" sx={SPEAKER_SX} data-testid="qa-answerer">{answerer}:{' '}</Box>
        <Tip title={outcome?.label}><Box
          component="span"
          data-testid="qa-resolution"
          data-resolved={resolved ? 'true' : 'false'}
          role={outcome ? 'img' : undefined}
          aria-label={outcome?.label}
          sx={{ color: outcome ? outcome.tone : 'success.main', overflowWrap: 'anywhere', minWidth: 0 }}
        >
          {outcome ? outcome.icon : resolution}
        </Box></Tip>
        {canRevise && (
          <Tip title="Revise — reopens this round. The answer moves to the activity log."><IconButton
            data-testid="qa-revise"
            size="small"
            color="secondary"
            disabled={submitting}
            /* Named by its round (six pencils are indistinguishable): the header,
               else the question clipped to 80 chars. */
            aria-label={`Revise answer to ${nonBlank(entry.header) || (entry.question ?? '').slice(0, 80) || 'this question'}`}
            /* Promises no delivery: with no live runner the record is corrected and nobody is told. */
            onClick={event => {
              // The press is the pencil's, not a tap opening the card.
              event.stopPropagation()
              onRevise(round.id)
            }}
            sx={theme => ({
              p: 0,
              ml: 0,
              fontSize: FONT_SIZE.base,
              lineHeight: LINE_HEIGHT.tight,
              alignSelf: 'center',
              flexShrink: 0,
              /* The 44px tap floor on a phone, without growing the line: the negative margins hand the
                 extra box back, so the glyph stays beside the answer. */
              [theme.breakpoints.down('sm')]: {
                width: theme.custom?.tapTargetPx ?? 44,
                height: theme.custom?.tapTargetPx ?? 44,
                my: -1.5,
                ml: -0.5,
                mr: -1.5,
              },
            })}
          >
            ✎
          </IconButton></Tip>
        )}
      </Typography>
      <Popper
        open={open && anchor !== null}
        anchorEl={anchor}
        placement="bottom-start"
        /* MOUNTED while closed (hidden), inside the entry: the details are the entry's description for a
           screen reader, and tests read them in place. `fixed` lets the card escape the Q&A tab's
           scrollport instead of being clipped by it. */
        keepMounted
        disablePortal
        popperOptions={POPPER_OPTIONS}
        modifiers={POPPER_MODIFIERS}
        /* Read-only and see-through to the pointer: a card over the next entry never blocks moving onto
           it (the pointer reaches that entry, this one closes). */
        sx={{ zIndex: theme => theme.zIndex.tooltip, maxWidth: `min(${grid(85)}, calc(100vw - ${grid(5)}))`, pointerEvents: 'none' }}
      >
        {/* A tap on the entry itself is the tap that opened it, not a click away. */}
        <ClickAwayListener
          /* Listening only while open: a closed card owns no document listener. */
          mouseEvent={open ? 'onClick' : false}
          touchEvent={open ? 'onTouchEnd' : false}
          onClickAway={event => {
            if (!(event.target instanceof Node && anchor?.contains(event.target))) setOpen(false)
          }}
        >
          <Paper
            elevation={6}
            sx={{ p: 1.5, border: 1, borderColor: 'secondary.main', bgcolor: 'background.paper' }}
          >
            <EntryDetails
              id={detailsId}
              entry={entry}
              round={round}
              status={status}
              outcomeLabel={outcome?.label ?? null}
              byline={byline}
              delivery={delivery}
              reopened={reopened}
              fullQuestion={longQuestion || clamped ? question : null}
            />
          </Paper>
        </ClickAwayListener>
      </Popper>
    </Box>
  )
}

/* Everything the two lines leave out, in reading order: what round it was, what was on offer, what the
   operator added, who settled it and where it went. */
function EntryDetails({ id, entry, round, status, outcomeLabel, byline, delivery, reopened, fullQuestion }: {
  id: string
  entry: QAEntry
  round: TaskQuestion | undefined
  status: string | null
  /* The non-answer outcome in words (the reply line shows only its icon). */
  outcomeLabel: string | null
  byline: string | null
  delivery: { text: string; title: string | undefined } | null
  reopened: boolean
  /* The whole question where the line clamps it; null when the line already shows it all. */
  fullQuestion: string | null
}) {
  return (
    <Box id={id} data-testid="qa-details" sx={{ display: 'flex', flexDirection: 'column', gap: 0.5, minWidth: 0 }}>
      {/* The round's ordinal (from the LIVE row; what an answer routes on), its own header and status.
          Guarded on having content (an empty wrapper would still take the column `gap`). */}
      {(round || nonBlank(entry.header) || status) && (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
          {round && (
            <Typography variant="caption" data-testid="qa-round" sx={{ fontWeight: FONT_WEIGHT.semibold, color: 'text.secondary' }}>
              Round {round.round}
            </Typography>
          )}
          <RoundHeaderChip header={entry.header} />
          {/* Never translated: green = decision landed, amber = one owed, anything else neutral. */}
          {status && (
            <StatusChip
              label={status}
              tone={status === 'answered' ? 'success' : status === 'open' ? 'warning' : 'neutral'}
            />
          )}
        </Box>
      )}
      {fullQuestion && (
        <Typography
          variant="caption"
          data-testid="qa-full-question"
          sx={{ color: 'text.primary', overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}
        >
          {fullQuestion}
        </Typography>
      )}
      {outcomeLabel && (
        <Typography variant="caption" data-testid="qa-outcome" sx={{ color: 'text.secondary' }}>
          {outcomeLabel}
        </Typography>
      )}
      {entry.options.length > 0 && (
        <Box
          component="ul"
          data-testid="qa-options"
          sx={{ m: 0, pl: 3, display: 'flex', flexDirection: 'column', gap: 0 }}
        >
          {entry.options.map((opt, j) => {
            /* `j` is the source index: `option_details` is index-aligned with `options` (`QuestionOption`
               in types.ts) and nothing is filtered here. Missing is ordinary (`[]` on old entries, a
               short array, a bare label, or the FIELD absent from an older API pod during a rolling
               deploy) — hence `?.` despite the non-nullable type. */
            const detail = entry.option_details?.[j]
            const description = optionDescription(opt, detail)
            const preview = nonBlank(detail?.preview)
            return (
              /* A `list-item`, not flex (a flex `li` keeps no marker). The muted colour is declared here
                 once: `OptionDescription` is `color: inherit` by design. */
              <Typography
                component="li"
                key={j}
                variant="caption"
                sx={{ color: 'text.secondary', overflowWrap: 'anywhere', minWidth: 0 }}
              >
                {opt}
                {/* The spacer is load-bearing: this `li` has no flex `gap` (a text space also lets the
                    badge wrap whole). `showsRecommended` skips labels already saying `(Recommended)`. */}
                {showsRecommended(opt, detail) && <>{' '}<RecommendedBadge /></>}
                {description && <OptionDescription text={description} />}
                {preview && <OptionPreview text={preview} />}
              </Typography>
            )
          })}
        </Box>
      )}
      {/* The operator's comment, read off the LIVE row (the projection never carried it). Chosen option
          and the human's remark are separate facts. Dropped on a reopened round. */}
      {!reopened && nonBlank(round?.comment) && (
        <Typography
          variant="caption"
          data-testid="qa-comment"
          sx={{ overflowWrap: 'anywhere', whiteSpace: 'pre-wrap', color: 'text.primary' }}
        >
          Comment: {round?.comment}
        </Typography>
      )}
      {/* Attribution last and quietest; the exact moment rides `title`. */}
      {byline && (
        <Tip title={entry.resolved_at ? absoluteTime(entry.resolved_at) : undefined}><Typography
          variant="caption"
          data-testid="qa-byline"
          // Conditional: `absoluteTime(null)` is "—", which would assert a moment.
          sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}
        >
          {byline}
        </Typography></Tip>
      )}
      {delivery && (
        <Tip title={delivery.title}><Typography
          variant="caption"
          data-testid="qa-delivery"
          sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}
        >
          {delivery.text}
        </Typography></Tip>
      )}
    </Box>
  )
}

/* The one entry that is an ASK, not a record: the trailing legacy `awaiting_response_body` entry parsed
 * from prose (no `task_questions` row: `question_id` null, `status` `unresolved`). THE PAIR IS THE TEST: a
 * null id alone is also an old historical note, and an absent status alone is an older API pod's entry;
 * without a status, fall back on "did anything settle it". */
function unresolvedAsk(entry: QAEntry, round: TaskQuestion | undefined): boolean {
  if (round) return false
  if (entry.status) return entry.status === 'unresolved'
  return entry.resolution === null
}

/* Which entries a working surface shows: the settled ones. A filter, not a mode: the Q&A tab already
 * lists OPEN rounds above History, so a reopened entry would show twice. The trail off a gate does not call this.
 * Drops two shapes: a round the LIVE row says is `open`, and the unresolved ask. The live row decides where
 * present (so unknown settled words stay). Entries with no live row are kept (history by definition). */
export function settledEntries(entries: QAEntry[], questions: TaskQuestion[]): QAEntry[] {
  return entries.filter(entry => {
    const round = liveRound(entry, questions)
    if (round) return round.status !== 'open'
    return !unresolvedAsk(entry, round)
  })
}

/* The live row behind an entry, or undefined (no snapshotted `question_id`, or a round a Reset hid —
 * `questions` excludes those, and revise would 409). Undefined withholds Revise. Matched on id only. */
function liveRound(entry: QAEntry, questions: TaskQuestion[]): TaskQuestion | undefined {
  return typeof entry.question_id === 'number'
    ? questions.find(q => q.id === entry.question_id)
    : undefined
}
