import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import type { QAEntry, TaskQuestion } from '../../types'
import { absoluteTime, relativeAge } from '../../lib/relativeTime'
import { DISMISSED_OUTCOME_TEXT, resolvedByLine } from '../../lib/resolvedBy'
import { StatusChip } from '../../ui/StatusChip'
import { Banner } from '../../ui/Banner'
import {
  nonBlank,
  optionDescription,
  OptionDescription,
  OptionPreview,
  RecommendedBadge,
  RoundBatch,
  RoundHeaderChip,
  batchGroups,
  showsRecommended,
} from './questionParts'

/* Spec R6 — the persisted Q&A trail for a Task from `qa_entries` (oldest-first on the wire; shown NEWEST
   FIRST — see the reversal below). Each entry: ordinal and round name, status, question, options with
   their authors' descriptions, the resolution and the operator's comment — or "⏳ awaiting answer". All
   plain text (never dangerouslySetInnerHTML): agent-authored content.
   ONE TRAIL, TWO PLACEMENTS: the Activity › Q&A subtab and the Answer tab's `History`. It is
   the UNION of two former renderers; neither surface may grow a private field. The only difference is a
   filter on the ENTRIES: the Answer tab passes only settled ones (`GateForm`, via `settledEntries`); the
   subtab passes all.
   The option prose is here because this is the COLD PATH (`routes/tasks/answers.py::_record_answered_round`: "a
   header missing from it is a header lost forever on the cold path"); the runner prompt reads
   `entry.header`/`entry.option_details` for the other half.
   Low-accent register (caption type, inherited muted colour) marks a settled entry vs an OPEN round. */
export default function QATrail({ entries, questions, submitting, error, onRevise }: {
  entries: QAEntry[]
  /* The live rounds: an entry is a SNAPSHOT, and only the row knows whether that settlement still stands
     (a just-reopened round would otherwise show its old answer and a Revise that 409s). Matched on
     `QAEntry.question_id` (question text is not a key). Also the source of the ordinal (`round`), current
     status and `comment`; no live row → none of the three. */
  questions: TaskQuestion[]
  /* The page's ONE gate-mutation busy flag, shared with the answer form: Revise and answers act on the
     same rounds, and two flags would allow two mutations on one row. `GateForm`'s Ignore takes it too. */
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
     (`_clear_reopened_resolutions` strips it in place). The Answer tab lists open rounds OLDEST-FIRST — the
     RECORD vs the WORKING QUEUE, deliberately opposite. Do not "fix" one to match the other.
     Reversed at RENDER only: `_task_qa_entries` also feeds `_task_qa_trail` (the runner prompt), which must
     stay chronological. Keyed by the ORIGINAL index so new rounds do not re-key every row. */
  const newestFirst = entries.map((entry, i) => ({ entry, i })).reverse()
  /* One entry's block; `inBatch`: under a `RoundBatch` naming its header already. */
  const renderEntry = ({ entry, i }: { entry: QAEntry; i: number }, inBatch: boolean) => {
    /* The outcome from `status`, inferred from the resolution when absent: `status` is additive and an
       older API pod (rolling deploy) omits it, so older trails render as before (`unresolvedAsk`). */
    const dismissed = entry.status === 'dismissed'
    /* Blank is not an answer: one predicate decides both word and colour (`— not answered`). */
    const resolution = nonBlank(entry.resolution)
    const answered = !dismissed && resolution !== null
    const round = liveRound(entry, questions)
    /* Already reopened (here or in another tab): the snapshot is superseded; no second Revise. */
    const reopened = round?.status === 'open'
    const resolved = (answered || dismissed) && !reopened
    /* The status chip: the LIVE row's where present (only it knows about a reopen), else the snapshot's —
       never composed here, so unknown API values render verbatim in the neutral tone. */
    const status = round?.status || entry.status || null
    /* Who settled it and when, under the entry's own verb (`resolvedVerb`; a dismissal is
       not "Answered by"). Dropped on a reopened round. */
    /* Where the answer went: the CP hands every answer over at once, when no round is open.
       Unknown (an older API pod, no live row) says nothing. */
    const delivery = !resolved || round?.delivered_at === undefined
      ? null
      : round.delivered_at
        ? { text: `Sent to the runner · ${relativeAge(round.delivered_at)}`, title: absoluteTime(round.delivered_at) }
        : questions.some(q => q.status === 'open')
          ? { text: 'Held — delivered when all rounds are answered', title: undefined }
          : null
    const byline = resolved
      ? resolvedByLine({
        by: entry.resolved_by,
        at: entry.resolved_at,
        // `|| 'answered'` for the rolling-deploy reason above.
        status: entry.status || 'answered',
      })
      : null
    /* The outcome, five claims:
         · reopened → superseded; a new answer is owed (amber, like open);
         · dismissed → the shared sentence (`DISMISSED_OUTCOME_TEXT`), muted — nothing was decided;
         · answered → the resolution, settled green;
         · still ASKED → `⏳ awaiting answer` (the trailing entry a parked task adds);
         · anything else → `— not answered`, muted (an unknown settled word, or a blank resolution;
           the chip names the word).
       `Answered — ` is the emphasised label, not the value (a multi-select resolution joins every label
       and would out-shout open questions). Never truncated; it wraps. */
    const outcome = reopened
      ? { tone: 'warning.main', text: '↩ Reopened — awaiting a new answer' }
      : dismissed
        ? { tone: 'text.secondary', text: DISMISSED_OUTCOME_TEXT }
        : answered
          ? {
            tone: 'success.main',
            text: (
              <>
                {'Answered — '}
                <Box component="span" sx={{ fontWeight: 400 }}>{resolution}</Box>
              </>
            ),
          }
          : unresolvedAsk(entry, round)
            ? { tone: 'warning.main', text: '⏳ awaiting answer' }
            : { tone: 'text.secondary', text: '— not answered' }
    return (
      <Box
        component="li"
        key={i}
        data-testid="qa-entry"
        sx={{
          px: 1.05,
          py: 0.85,
          bgcolor: 'background.paper',
          border: 1,
          borderColor: 'divider',
          borderLeft: 3,
          borderLeftColor: 'secondary.main',
          borderRadius: 1,
          display: 'flex',
          flexDirection: 'column',
          gap: 0.65,
        }}
      >
        {/* The round's ordinal (from the LIVE row; what an answer routes on) and its own header
            (`TaskQuestion.header`) above the question — the only way to tell a batch's entries apart.
            The chip is `questionParts`' neutral one (`ROUND_HEADER_SX`). Guarded on having content (an
            empty wrapper would still take the column `gap`); `display: flex` so parts shrink to text. */}
        {(round || nonBlank(entry.header) || status) && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
            {round && (
              <Typography
                variant="caption"
                data-testid="qa-round"
                sx={{ fontWeight: 600, color: 'text.secondary' }}
              >
                Round {round.round}
              </Typography>
            )}
            {/* A round under a `RoundBatch` takes the batch's one label. */}
            {!inBatch && <RoundHeaderChip header={entry.header} />}
            {/* The round's own status, never translated: green = decision landed, amber = one owed,
                anything else neutral (as `StatusChip` does). */}
            {status && (
              <StatusChip
                label={status}
                tone={status === 'answered' ? 'success' : status === 'open' ? 'warning' : 'neutral'}
              />
            )}
          </Box>
        )}
        {entry.question && (
          // The question is raw multi-line 🛑 FLEET BLOCKED markdown — preserve its newlines so
          // the "**Options:**" lines do not collapse onto one.
          <Typography
            variant="body2"
            sx={{ fontWeight: 600, color: 'text.primary', overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}
          >
            {entry.question}
          </Typography>
        )}
        {entry.options.length > 0 && (
          <Box
            component="ul"
            data-testid="qa-options"
            sx={{ m: 0, pl: '1.125rem', display: 'flex', flexDirection: 'column', gap: 0.2 }}
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
                /* A `list-item`, not flex (a flex `li` loses the markers tying lines to the question's
                   "**Options:**"). The detail parts are block boxes. The muted colour is declared here
                   once: `OptionDescription` is `color: inherit` by design. */
                <Typography
                  component="li"
                  key={j}
                  variant="caption"
                  sx={{ color: 'text.secondary', overflowWrap: 'anywhere', minWidth: 0 }}
                >
                  {opt}
                  {/* Which option the runner recommended, as a small tag on its line (what the operator
                      was steered toward). `showsRecommended` skips labels already saying `(Recommended)`. */}
                  {showsRecommended(opt, detail) && (
                    /* The spacer is load-bearing: this `li` has no flex `gap`, so the badge sat against the
                       label ("child editRECOMMENDED", measured). A text space also lets it wrap whole. */
                    <>{' '}<RecommendedBadge /></>
                  )}
                  {description && <OptionDescription text={description} />}
                  {preview && <OptionPreview text={preview} />}
                </Typography>
              )
            })}
          </Box>
        )}
        <Typography
          variant="caption"
          data-testid="qa-resolution"
          data-resolved={resolved ? 'true' : 'false'}
          /* One predicate (`outcome`) decides word and colour. */
          sx={{ color: outcome.tone, overflowWrap: 'anywhere', fontWeight: 600 }}
        >
          {outcome.text}
        </Typography>
        {/* The operator's comment on its own line, read off the LIVE row (the projection never carried
            it). Chosen option and the human's remark are separate facts. Dropped on a reopened round. */}
        {!reopened && nonBlank(round?.comment) && (
          <Typography
            variant="caption"
            data-testid="qa-comment"
            sx={{ overflowWrap: 'anywhere', whiteSpace: 'pre-wrap', color: 'text.secondary' }}
          >
            Comment: {round?.comment}
          </Typography>
        )}
        {/* Attribution last and quietest; absent when neither author nor time is recorded. The exact
            moment rides `title`. */}
        {byline && (
          <Typography
            variant="caption"
            data-testid="qa-byline"
            // Conditional: `absoluteTime(null)` is "—", which would assert a moment.
            title={entry.resolved_at ? absoluteTime(entry.resolved_at) : undefined}
            sx={{ color: 'text.disabled', overflowWrap: 'anywhere' }}
          >
            {byline}
          </Typography>
        )}
        {delivery && (
          <Typography
            variant="caption"
            data-testid="qa-delivery"
            title={delivery.title}
            sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}
          >
            {delivery.text}
          </Typography>
        )}
        {/* REVISE — the one write on an audit surface: an answer need not be final. On BOTH surfaces:
            the Answer tab is where a mind changes; the Q&A subtab survives the gate
            (`GateForm` disappears once all rounds settle).
            Withheld when: `onRevise` absent (no permission, or a chat — as `GateForm`'s `readOnly`);
            `round` undefined (no live row; the server would 409, never guess); `reopened` (already open).
            One click, no confirm (the operator's call); `disabled={submitting}` is the only brake. */}
        {onRevise && round && !reopened && (
          <Box sx={{ display: 'flex' }}>
            <Button
              data-testid="qa-revise"
              size="small"
              variant="outlined"
              color="secondary"
              disabled={submitting}
              /* Named by its round (six "Revise" buttons are indistinguishable — as `GateForm`'s Ignore):
                 the header, else the question clipped to 80 chars. The visible label stays bare. */
              aria-label={`Revise answer to ${nonBlank(entry.header) || (entry.question ?? '').slice(0, 80) || 'this question'}`}
              /* Promises no delivery: with no live runner the record is corrected and nobody is told. */
              title="Reopens this round. The answer moves to the activity log."
              onClick={() => onRevise(round.id)}
              sx={{ textTransform: 'none', flexShrink: 0 }}
            >
              Revise
            </Button>
          </Box>
        )}
      </Box>
    )
  }
  return (
    /* No heading: the host owns it (the subtab is the tab; `GateForm` has `History (n)`). */
    <Box data-testid="qa-trail" sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      {error && <Banner severity="error" testId="qa-revise-error">{error}</Banner>}
      <Box component="ul" sx={{ listStyle: 'none', m: 0, p: 0, display: 'flex', flexDirection: 'column', gap: 1.35 }}>
        {/* Rounds from one `AskUserQuestion` call sharing a header sit under that header once (`batchGroups`);
            judged on the LIVE rows, so an entry with none stands alone. */}
        {batchGroups(newestFirst, ({ entry }) => liveRound(entry, questions)).map(group => (group.length > 1
          ? (
            <RoundBatch key={`batch-${group[0].i}`} header={group[0].entry.header} gap={1.35} testId="qa-batch">
              {group.map(item => renderEntry(item, true))}
            </RoundBatch>
          )
          : renderEntry(group[0], false)))}
      </Box>
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

/* Which entries a working surface shows: the settled ones. A filter, not a mode: the Answer tab already
 * lists OPEN rounds above History, so a reopened entry would show twice. The Q&A subtab does not call this.
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
