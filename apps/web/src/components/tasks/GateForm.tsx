import { useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import Accordion from '@mui/material/Accordion'
import AccordionSummary from '@mui/material/AccordionSummary'
import AccordionDetails from '@mui/material/AccordionDetails'
import type { PipelineTask, QuestionOption, TaskQuestion } from '../../types'
import { countOf } from '../../lib/humanize'
import { answerPath } from './shared'
import { Banner } from '../../ui/Banner'
import { CopyButton } from '../../ui/CopyButton'
import SubmitButton from '../../ui/SubmitButton'
import RelativeTime from '../../ui/RelativeTime'
/* Rendering parts for agent-authored rounds, single-homed in `questionParts.tsx`. */
import {
  clampDetail,
  hasGlyph,
  nonBlank,
  optionDescription,
  optionDetail,
  OPTION_LABEL_SX,
  showsRecommended,
} from './questionParts'
import QATrail, { settledEntries } from './QATrail'
import AnswerAttachments from './AnswerAttachments'
import GateSteps, { type RoundSend } from './GateSteps'
import type { Attachment } from '../../types'
import { CodeBlock } from '../../ui/CodeBlock'
import { FIELD_SX, TEXTAREA_RESIZE_SX } from '../../ui/formDensity'
import { usePhone } from '../../hooks/usePhone'
import { PHONE } from '../../ui/listStyles'
import { FONT_WEIGHT, LETTER_SPACING, LINE_HEIGHT, RADIUS, SIZE, grid } from '../../theme'
import Tip from '../../ui/Tip'

const BUTTON_SX = {
  /* Narrower sides on a phone, so an action row's last control (Attach) keeps the row at 320px. */
  [PHONE]: { minHeight: SIZE.tapTarget, paddingLeft: 1.5, paddingRight: 1.5 },
  /* MUI gives a disabled button `pointer-events: none`; `auto` restores native hit-testability (still no
     click) so the reachability oracles' `elementFromPoint` probes of the docked mobile nav stay meaningful. */
  '&.Mui-disabled': { pointerEvents: 'auto' },
} as const

// An action row's tighter gap on a phone: with `BUTTON_SX`'s sides, Attach stays on the row.
const ACTION_ROW_PHONE_SX = { [PHONE]: { gap: 1 } } as const

/* The secondary acts (Comment only, Attach) as ONE item of the action row: the row wraps them as a group,
   so at 400px Attach no longer drops onto a line of its own. It wraps inside only once attached files'
   chips join it. */
const SECONDARY_ACTS_SX = { display: 'flex', flexWrap: 'wrap', gap: 1, maxWidth: '100%', ...ACTION_ROW_PHONE_SX } as const

// The one-line preview: the question without the `🛑 FLEET BLOCKED — … (re-affirmed)` marker boilerplate
// (the title already signals it). A marker-only line is skipped; the raw body stays behind `Show details`.
function questionPreview(body: string): string {
  for (const raw of body.split('\n')) {
    const line = raw.trim()
    if (!line) continue
    const cleaned = line
      .replace(/🛑/g, '')
      .replace(/fleet\s+blocked/i, '')
      .replace(/\(re-?affirmed\)/i, '')
      .replace(/^[\s—:-]+/, '')
      .trim()
    if (cleaned) return cleaned
  }
  return ''
}

/* The legacy single-shot gate's answer control (`task.awaiting_response_options`, bare labels): a button
   per option, or a free-text textarea. On failure the caller keeps it usable.

   PICKER: an EXTRA COMMENT box above the buttons rides along with whichever option is clicked, as its own
   field — the option is posted VERBATIM, since the server's delimiter-neutralization matches it exactly
   against the option list. `Comment only` posts the comment AS the answer (disabled until typed). The two
   differ by COLOUR: options primary (the default act), `Comment only` secondary purple. */
function AnswerControl({
  options,
  submitting,
  onSubmit,
  taskId,
}: {
  options: string[] | null | undefined
  submitting: boolean
  /* The comment is a separate argument: the server classifies the two halves separately. `attachmentIds`
     are the files attached for this answer (absent when none); with files, the text half may be EMPTY —
     the server records a file-only answer. */
  onSubmit: (answer: string, comment?: string, attachmentIds?: number[]) => void
  /* The task whose attachments the answer's files become. */
  taskId: number
}) {
  const [text, setText] = useState('')
  const [comment, setComment] = useState('')
  /* Files attached for THIS answer (uploaded already; `AnswerAttachments`), and whether one is uploading —
     every send control holds while it is, or the answer would leave without the file. */
  const [files, setFiles] = useState<Attachment[]>([])
  const [uploading, setUploading] = useState(false)
  const sending = submitting || uploading
  const phone = usePhone()
  /* Every send goes through here. The ids ride as a trailing argument ONLY when files are attached, so an
     answer without files reaches the host exactly as it always did. */
  const send = (...args: [answer: string, comment?: string]) => {
    if (files.length === 0) onSubmit(...args)
    else onSubmit(args[0], args[1], files.map(f => f.id))
  }
  const trimmedComment = comment.trim()
  /* `Comment only` is live with a comment OR files: a file-only answer is a real one (the server words it). */
  const canCommentOnly = !sending && (trimmedComment !== '' || files.length > 0)

  /* The Attach control, built once for both shapes; it joins each shape's action row rather than spending
     a row of its own under the box. */
  const attachControl = (
    <AnswerAttachments
      taskId={taskId}
      files={files}
      onChange={setFiles}
      onBusyChange={setUploading}
      disabled={submitting}
    />
  )

  if (options && options.length > 0) {
    return (
      // `column` is a REQUIREMENT (the box is filled before the click that sends it); the testid pins it,
      // since DOM order would survive `column-reverse`.
      <Box data-testid="gate-answer-controls" sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
        {/* ABOVE the buttons: every button submits on the click. Two rows: a qualification, not the answer. */}
        <TextField
          multiline
          minRows={2}
          value={comment}
          disabled={submitting}
          onChange={e => setComment(e.target.value)}
          placeholder="Extra comment (optional)"
          fullWidth
          slotProps={{ htmlInput: { 'aria-label': 'Extra comment' } }}
          sx={{
            ...FIELD_SX,
            '& .MuiInputBase-root': { alignItems: 'flex-start' },
            ...TEXTAREA_RESIZE_SX,
          }}
        />
        {/* `row` spelled out so the left-to-right order can be pinned (see above). */}
        <Box sx={{ display: 'flex', flexDirection: 'row', flexWrap: 'wrap', gap: 1, ...ACTION_ROW_PHONE_SX }}>
          {options.map((opt, i) => (
            // Long labels wrap and are bounded by the shared `OPTION_LABEL_SX` (R1); `title={opt}` is the
            // full value. The button grows (`height: auto`, `whiteSpace: normal`).
            <Tip key={i} title={opt}><Button
              variant="contained"
              color="primary"
              disabled={sending}
              onClick={() => send(opt, trimmedComment || undefined)}
              sx={{ ...BUTTON_SX, maxWidth: '100%', whiteSpace: 'normal', textAlign: 'left', height: 'auto' }}
            >
              <Box component="span" sx={OPTION_LABEL_SX}>{opt}</Box>
            </Button></Tip>
          ))}
          <Box data-testid="gate-secondary-acts" sx={SECONDARY_ACTS_SX}>
            <Button
              data-testid="gate-comment-only"
              /* Outlined on a phone: the options are the answer, and a second filled button there
                 out-shouted them in the one row a phone has. Outlined everywhere while disabled, as
                 Submit answer is: a disabled contained button is a filled grey slab. */
              variant={phone || (!canCommentOnly && !sending) ? 'outlined' : 'contained'}
              color="secondary"
              disabled={!canCommentOnly}
              onClick={() => send(trimmedComment)}
              sx={{ ...BUTTON_SX, flexShrink: 0 }}
            >
              Comment only
            </Button>
            {attachControl}
          </Box>
        </Box>
      </Box>
    )
  }
  const canSubmit = !uploading && (text.trim() !== '' || files.length > 0)
  return (
    <Box
      component="form"
      onSubmit={e => {
        e.preventDefault()
        if (sending) return
        if (text.trim() || files.length > 0) send(text.trim())
      }}
      sx={{ display: 'flex', flexDirection: 'column', gap: 1, alignItems: 'flex-start' }}
    >
      <TextField
        multiline
        minRows={3}
        value={text}
        disabled={submitting}
        onChange={e => setText(e.target.value)}
        placeholder="Type your answer…"
        fullWidth
        slotProps={{ htmlInput: { 'aria-label': 'Answer' } }}
        // DEC-3: the `grid(17)` floor on the TEXTAREA itself — `task-detail-fit.spec.ts` measures its own
        // `clientHeight` (a wrapper floor left 69px, under the 6rem criterion). `min-height` beats MUI
        // autosize's inline `height`; the box still grows and stays resizable.
        sx={{ ...FIELD_SX,
          '& .MuiInputBase-root': { alignItems: 'flex-start' },
          '& textarea[aria-label="Answer"]': { minHeight: grid(17) },
          ...TEXTAREA_RESIZE_SX,
        }}
      />
      {/* No comment box here: the textarea IS the operator's words. */}
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, ...ACTION_ROW_PHONE_SX }}>
        {/* Outlined while there is nothing to submit: a disabled CONTAINED button is a filled grey slab
            that reads as an input on a phone; disabled, it matches the task form's Save. */}
        <SubmitButton sx={{ ...BUTTON_SX, flexShrink: 0 }} busy={submitting}
          busyLabel="Submitting…" disabled={!canSubmit} variant={canSubmit || submitting ? 'contained' : 'outlined'}>
          Submit answer
        </SubmitButton>
        <Box data-testid="gate-secondary-acts" sx={SECONDARY_ACTS_SX}>
          {attachControl}
        </Box>
      </Box>
    </Box>
  )
}

/* ── THE MULTI-ROUND GATE ──────────────────────────────────────────────────────────────────────────
   The open rounds as pages (`GateSteps`, which owns every answer control) over the RESOLVED trail
   (`QATrail`). The history is a scrollport because round count is unbounded (a `maxHeight` clamp would
   hide rounds unreachably), and `e2e/task-detail-fit.spec.ts` forbids the answer panel scrolling the page. */

/* The history says `overscroll-behavior: auto` out loud: see PAGE_SX's "WHY NO SCROLLPORT ON THIS PAGE
   CONTAINS ITS OVERSCROLL" in `pages/taskDetail/taskDetailConstants.ts`, and `e2e/app-shell-scroll.spec.ts` fails on
   contain/none. At <=480px it stops being a scroll container ("ONE SCROLLPORT PER PHONE"): the panel is
   content-sized there. Declared here because this file owns the element. */
const SCROLLPORT_SX = {
  overflowY: 'auto',
  minHeight: 0,
  overscrollBehavior: 'auto',
} as const

/* The phone half, spread LAST in the list's `sx`: a spread-in `@media` key is emitted at its first
   insertion position, so a mid-spread media block would lose to the base values it overrides. */
const SCROLLPORT_PHONE_SX = { overflowY: 'visible' } as const

/* ── EVERY OPEN ROUND AS TEXT SOMEONE CAN PASTE INTO A CHAT ────────────────────────────────────────
   Options are buttons and radio/checkbox labels, so selecting their text picks them; the panel offers the
   transcript as an action instead. PLAIN TEXT (reads the same in any chat or email). Carries label,
   description and the Recommended mark — the description is what the decision turns on — but not the
   preview (a monospace mockup; named as present). OPEN rounds only.
   Per-option clamp parity with the screen (`clampDetail`, `OPTION_DETAIL_MAX_CHARS`), but NO whole-paste
   budget — unlike the Slack card's wire-limited `slack_blocks._OPTION_DETAIL_TOTAL_MAX`, a clipboard has
   no wall, and dropping a round would break the promise. Same option SET as the screen: multi-select
   drops blank-glyph options (`hasGlyph`) so numbering matches; single-choice does not filter. */

const COPY_OPTION_INDENT = '     '

/* Indent an agent-authored block so its newlines stay in the list; never re-wrapped (the client owns
   wrap). Blank lines stay blank (no trailing whitespace). */
function indentBlock(text: string, indent: string): string {
  return text
    .split('\n')
    .map(line => (line.trim() ? indent + line : ''))
    .join('\n')
}

/* One round: header, question, and its choice-set; a free-text round says so. */
function copyRoundText(
  round: number,
  header: string | null,
  question: string,
  options: string[] | null | undefined,
  details: QuestionOption[] | null | undefined,
  multiSelect: boolean,
): string {
  const name = nonBlank(header)
  const lines = [name ? `Round ${round} — ${name}` : `Round ${round}`, question.trim()]
  /* Rendered set paired with SOURCE index (as `GateSteps`'s `rows`), so no option gets its neighbour's
     detail. */
  const shown = (options ?? [])
    .map((opt, source) => ({ opt, source }))
    .filter(row => !multiSelect || hasGlyph(row.opt))
  if (shown.length === 0) {
    lines.push('Free-text answer — no options were offered.')
    return lines.join('\n')
  }
  lines.push(multiSelect ? 'Pick ONE OR MORE of:' : 'Pick ONE of:')
  shown.forEach(({ opt, source }, i) => {
    const detail = optionDetail(details, source)
    // `showsRecommended`, so a label already spelling "(Recommended)" is not marked twice.
    const mark = showsRecommended(opt, detail) ? '  (recommended)' : ''
    lines.push(`  ${i + 1}. ${opt}${mark}`)
    const description = optionDescription(opt, detail)
    if (description) lines.push(indentBlock(clampDetail(description.trim()), COPY_OPTION_INDENT))
    // The mockup is not pasted; saying it exists keeps the option from reading as poorly explained.
    if (nonBlank(detail?.preview)) {
      lines.push(`${COPY_OPTION_INDENT}[a mockup is attached to this option — see it in the console]`)
    }
  })
  return lines.join('\n')
}

/** The paste's header: which task, and where to answer it. */
function copyHeaderText(task: PipelineTask, url: string): string {
  const title = nonBlank(task.title)
  return [title ? `Task #${task.id} — ${title.trim()}` : `Task #${task.id}`, url].join('\n')
}

/* The whole paste for the multi-round gate. Exported for its test: the text is the contract. */
export function outstandingQuestionsText(task: PipelineTask, outstanding: TaskQuestion[], url: string): string {
  const count = outstanding.length
  const blocks = outstanding.map(q =>
    copyRoundText(q.round, q.header, q.question, q.options, q.option_details, q.multi_select),
  )
  return [
    copyHeaderText(task, url),
    '',
    `${countOf(count, 'open question')}, waiting on a decision:`,
    '',
    blocks.join('\n\n'),
    '',
  ].join('\n')
}

/* The same paste for the legacy single-shot gate (a cold-path task has no `task_questions` rows).
   Carries the WHOLE prose, not the one-line preview: the panel hides the writeup behind `Show details` to
   keep controls visible, but a chat recipient has no such control. Only the marker is
   stripped. `Round 1` keeps both shapes reading alike. */
export function singleQuestionText(task: PipelineTask, url: string): string {
  const question = coldPathQuestion(task) || '(no question text was recorded)'
  return [
    copyHeaderText(task, url),
    '',
    '1 open question, waiting on a decision:',
    '',
    copyRoundText(1, null, question, task.awaiting_response_options, null, false),
    '',
  ].join('\n')
}

/* The cold-path body as a question: marker stripped, trailing `**Options:**` block cut so choices are not
   printed twice. The cut happens only when `awaiting_response_options` is non-null — the server's own
   `task_awaiting_response_parser.parse_response_options` verdict — never a second client parse. */
const COLD_PATH_OPTIONS_SENTINEL = /^\*\*Options:\*\*[ \t]*$/
function coldPathQuestion(task: PipelineTask): string {
  const body = (task.awaiting_response_body ?? '').replace(/\r\n/g, '\n')
  if (!body.trim()) return ''
  const lines = body.split('\n')
  const sentinel = task.awaiting_response_options?.length
    ? lines.findIndex(line => COLD_PATH_OPTIONS_SENTINEL.test(line))
    : -1
  const prose = (sentinel >= 0 ? lines.slice(0, sentinel) : lines).join('\n')
  // Splice the marker-stripped first line back in; the rest keeps its shape.
  const first = prose.split('\n').findIndex(line => line.trim())
  if (first < 0) return ''
  const stripped = questionPreview(prose)
  const rest = prose
    .split('\n')
    .slice(first + 1)
    .join('\n')
    .trimEnd()
  return (stripped ? [stripped, rest].filter(Boolean).join('\n') : prose).trim()
}

/* Where to answer, absolute (the paste leaves this browser), via `answerPath` — the one spelling of
   "/tasks/<id>?tab=qa". Not `answerHref`, which answers `null` while an answer is DELIVERING (this
   panel still shows then). */
function answerUrl(task: PipelineTask): string {
  const origin = typeof window === 'undefined' ? '' : window.location.origin
  return `${origin}${answerPath(task.id)}`
}

/* COPY ALL, as an icon (one name on both gate shapes), at the right end of its row. Not withheld from a reader —
   forwarding the questions is a read. */
function GateCopyAll({ text }: { text: () => string }) {
  return <CopyButton testId="gate-copy-all" variant="icon" label="Copy all questions" title="Copy all questions" sx={{ ml: 'auto' }} text={text} />
}

/* Where an in-flight answer stands. Once the node has refused the `deliver_answer` intent, its latest reason
   (`answer_delivery_failure`, redacted server-side) replaces "Delivering…": the answer has NOT reached the runner,
   and the operator should see why (a node whose pane sensor is down refuses every delivery). */
function DeliveryBanner({ task }: { task: PipelineTask }) {
  const failure = task.answer_delivery_failure
  if (!failure) {
    return <Banner severity="info" testId="gate-delivering">Delivering your answer to the runner…</Banner>
  }
  // The count is the run of identical refusals (a new reason restarts it at 1), so a lone one goes unsaid.
  const retries = task.answer_delivery_retries ?? 1
  const refused = retries > 1 ? `refused it ${countOf(retries, 'time')} in a row` : 'refused it'
  return (
    <Banner severity="warning" testId="gate-delivering">
      Not delivered yet — the node {refused}: {failure.replace(/\.$/, '')}. Retrying.
    </Banner>
  )
}

// The gate-answer form for a Task pending a human decision (spec R5). Two shapes:
//   • Multi-round — the Task has `questions`: the OUTSTANDING rounds as pages (`GateSteps`), whose last
//     page's Send hands every round's answer or skip to `onSendRounds` at once, over the RESOLVED history
//     (`QATrail`). No answer control once nothing is outstanding.
//   • Legacy single-shot — otherwise: the `awaiting_response_body` preview + the
//     `awaiting_response_options` picker/free-text, via `onSubmit` (POST /tasks/{id}/answer).
// On failure the caller's error stays visible and the form stays usable.
export default function GateForm({
  task,
  submitting,
  delivering = false,
  error,
  onSubmit,
  onReviseQuestion,
  reviseError,
  readOnly = false,
  onSendRounds,
  trailLink,
  parkEvent,
}: {
  task: PipelineTask
  submitting: boolean
  /* Answer recorded, being handed to the live runner: a WARM delivery leaves the Task `awaiting_response`
     until the node acks (`_deliver_answer_to_live_runner`). Distinct from `submitting` (the request). The
     row's own server-derived `answer_delivering`; this form owns no clearing rule. */
  delivering?: boolean
  error: string | undefined
  // `comment` and `attachmentIds` are posted as their own fields (see `AnswerControl`).
  onSubmit: (answer: string, comment?: string, attachmentIds?: number[]) => void
  /* Reopen a SETTLED round (`POST /tasks/{id}/revise-question`), offered per entry by `QATrail`. Presence
     is the capability check; `readOnly` withholds it too. The Q&A trail off a gate offers it as well,
     since this form is gone once the last round settles. */
  onReviseQuestion?: (questionId: number) => void
  /* A failed Revise, apart from `error` (the host raises it from two tabs); rendered by `QATrail`. */
  reviseError?: string
  /* This form is the ONLY surface rendering what a gate asks, and asking is a `tasks:view` READ
     (`GET /api/tasks/{id}/questions`). `readOnly` withholds exactly the `tasks:edit` verbs: option
     controls, answer textarea, Submit, Revise. One prop for the set (one server fact); a prop, not
     `useCan()` here, so tests need no permission mock (as `TaskDependencies`/`TaskAttachmentsField`). */
  readOnly?: boolean
  /* The open rounds' answers and skips, handed over at once by the last page's Send (`GateSteps`). */
  onSendRounds: (sends: RoundSend[]) => void
  /* Where the task's recorded trail is (`to`, and the `label` the link reads), for the card a gate with no question
     text would otherwise leave without a next step. The host picks the trail (Q&A when there are entries,
     else the Timeline); one with no such tab (the console chat) passes none. */
  trailLink?: { to: string; label: string }
  /* The Timeline row that parked the gate (`at`, and the person who did, `actor`, null for a machine row), shown on that
     same empty card so the reader needs no second click. A host with no feed passes none. */
  parkEvent?: { at: string; actor: string | null }
}) {
  const questions = task.questions ?? []

  if (questions.length > 0) {
    /* One pass, no sort: the API returns rounds oldest-first and `round` is an ordinal over that order.
       Anything not `open` is history (the safe direction for a future status).
       `outstanding` stays oldest-first (a work queue); `QATrail` reverses the history to newest-first.
       The history IS the Q&A trail: `settledEntries` filters out entries whose round is open above. The
       open pages read the LIVE `task_questions` rows (what answers post against); the history reads the
       note-derived `qa_entries`, as the Q&A subtab does. They agree on the settled set. */
    const outstanding = questions.filter(q => q.status === 'open')
    const resolved = settledEntries(task.qa_entries ?? [], questions)
    return (
      /* The root FILLS the panel: `DetailSection fill` (`ui/detail`, the single home for the chain) gives a
         definite height, so `height: 100%` + `minHeight: 0` lets the pages and the history divide it instead
         of growing the page. At <=480px it resolves to auto and the shell scrolls. */
      <Box
        data-testid="gate-form"
        sx={{ height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0, gap: 1 }}
      >
        {outstanding.length > 0 ? (
          <GateSteps
            rounds={outstanding}
            readOnly={readOnly}
            submitting={submitting}
            taskId={task.id}
            onSend={onSendRounds}
            copyAll={
              <GateCopyAll
                text={() => outstandingQuestionsText(task, outstanding, answerUrl(task))}
              />
            }
          />
        ) : delivering ? null : (
          /* While an answer is in flight the banner below says where it stands; this line would claim it landed. */
          <Typography variant="caption" sx={{ flexShrink: 0, color: 'text.secondary', fontStyle: 'italic' }}>
            All rounds answered — waiting on the runner's next step.
          </Typography>
        )}

        {/* Directly under the answer area, where the operator just clicked (not below the history). Clears
            when the row's `answer_delivering` does — no timer. */}
        {delivering && (
          <Box sx={{ flexShrink: 0 }}>
            <DeliveryBanner task={task} />
          </Box>
        )}

        {/* No history, no box. */}
        {resolved.length > 0 && (
          <>
            <Typography
              variant="caption"
              sx={{
                flexShrink: 0,
                fontWeight: FONT_WEIGHT.semibold,
                color: 'text.secondary',
                textTransform: 'uppercase',
                letterSpacing: LETTER_SPACING.caps,
                borderTop: '1px solid',
                borderColor: 'divider',
                pt: 1,
              }}
            >
              History ({resolved.length})
            </Typography>
            {/* The scrollport only: `QATrail` renders its own `<ul>` (an `ol` around it would be invalid).
                Flex column so the child fills the port. */}
            <Box
              data-testid="gate-history-list"
              sx={{
                ...SCROLLPORT_SX,
                display: 'flex',
                flexDirection: 'column',
                /* The history YIELDS while rounds are outstanding (`0 1 auto`, capped at a third of the panel)
                   so a long trail cannot push open questions away; with nothing outstanding it fills. */
                flex: outstanding.length > 0 ? '0 1 auto' : '1 1 auto',
                maxHeight: outstanding.length > 0 ? '33%' : 'none',
                /* A FLOOR under the yield: proportional shrink left the history 24px beside 30 open rounds.
                   `grid(9)` ≈ two compact entries. Declared after the `SCROLLPORT_SX` spread to replace its
                   `minHeight: 0` with a bounded shrink. */
                minHeight: grid(9),
                /* The cap lifted outright at phone width — `33%` already computes to `none` there, but saying
                   so locally keeps it correct if the panel ever gains a definite height. */
                [PHONE]: { ...SCROLLPORT_PHONE_SX, maxHeight: 'none' },
              }}
            >
              <QATrail
                entries={resolved}
                questions={questions}
                submitting={submitting}
                error={reviseError ?? ''}
                /* Withheld from a reader (see `readOnly`). */
                onRevise={readOnly ? undefined : onReviseQuestion}
              />
            </Box>
          </>
        )}
        {error && (
          <Box sx={{ flexShrink: 0 }}>
            <Banner severity="error">{error}</Banner>
          </Box>
        )}
      </Box>
    )
  }

  // A one-line question preview by default, the raw blocker writeup behind `Show details`
  // (plain text, one click away).
  const body = task.awaiting_response_body
  const questionLine = body ? questionPreview(body) : ''
  return (
    <Box data-testid="gate-form" sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      {/* The question is the headline, `Copy all` at its right end (same name as multi-round). */}
      {body && (
        <>
          <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1 }}>
            {questionLine && (
              <Typography variant="body2" data-testid="gate-question-line" sx={{ fontWeight: FONT_WEIGHT.semibold, mb: 0.5, minWidth: 0 }}>
                {questionLine}
              </Typography>
            )}
            <GateCopyAll text={() => singleQuestionText(task, answerUrl(task))} />
          </Box>
          {/* Open by default for a READER: the writeup is collapsed for an editor because it pushed the CONTROLS
              off the panel, and a reader has none. The editor's default is unchanged. */}
          <Accordion
            disableGutters
            elevation={0}
            variant="outlined"
            defaultExpanded={readOnly}
            sx={{ mb: 1 }}
          >
            <AccordionSummary
              expandIcon="▾"
              sx={{
                // `auto` at desktop; at <=480px the 44px touch floor wins.
                minHeight: 'auto',
                [PHONE]: { minHeight: SIZE.tapTarget },
                color: 'text.secondary',
                '&.Mui-expanded': { minHeight: 'auto', [PHONE]: { minHeight: SIZE.tapTarget } },
                '& .MuiAccordionSummary-content': { my: 0.5, mx: 0 },
                '& .MuiAccordionSummary-content.Mui-expanded': { my: 0.5, mx: 0 },
              }}
            >
              <Typography variant="caption">Show details</Typography>
            </AccordionSummary>
            <AccordionDetails sx={{ p: 0 }}>
              {/* Raw body as plain text (never dangerouslySetInnerHTML: runner-authored, XSS), whitespace-preserving. */}
              <CodeBlock
                bordered
                maxHeight={grid(33)}
                sx={{
                  py: 1.5, px: 2, borderRadius: RADIUS.none, bgcolor: 'background.paper',
                  borderLeft: `${grid(0.5)} solid`, borderLeftColor: 'info.main', lineHeight: LINE_HEIGHT.normal, color: 'text.secondary',
                }}
              >
                {body}
              </CodeBlock>
            </AccordionDetails>
          </Accordion>
        </>
      )}
      {/* Read-only, options as text (the body is behind `Show details`). */}
      {readOnly ? (
        task.awaiting_response_options && task.awaiting_response_options.length > 0 ? (
          <Typography
            variant="caption"
            data-testid="gate-round-options"
            sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}
          >
            Options: {task.awaiting_response_options.join(' · ')}
          </Typography>
        ) : !body ? (
          /* Parked with no question and no rounds: say so rather than render an empty card, and point at the
             trail where whatever the run did record lives (`trailLink`). */
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
            {parkEvent && (
              <Typography variant="body2" data-testid="gate-park-event">
                {parkEvent.actor ? 'Parked' : 'Parked automatically'} <RelativeTime iso={parkEvent.at} />
                {parkEvent.actor && <> by {parkEvent.actor}</>}
              </Typography>
            )}
            <Typography variant="caption" data-testid="gate-no-question" sx={{ color: 'text.secondary', fontStyle: 'italic' }}>
              No question text was recorded for this gate.
              {trailLink && (
                <>
                  {' '}See{' '}
                  <Link component={RouterLink} to={trailLink.to} data-testid="gate-trail-link" sx={{ fontStyle: 'normal' }}>
                    {trailLink.label}
                  </Link>
                  .
                </>
              )}
            </Typography>
          </Box>
        ) : null
      ) : (
        <AnswerControl
          options={task.awaiting_response_options}
          submitting={submitting}
          taskId={task.id}
          onSubmit={onSubmit}
        />
      )}
      {/* The single-shot branch's copy of the delivering banner. */}
      {delivering && <DeliveryBanner task={task} />}
      {error && <Banner severity="error">{error}</Banner>}
    </Box>
  )
}
