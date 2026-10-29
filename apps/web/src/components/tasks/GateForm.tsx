import { useId, useRef, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
import Link from '@mui/material/Link'
import FormControlLabel from '@mui/material/FormControlLabel'
import FormGroup from '@mui/material/FormGroup'
// `alpha` for the ticked row's accent tint — see the row's own `sx`.
import { alpha } from '@mui/material/styles'
import type { Theme } from '@mui/material/styles'
import type { SystemStyleObject } from '@mui/system'
import TextField from '@mui/material/TextField'
import useMediaQuery from '@mui/material/useMediaQuery'
import Typography from '@mui/material/Typography'
import Accordion from '@mui/material/Accordion'
import AccordionSummary from '@mui/material/AccordionSummary'
import AccordionDetails from '@mui/material/AccordionDetails'
import type { PipelineTask, QuestionOption, QuestionPlainOption, TaskQuestion } from '../../types'
import { answerPath, taskLabel, taskStatus } from './shared'
import { taskRoleLabel } from './personaRegistry'
import { StatusChip } from '../../ui/StatusChip'
import StatusDot from '../../ui/StatusDot'
import { Banner } from '../../ui/Banner'
import { CopyButton } from '../../ui/CopyButton'
import SubmitButton from '../../ui/SubmitButton'
import RelativeTime from '../../ui/RelativeTime'
/* Rendering parts for agent-authored rounds, single-homed in `questionParts.tsx`. */
import {
  clampDetail,
  hasGlyph,
  MAX_SELECTIONS,
  LABEL_LINE_SX,
  nonBlank,
  optionDescription,
  optionDetail,
  OptionDescription,
  OptionPreview,
  OPTION_LABEL_SX,
  RecommendedBadge,
  RoundBatch,
  RoundHeaderChip,
  batchGroups,
  showsRecommended,
} from './questionParts'
import QATrail, { settledEntries } from './QATrail'
import AnswerAttachments from './AnswerAttachments'
import GateSteps, { pageWords, type RoundSend } from './GateSteps'
import type { Attachment } from '../../types'
import { CodeBlock } from '../../ui/CodeBlock'
import { FIELD_SX, TEXTAREA_RESIZE_SX } from '../../ui/formDensity'
import { PHONE, PHONE_QUERY } from '../../ui/listStyles'

const BUTTON_SX = {
  /* Narrower sides on a phone, so an action row's last control (Attach) keeps the row at 320px. */
  '@media (max-width:480px)': { minHeight: 44, paddingLeft: '10px', paddingRight: '10px' },
  /* MUI gives a disabled button `pointer-events: none`; `auto` restores native hit-testability (still no
     click) so the reachability oracles' `elementFromPoint` probes of the docked mobile nav stay meaningful. */
  '&.Mui-disabled': { pointerEvents: 'auto' },
} as const

// An action row's tighter gap on a phone: with `BUTTON_SX`'s sides, Attach stays on the row.
const ACTION_ROW_PHONE_SX = { [PHONE]: { gap: 1 } } as const

/* The round's secondary acts (Comment only, Ignore, Attach) as ONE item of the action row: the row
   wraps them as a group, so at 400px Attach no longer drops onto a line of its own. It wraps inside only once
   attached files' chips join it. */
const SECONDARY_ACTS_SX = { display: 'flex', flexWrap: 'wrap', gap: 1.33, maxWidth: '100%', ...ACTION_ROW_PHONE_SX } as const

/* An answer: TEXT (an option label verbatim, or prose) or a multi-choice round's TICK SET. A union, not a
   fourth argument: the route takes `answer` XOR `selections` (422 on both). Structural, so existing call
   sites read unchanged. The EMPTY ARRAY IS A VALUE ("none apply", recorded with a sentinel resolution —
   not Ignore), so every hop branches on TYPE, never length. */
export type GateAnswer = string | { selections: string[] }

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

/* The answer control for the open question: a button-per-option picker, a checkbox list, or a free-text
   textarea. Shared by the legacy single-shot gate (`task.awaiting_response_options`) and the multi-round
   gate. On failure the caller keeps it usable.

   PICKER: an EXTRA COMMENT box above the buttons rides along with whichever option is clicked, as its own
   field — the option is posted VERBATIM, since the server's delimiter-neutralization matches it exactly
   against the option list. `Comment only` posts the comment AS the answer (disabled until typed).
   `Ignore` sits in the same row; the three differ by COLOUR: options primary (the default act),
   `Comment only` secondary purple, `Ignore` outlined warning amber (lowest emphasis, a different kind).

   MULTI-CHOICE (`multiSelect`): an instant-submit button cannot express "A and C", so a CHECKBOX LIST
   plus one Submit, posting `{ selections }`.
     · Submit is always live: ZERO TICKS is a valid answer; the button's label counts ticks and says what
       zero will record.
     · No `Comment only`: that sentence is zero ticks + a comment (`selections: []`).
     · The comment box sits BELOW the list (options, then qualification, then act). */
function AnswerControl({
  options,
  optionDetails,
  plainOptions,
  multiSelect,
  submitting,
  onSubmit,
  describedById,
  onIgnore,
  roundLabel,
  taskId,
}: {
  options: string[] | null | undefined
  /* What each option means (`QuestionOption` in types.ts; `OPTION_DESCRIPTION_SX`). Optional and absent
     by default — unlike `multiSelect` — because a round may simply not carry details (free-text, older
     rounds, the legacy gate). INDEX-ALIGNED with `options`, never matched by label (labels may repeat);
     read only via `optionDetail(…, source)` with the RAW index. */
  optionDetails?: QuestionOption[] | null
  /* The reworded options (`TaskQuestion.plain.options`), INDEX-ALIGNED with `options` like `optionDetails`.
     Display only: a `short` replaces the visible label, a `gist` stands in for a missing description; the
     posted answer is always the raw label. Absent = the runner's own words. */
  plainOptions?: QuestionPlainOption[] | null
  /* The round's `AskUserQuestion` `multiSelect`. REQUIRED, not defaulted: the API always sends it
     (`TaskQuestion.multi_select`), and a default would silently render multi-choice rounds as single. The
     legacy gate says `false` out loud. */
  multiSelect: boolean
  submitting: boolean
  /* The comment is a separate argument: the server classifies the two halves separately. With
     `multiSelect` false only a string is emitted. `attachmentIds` are the files attached for this answer
     (absent when none); with files, the text half may be EMPTY — the server records a file-only answer. */
  onSubmit: (answer: GateAnswer, comment?: string, attachmentIds?: number[]) => void
  /* Id of the element naming WHICH question this answers (undefined on the single-shot gate). A
     DESCRIPTION, not the name: the name must stay the literal "Answer" (`e2e/task-detail-fit.spec.ts`
     pins `textarea[aria-label="Answer"]`), and group names are announced inconsistently, while
     `aria-describedby` is always read. Also on the option buttons ("Skip" ×3 rounds). */
  describedById?: string
  /* Dismiss without answering. Absent = no dismissal endpoint (legacy gate), so no button at all. */
  onIgnore?: () => void
  /* E.g. `round 3`: ONE prop naming every added control in its accessible name only (visible labels stay
     bare). Absent on the legacy gate. */
  roundLabel?: string
  /* The task whose attachments the answer's files become. Absent = no Attach control. */
  taskId?: number
}) {
  const [text, setText] = useState('')
  const [comment, setComment] = useState('')
  /* Files attached for THIS answer (uploaded already; `AnswerAttachments`), and whether one is uploading —
     every send control holds while it is, or the answer would leave without the file. */
  const [files, setFiles] = useState<Attachment[]>([])
  const [uploading, setUploading] = useState(false)
  const sending = submitting || uploading
  const phone = useMediaQuery(PHONE_QUERY)
  /* Every send goes through here. The ids ride as a trailing argument ONLY when files are attached, so an
     answer without files reaches the host exactly as it always did. */
  const send = (...args: [answer: GateAnswer, comment?: string]) => {
    if (files.length === 0) onSubmit(...args)
    else onSubmit(args[0], args[1], files.map(f => f.id))
  }
  /* The tick set — per-round by construction (mounted inside each `OutstandingRound`, keyed by `q.id`);
     hoisting it would recreate the one-shared-control defect of posting the wrong round's id. Keyed by
     INDEX, not label (two identical labels must be two selections), into the RENDERED list `visible`, not
     the raw `options` (blank-glyph options are filtered out; a raw index would post an unclicked label). */
  const [ticked, setTicked] = useState<number[]>([])
  /* Names the cap caption for the option group's `aria-describedby`. Not `aria-live`: it changes every tick. */
  const capId = useId()
  /* Id prefix for per-option description/badge ids. Detail is a DESCRIPTION, never part of the name (a
     run-on name has no seam between "which option" and "what it means"). Suffixed by SOURCE index (repeat
     labels get distinct ids); `useId` since one instance mounts per open round. */
  const optionIdBase = useId()
  const trimmedComment = comment.trim()
  /* `Comment only` is live with a comment OR files: a file-only answer is a real one (the server words it). */
  const canCommentOnly = !sending && (trimmedComment !== '' || files.length > 0)
  /* A NEEDS-INPUT OPTION: `QuestionOption.needs_comment` marks an option whose answer is the typed
     value ("Change integration branch" — the checkout gate reads the branch off the comment). Clicked with the
     box empty it sends nothing: the box takes focus and this line says what to type. Set by that click only;
     a send replaces the round, so nothing clears it. */
  const [commentPrompt, setCommentPrompt] = useState<string | null>(null)
  const commentInputRef = useRef<HTMLTextAreaElement>(null)
  // MUI names the helper `${id}-helper-text`; the input's own `aria-describedby` (set below) must list it.
  const commentId = useId()

  /* One option's detail, resolved once for both branches (SOURCE index; see `optionDetail`). `describedBy`
     APPENDS to `describedById` (an id list: question, then recommendation, then explanation); no detail
     composes back to the bare `describedById`. */
  const detailParts = (label: string, source: number) => {
    const detail = optionDetail(optionDetails, source)
    const plain = plainOptions?.[source]
    const shown = nonBlank(plain?.short) ?? label
    /* The runner's own description wins; the gist only fills an option that has none. */
    const description = optionDescription(label, detail)
      ?? optionDescription(shown, { label: shown, description: plain?.gist ?? null, preview: null, recommended: false })
    const preview = nonBlank(detail?.preview)
    const recommended = showsRecommended(shown, detail)
    const descriptionId = description ? `${optionIdBase}-desc-${source}` : undefined
    const recommendedId = recommended ? `${optionIdBase}-rec-${source}` : undefined
    return {
      shown,
      description,
      preview,
      recommended,
      descriptionId,
      recommendedId,
      describedBy: [describedById, recommendedId, descriptionId].filter(Boolean).join(' ') || undefined,
      /* Whether the control gains anything; false renders the bare label with no wrapper, span or
         `aria-label`. The preview is excluded on the single-choice branch (it sits outside the button). */
      hasInlineDetail: Boolean(description || recommended),
    }
  }
  /* Built once, placed by each branch. The tooltip names what is discarded: the comment box (picker) or the
     typed answer (free-text). */
  const ignoreButton = onIgnore ? (
    <Button
      data-testid="gate-ignore"
      variant="outlined"
      color="warning"
      disabled={submitting}
      aria-label={roundLabel ? `Ignore ${roundLabel}` : 'Ignore'}
      aria-describedby={describedById}
      /* Typed text is NOT sent (a dismissed round records no answer). */
      title={`Skips this round without answering. Typed text${files.length > 0 ? ' and files' : ''} not sent.`}
      onClick={onIgnore}
      sx={{ ...BUTTON_SX, flexShrink: 0 }}
    >
      Ignore
    </Button>
  ) : null

  /* The extra-comment box, built once for both option branches (placement: see the header). Two rows: a
     qualification, not the answer. The placeholder differs because it names the gesture that sends it. */
  const commentBox = (
    <TextField
      multiline
      minRows={2}
      value={comment}
      disabled={submitting}
      onChange={e => { setComment(e.target.value); setCommentPrompt(null) }}
      // While prompting, the placeholder names the need too (the caption below says what to type).
      placeholder={commentPrompt ? 'Type the value here' : 'Extra comment (optional)'}
      id={commentId}
      inputRef={commentInputRef}
      helperText={commentPrompt ?? undefined}
      fullWidth
      slotProps={{
        formHelperText: { role: 'status' },
        htmlInput: {
          'aria-label': roundLabel ? `Extra comment for ${roundLabel}` : 'Extra comment',
          'aria-describedby': [describedById, commentPrompt ? `${commentId}-helper-text` : null]
            .filter(Boolean).join(' ') || undefined,
        },
      }}
      sx={{
        ...FIELD_SX,
        '& .MuiInputBase-root': { alignItems: 'flex-start' },
        ...TEXTAREA_RESIZE_SX,
      }}
    />
  )

  /* The Attach control, built once for every shape; it joins each shape's action row (after Ignore) rather
     than spending a row of its own under the box. */
  const attachControl = taskId === undefined ? null : (
    <AnswerAttachments
      taskId={taskId}
      files={files}
      onChange={setFiles}
      onBusyChange={setUploading}
      disabled={submitting}
      roundLabel={roundLabel}
    />
  )
  /* The Ignore consequence once files are attached: they stay on the task, but this answer will not send them. */
  const ignoreCaption = files.length > 0
    ? 'Ignore sends neither the comment nor the files.'
    : null

  /* ── THE MULTI-CHOICE ROUND ────────────────────────────────────────────────────────────────────── */
  if (options && options.length > 0 && multiSelect) {
    /* Ticked indices become posted LABELS here, the only crossing: `selections` of labels is the API
       contract (validated against the round's stored options). A repeated label ticked twice posts twice.
       Posted in the round's own OPTION ORDER, so the recorded `chosen_resolution` reads as seen and the
       body depends on the tick SET alone.
       The rendered set drops blank-glyph options (`hasGlyph`) — the same set the Slack card renders and the
       route accepts. Filtered here, not in the branch predicate: an all-blank round is still multi-choice
       and still has the zero-tick answer.
       Each row carries its SOURCE index (the `optionDetail` key) on the row itself, not a parallel array; a
       tick indexes the rendered list. The two differ whenever a blank option was dropped. */
    const visible = options.map((opt, source) => ({ opt, source })).filter(row => hasGlyph(row.opt))
    const selections = visible.filter((_, i) => ticked.includes(i)).map(row => row.opt)
    /* At the cap (`MAX_SELECTIONS`), measured on the posted array the route bounds. */
    const atCap = selections.length >= MAX_SELECTIONS
    /* The cap is shown only on rounds that can reach it, from the first tick. */
    const capReachable = visible.length > MAX_SELECTIONS
    /* The bound is enforced here, where the set grows; `disabled` below only shows it. Untick is never
       refused — at the cap it is the only move left. */
    const toggle = (i: number) => setTicked(prev => {
      if (prev.includes(i)) return prev.filter(n => n !== i)
      return prev.length >= MAX_SELECTIONS ? prev : [...prev, i]
    })
    /* The button says what it will do: it counts ticks, and at zero names the answer being recorded.
       Singular/plural spelled out. */
    const submitLabel = selections.length === 0
      ? 'Submit — none of these'
      : `Submit ${selections.length} option${selections.length === 1 ? '' : 's'}`
    return (
      <Box
        data-testid="gate-answer-controls"
        data-answer-shape="multi-select"
        sx={{ display: 'flex', flexDirection: 'column', gap: 1.33 }}
      >
        {/* Says "multi-choice" in words: operators used to one-click buttons would tick one box and wait.
            The second wording covers a round whose options were all blank-glyph (the zero-tick answer
            remains). */}
        <Typography variant="caption" data-testid="gate-multi-hint" sx={{ color: 'text.secondary' }}>
          {visible.length > 0
            ? 'Tick all that apply — more than one is allowed.'
            : 'No options — nothing to tick.'}
        </Typography>
        {/* A GROUP (not a list — the rounds are the lists), named per round (`roundLabel`). Rendered only with
            members; an empty named group describes a choice that does not exist. */}
        {visible.length > 0 && (
        <FormGroup
          role="group"
          aria-label={roundLabel ? `Options for ${roundLabel}` : 'Options'}
          // The cap bounds the GROUP; referenced only when reachable (else the id resolves to nothing).
          aria-describedby={capReachable ? capId : undefined}
          data-testid="gate-multi-options"
          sx={{ gap: 0.5 }}
        >
          {visible.map(({ opt, source }, i) => {
            // By index (see `ticked`).
            const checked = ticked.includes(i)
            // By SOURCE for the detail (`option_details`).
            const detail = detailParts(opt, source)
            /* Cap-locked: unticked rows cannot tick but stay readable — prevented at the gesture, not as a
               lost payload. A ticked row is never locked (see `toggle`). */
            const lockedOut = atCap && !checked
            return (
              /* The WHOLE ROW carries the ticked state (accent border + 12% fill of `primary.main`, one
                 token so the halves cannot drift); the checkmark confirms. `data-checked` lets a test read it. */
              <FormControlLabel
                key={i}
                data-testid="gate-multi-option"
                data-checked={checked ? 'true' : 'false'}
                // `data-locked` likewise, readable on the row.
                data-locked={lockedOut ? 'true' : 'false'}
                title={opt}
                /* The ROW owns `disabled`: `FormControlLabel` clones `disabledProp ?? control.props.disabled`
                   onto the checkbox, so a row saying `false` would override the box's `true`. Native
                   `disabled` (AT announces it; the tick cannot dispatch). */
                disabled={submitting || lockedOut}
                control={
                  <Checkbox
                    checked={checked}
                    onChange={() => toggle(i)}
                    /* The touch floor as a literal at <=480px (like `BUTTON_SX`/`FIELD_SX`), not
                       `theme.custom.tapTargetPx`: that is absent from MUI's default theme, which this
                       suite's bare `render()` calls use. */
                    sx={{ '@media (max-width:480px)': { minWidth: 44, minHeight: 44 } }}
                    /* The option label is the accessible name; the round rides `aria-describedby`. */
                    slotProps={{ input: { 'aria-label': detail.shown, 'aria-describedby': detail.describedBy } }}
                  />
                }
                /* The WHOLE option (name, badge, description, preview) sits in the label slot so the row is ONE
                   hit target (`FormControlLabel` forwards clicks). The preview opts out itself
                   (`OptionPreview`). Nothing to add = the bare label node. */
                label={
                  // Bounded and `title`-backed by `OPTION_LABEL_SX`, shared with the buttons.
                  detail.hasInlineDetail || detail.preview ? (
                    <Box component="span" sx={{ display: 'block', minWidth: 0 }}>
                      <Box component="span" sx={LABEL_LINE_SX}>
                        <Box component="span" sx={OPTION_LABEL_SX}>{detail.shown}</Box>
                        {detail.recommended && <RecommendedBadge id={detail.recommendedId} />}
                      </Box>
                      {detail.description && (
                        <OptionDescription text={detail.description} id={detail.descriptionId} />
                      )}
                      {detail.preview && <OptionPreview text={detail.preview} />}
                    </Box>
                  ) : (
                    <Box component="span" sx={OPTION_LABEL_SX}>{detail.shown}</Box>
                  )
                }
                sx={theme => ({
                  // Undo MUI's negative start margin, which would pull the border out of the padding.
                  ml: 0,
                  mr: 0,
                  // Top-aligned: the box sits beside the label's first line.
                  alignItems: 'flex-start',
                  pr: 1,
                  py: 0.25,
                  borderRadius: 1,
                  border: '1px solid',
                  borderColor: checked ? 'primary.main' : 'divider',
                  /* The accent, not `action.selected` (a grey wash that reads as hover/disabled, and vanishes
                     in dark). Derived from the palette. */
                  backgroundColor: checked ? alpha(theme.palette.primary.main, 0.12) : 'transparent',
                  // The whole row is the hit target.
                  cursor: submitting ? 'default' : 'pointer',
                  // No hover lift on a locked row.
                  '&:hover': { borderColor: checked ? 'primary.main' : lockedOut ? 'divider' : 'text.disabled' },
                  /* A CAP-locked row (not the in-flight freeze) must not inherit two `Mui-disabled` traits:
                       · the greyed label — the options are being re-read to choose what to untick; three
                         classes deep to match MUI's specificity;
                       · the `default` cursor — `not-allowed` says so before the click. */
                  ...(lockedOut && !submitting ? {
                    '&.Mui-disabled': { cursor: 'not-allowed' },
                    '& .MuiFormControlLabel-label.Mui-disabled': { color: 'text.primary' },
                  } : {}),
                  '@media (max-width:480px)': { minHeight: 44 },
                })}
              />
            )
          })}
        </FormGroup>
        )}
        {/* The cap in words, under the list: the number AND the count (at the wall the answer is a trade).
            `warning.main` only at the cap. */}
        {capReachable && (
          <Typography
            id={capId}
            variant="caption"
            data-testid="gate-multi-cap"
            sx={{ color: atCap ? 'warning.main' : 'text.secondary' }}
          >
            {atCap
              ? `${selections.length} of ${MAX_SELECTIONS} ticked. Untick one to change.`
              : `${selections.length} of ${MAX_SELECTIONS} ticked`}
          </Typography>
        )}
        {commentBox}
        {/* `row` spelled out: `row-reverse` would flip the paint with DOM order unchanged. */}
        <Box sx={{ display: 'flex', flexDirection: 'row', flexWrap: 'wrap', gap: 1.33, ...ACTION_ROW_PHONE_SX }}>
          <Button
            data-testid="gate-multi-submit"
            variant="contained"
            color="primary"
            /* ENABLED AT ZERO TICKS: an empty selection is a real answer; only in-flight disables it. */
            disabled={sending}
            // Per round, containing the visible label (WCAG 2.5.3).
            aria-label={roundLabel ? `${submitLabel} for ${roundLabel}` : undefined}
            aria-describedby={describedById}
            onClick={() => send({ selections }, trimmedComment || undefined)}
            sx={{ ...BUTTON_SX, flexShrink: 0 }}
          >
            {submitting ? 'Submitting…' : submitLabel}
          </Button>
          <Box data-testid="gate-secondary-acts" sx={SECONDARY_ACTS_SX}>
            {ignoreButton}
            {attachControl}
          </Box>
        </Box>
        {/* Two captions, each silent until it has a subject: the zero-tick meaning in words (no hover on a
            phone; names Ignore as the other act), and the discard warning without `Comment only`. */}
        {selections.length === 0 && (
          <Typography variant="caption" data-testid="gate-multi-none" sx={{ color: 'text.secondary' }}>
            Nothing ticked: submitting answers “none apply”. Ignore sends no answer.
          </Typography>
        )}
        {onIgnore && (ignoreCaption || trimmedComment) && (
          <Typography variant="caption" data-testid="gate-ignore-discards" sx={{ color: 'text.secondary' }}>
            {ignoreCaption ?? 'Ignore discards the comment.'}
          </Typography>
        )}
      </Box>
    )
  }

  if (options && options.length > 0) {
    /* With a recommendation the suggested option is the one filled act and the rest are outlined, so the
       eye lands on it; without one every option stays filled (none outranks another). */
    const anyRecommended = options.some((opt, i) => detailParts(opt, i).recommended)
    return (
      // `column` is a REQUIREMENT (the box is filled before the click that sends it); the testid pins it,
      // since DOM order would survive `column-reverse`.
      <Box data-testid="gate-answer-controls" sx={{ display: 'flex', flexDirection: 'column', gap: 1.33 }}>
        {/* ABOVE the buttons: every button submits on the click. */}
        {commentBox}
        {/* `row` spelled out so the left-to-right order can be pinned (see above). */}
        <Box sx={{ display: 'flex', flexDirection: 'row', flexWrap: 'wrap', gap: 1.33, ...ACTION_ROW_PHONE_SX }}>
          {options.map((opt, i) => {
            /* Raw index = source index here: this branch renders `options` unfiltered (unlike the
               multi-choice branch). */
            const detail = detailParts(opt, i)
            // Long labels wrap and are bounded by the shared `OPTION_LABEL_SX` (R1); `title={opt}` is the
            // full value. The button grows (`height: auto`, `whiteSpace: normal`).
            const button = (
              <Button
                key={i}
                variant={anyRecommended && !detail.recommended ? 'outlined' : 'contained'}
                color="primary"
                title={opt}
                /* The accessible name stays the label alone (the meaning rides `aria-describedby`; WCAG 2.5.3).
                   Only set when there is detail to keep out, else no attribute at all. */
                aria-label={detail.hasInlineDetail ? detail.shown : undefined}
                aria-describedby={detail.describedBy}
                disabled={sending}
                onClick={() => {
                  /* Needs typed input and the box is empty: prompt instead of sending (see `commentPrompt`). */
                  if (optionDetail(optionDetails, i)?.needs_comment && !trimmedComment) {
                    setCommentPrompt(detail.description ?? `Type a value in the comment, then choose “${detail.shown}” again.`)
                    commentInputRef.current?.focus()
                    return
                  }
                  send(opt, trimmedComment || undefined)
                }}
                sx={{
                  ...BUTTON_SX,
                  maxWidth: '100%',
                  whiteSpace: 'normal',
                  textAlign: 'left',
                  height: 'auto',
                  /* A detailed option takes its own wrap line via `flexBasis: 100%` — the row direction is a
                     pinned requirement (`[options…][Comment only][Ignore]`), and three detailed options sharing
                     one line would clamp to nothing. Column + `flex-start` inside (MUI Button centres). The basis
                     goes on the row's own child: with a preview that is the (column) wrapper, where a basis
                     would size height; the button stretches inside it. */
                  ...(detail.hasInlineDetail
                    ? { flexDirection: 'column', alignItems: 'flex-start', gap: 0.25 }
                    : null),
                  ...(detail.hasInlineDetail && !detail.preview ? { flexBasis: '100%' } : null),
                }}
              >
                {detail.hasInlineDetail ? (
                  <>
                    <Box component="span" sx={LABEL_LINE_SX}>
                      <Box component="span" sx={OPTION_LABEL_SX}>{detail.shown}</Box>
                      {detail.recommended && <RecommendedBadge id={detail.recommendedId} />}
                    </Box>
                    {detail.description && (
                      <OptionDescription text={detail.description} id={detail.descriptionId} />
                    )}
                  </>
                ) : (
                  <Box component="span" sx={OPTION_LABEL_SX}>{detail.shown}</Box>
                )}
              </Button>
            )
            /* The preview sits UNDER the button, outside it: a scrollbar drag inside a `<button>` would
               submit the round. The wrapper takes the full-line basis; with no preview there is no wrapper,
               keeping the pinned children assertion honest. */
            return detail.preview ? (
              <Box
                key={i}
                data-testid="gate-option-block"
                sx={{ flexBasis: '100%', minWidth: 0, display: 'flex', flexDirection: 'column' }}
              >
                {button}
                <OptionPreview text={detail.preview} />
              </Box>
            ) : button
          })}
          <Box data-testid="gate-secondary-acts" sx={SECONDARY_ACTS_SX}>
            <Button
              data-testid="gate-comment-only"
              /* Outlined on a phone: the options are the answer, and a second filled button there
                 out-shouted them in the one row a phone has. Outlined everywhere while disabled, as
                 Submit answer is: a disabled contained button is a filled grey slab. */
              variant={phone || (!canCommentOnly && !sending) ? 'outlined' : 'contained'}
              color="secondary"
              // Named per round (see `roundLabel`); the visible label stays bare.
              aria-label={roundLabel ? `Comment only for ${roundLabel}` : 'Comment only'}
              aria-describedby={describedById}
              disabled={!canCommentOnly}
              onClick={() => send(trimmedComment)}
              sx={{ ...BUTTON_SX, flexShrink: 0 }}
            >
              Comment only
            </Button>
            {ignoreButton}
            {attachControl}
          </Box>
        </Box>
        {/* The discard consequence said out loud (no hover on touch), only once there is text to lose. */}
        {onIgnore && (ignoreCaption || trimmedComment) && (
          <Typography variant="caption" data-testid="gate-ignore-discards" sx={{ color: 'text.secondary' }}>
            {ignoreCaption ?? 'Ignore discards the comment.'}
          </Typography>
        )}
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
      sx={{ display: 'flex', flexDirection: 'column', gap: 1.33, alignItems: 'flex-start' }}
    >
      <TextField
        multiline
        minRows={3}
        value={text}
        disabled={submitting}
        onChange={e => setText(e.target.value)}
        placeholder="Type your answer…"
        fullWidth
        slotProps={{ htmlInput: { 'aria-label': 'Answer', 'aria-describedby': describedById } }}
        // DEC-3: the 6.5rem floor on the TEXTAREA itself — `task-detail-fit.spec.ts` measures its own
        // `clientHeight` (a wrapper floor left 69px, under the 6rem criterion). `min-height` beats MUI
        // autosize's inline `height`; the box still grows and stays resizable.
        sx={{ ...FIELD_SX,
          '& .MuiInputBase-root': { alignItems: 'flex-start' },
          '& textarea[aria-label="Answer"]': { minHeight: '6.5rem' },
          ...TEXTAREA_RESIZE_SX,
        }}
      />
      {/* No comment box here: the textarea IS the operator's words. Ignore joins the row (it belongs to the
          ROUND, not the answer shape). */}
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1.33, ...ACTION_ROW_PHONE_SX }}>
        {/* Outlined while there is nothing to submit: a disabled CONTAINED button is a filled grey slab
            that reads as an input on a phone; disabled, it matches the task form's Save. */}
        <SubmitButton sx={{ ...BUTTON_SX, flexShrink: 0 }} aria-describedby={describedById} busy={submitting}
          busyLabel="Submitting…" disabled={!canSubmit} variant={canSubmit || submitting ? 'contained' : 'outlined'}>
          Submit answer
        </SubmitButton>
        <Box data-testid="gate-secondary-acts" sx={SECONDARY_ACTS_SX}>
          {ignoreButton}
          {attachControl}
        </Box>
      </Box>
    </Box>
  )
}

/* ── THE MULTI-ROUND GATE, REBUILT AROUND ONE ANSWER CONTROL PER OPEN ROUND ─────────────────────────
   Several rounds can be open at once (eight, on dev), so ONE control wired to the first open round
   silently answered the wrong one and left the rest unanswerable. The panel is two BOUNDED regions:
   OUTSTANDING rounds on top, each block carrying its own controls, and the RESOLVED trail below. Each is a
   scrollport because round count is unbounded (a `maxHeight` clamp would hide rounds unreachably), and
   `e2e/task-detail-fit.spec.ts` forbids the answer panel scrolling the page. */

/* Both regions say `overscroll-behavior: auto` out loud: see PAGE_SX's "WHY NO SCROLLPORT ON THIS PAGE
   CONTAINS ITS OVERSCROLL" in `pages/taskDetail/taskDetailConstants.ts`, and `e2e/app-shell-scroll.spec.ts` fails on
   contain/none. At <=480px they stop being scroll containers ("ONE SCROLLPORT PER PHONE"): the panel is
   content-sized there. Declared here because this file owns these elements. */
const SCROLLPORT_SX = {
  overflowY: 'auto',
  minHeight: 0,
  overscrollBehavior: 'auto',
} as const

/* The phone half, spread LAST in each list's `sx`: a spread-in `@media` key is emitted at its first
   insertion position, so a mid-spread media block would lose to the base values it overrides. */
const SCROLLPORT_PHONE_SX = { overflowY: 'visible' } as const

// Ordered lists (the ordinal is the meaning) with the marker suppressed ("Round N" is the ordinal).
// Explicit `role="list"`: Safari strips list semantics under `list-style: none`, losing "list, 8 items".
const LIST_RESET_SX = { listStyle: 'none', m: 0, p: 0, display: 'flex', flexDirection: 'column' } as const

/* ── EVERY OPEN ROUND AS TEXT SOMEONE CAN PASTE INTO A CHAT ────────────────────────────────────────
   Options are buttons and checkbox labels, so selecting their text submits them; the panel offers the
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
  /* Rendered set paired with SOURCE index (as `AnswerControl`'s `visible`), so no option gets its
     neighbour's detail. */
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
    `${count} open question${count === 1 ? '' : 's'}, waiting on a decision:`,
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
   "/tasks/<id>?tab=answer". Not `answerHref`, which answers `null` while an answer is DELIVERING (this
   panel still shows then). */
function answerUrl(task: PipelineTask): string {
  const origin = typeof window === 'undefined' ? '' : window.location.origin
  return `${origin}${answerPath(task.id)}`
}

/* THE GATE'S IDENTIFIER ROW, one for both forms: ● `task #<id> · <role>`. Named unconditionally:
   `persona` is NOT NULL. The ROLE label (playbook stage, else persona) is the word the board row and Details ›
   Persona read for this same row. The lead dot takes the row's own status tone: a red 🛑
   beside the amber Asking chip read as a failure. */
function GateTaskHeading({ task, sx }: { task: PipelineTask; sx?: SystemStyleObject<Theme> }) {
  return (
    <Typography variant="body2" data-testid="gate-task-heading" sx={{ fontWeight: 600, color: 'text.primary', ...sx }}>
      <StatusDot data-testid="gate-task-dot" tone={taskStatus(task).tone} sx={{ mr: '0.4em', verticalAlign: '0.1em' }} />
      <Typography component="span" variant="mono" sx={{ color: 'text.secondary' }}>
        {taskLabel(task)}
      </Typography>
      <Typography component="span" variant="body2" sx={{ color: 'text.secondary' }}>
        {' '}· {taskRoleLabel(task)}
      </Typography>
    </Typography>
  )
}

/* THE HEADING'S `Copy all` (one name on both gate shapes): `ml: auto` + the heading's `flexWrap` drops it to
   its own line on a phone. Not withheld from a reader — forwarding the questions is a read. */
function GateCopyAll({ title, text }: { title: string; text: () => string }) {
  return <CopyButton testId="gate-copy-all" label="Copy all" title={title} sx={{ ml: 'auto' }} text={text} />
}

/* ONE OUTSTANDING ROUND: the question and its controls in one bordered block, so a click can only post
   `q.id`. */
function OutstandingRound({
  q,
  submitting,
  readOnly,
  onAnswer,
  onIgnore,
  taskId,
  batched,
  inBatch = false,
}: {
  q: TaskQuestion
  submitting: boolean
  /* Under a `RoundBatch` that already names the header: the round shows no chip of its own. */
  inBatch?: boolean
  /* Other rounds are open too: the CP holds every answer until none is (`_settle_gate_if_nothing_outstanding`),
     so answering this one alone sends nothing yet. */
  batched: boolean
  /* The round's text is a `tasks:view` read; only the two verbs (`answer-question`, `ignore-question`) are
     `tasks:edit`, so a reader keeps everything but those. Distinct from `onIgnore`'s absence (a host that
     wired no dismissal vs. a principal who may not use one). */
  readOnly: boolean
  // Optional: `readOnly` admits no handler (see `multiRound`); read under `!readOnly` only.
  onAnswer?: (answer: GateAnswer, questionId: number, comment?: string, attachmentIds?: number[]) => void
  onIgnore: ((questionId: number) => void) | undefined
  taskId: number
}) {
  // A group NAMED BY THE QUESTION distinguishes eight controls whose `aria-label` must stay "Answer"
  // (`e2e/task-detail-fit.spec.ts`); the same id also goes to `aria-describedby`, since group names are
  // not reliably announced.
  const labelId = `gate-q-${q.id}`
  /* The reword, when it is no longer than the runner's question (`pageWords`); else the runner's text. */
  const words = pageWords(q.question, q.plain)
  const plain = words.reworded ? q.plain : null
  return (
    <Box
      component="li"
      data-testid="gate-round"
      data-round-state="open"
      sx={{
        py: 1.5,
        px: 1.75,
        backgroundColor: 'background.paper',
        border: '1px solid',
        borderColor: 'divider',
        borderLeft: '3px solid',
        // Open rounds carry the accent (`info`, "pending decision"); history carries none.
        borderLeftColor: 'info.main',
        borderRadius: 1,
        display: 'flex',
        flexDirection: 'column',
        gap: 1,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.33 }}>
        <Typography variant="caption" sx={{ fontWeight: 600, color: 'text.secondary' }}>
          Round {q.round}
        </Typography>
        {/* The runner's short name per round (one `AskUserQuestion` batch arrives as blocks told apart only
            by ordinal). After the ordinal, which is what an answer routes on. */}
        {!inBatch && <RoundHeaderChip header={q.header} />}
        <StatusChip label={q.status} tone="warning" />
        {/* `Ignore` lives in the action row (see `AnswerControl`), de-emphasised by variant and hue. */}
      </Box>
      {/* The full question, never clamped (the bounded scrollport pays the height). `pre-wrap` for
          multi-line text, `anywhere` for unbroken paths. Plain text only — agent-authored. */}
      {plain ? (
        <>
          {/* The reworded headline names the round; the runner's own wording is on hover over it. */}
          <Typography
            id={labelId}
            tabIndex={-1}
            data-testid="gate-plain-headline"
            title={q.question}
            sx={{ fontWeight: 650, fontSize: '1.0625rem', lineHeight: 1.35, color: 'text.primary', overflowWrap: 'anywhere', cursor: 'help' }}
          >
            {words.headline}
          </Typography>
          {words.why && (
            <Typography variant="body2" data-testid="gate-plain-why" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
              {words.why}
            </Typography>
          )}
        </>
      ) : (
        <Typography
          id={labelId}
          tabIndex={-1}
          variant="body2"
          sx={{ fontWeight: 600, color: 'text.primary', overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}
        >
          {q.question}
        </Typography>
      )}
      {/* A dashed 1px rule between ask and answer (distinct from the solid block borders); dropped when
          read-only, since there is no second half to divide. */}
      {!readOnly && (
      <Box
        role="group"
        aria-labelledby={labelId}
        sx={{ borderTop: '1px dashed', borderColor: 'divider', pt: 1.33 }}
      >
        {/* The round's own buttons, or free-text + Submit. No inner `key`: the parent list keys the block
            by `q.id`, which is where draft isolation lives. */}
        <AnswerControl
          options={q.options}
          // What each option MEANS, index-aligned with `options`, straight off the round.
          optionDetails={q.option_details}
          plainOptions={plain?.options}
          // The runner's own `multiSelect`, never inferred (`TaskQuestion.multi_select`).
          multiSelect={q.multi_select}
          submitting={submitting}
          describedById={labelId}
          onSubmit={(answer, comment, attachmentIds) => (attachmentIds
            ? onAnswer?.(answer, q.id, comment, attachmentIds)
            : onAnswer?.(answer, q.id, comment))}
          onIgnore={onIgnore ? () => onIgnore(q.id) : undefined}
          roundLabel={`round ${q.round}`}
          taskId={taskId}
        />
      </Box>
      )}
      {/* Read-only, the options return as comma-joined prose: they are half of what the round ASKS. */}
      {readOnly && q.options && q.options.length > 0 && (
        <Typography
          variant="caption"
          data-testid="gate-round-options"
          sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}
        >
          Options: {q.options.join(' · ')}
        </Typography>
      )}
      {batched && (
        <Typography variant="caption" data-testid="gate-round-delivery" sx={{ color: 'text.secondary' }}>
          Delivered when all rounds are answered.
        </Typography>
      )}
    </Box>
  )
}

// The gate-answer form for a Task pending a human decision (spec R5). Two additive modes:
//   • Multi-round — when `onAnswerQuestion` is wired (or `readOnly`) AND the Task has `questions`: the
//     OUTSTANDING rounds, each answering exactly itself via POST /tasks/{id}/input-requests/{qid}/answer, over the
//     RESOLVED history. No answer control once nothing is outstanding.
//   • Legacy single-shot — otherwise: the `awaiting_response_body` preview + the
//     `awaiting_response_options` picker/free-text, via `onSubmit` (POST /tasks/{id}/answer).
// On failure the caller's error stays visible and the form stays usable.
export default function GateForm({
  task,
  submitting,
  delivering = false,
  error,
  onSubmit,
  onAnswerQuestion,
  onIgnoreQuestion,
  onReviseQuestion,
  reviseError,
  readOnly = false,
  hideTaskHeader = false,
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
  /* Multi-round answering; optional so single-shot callers are untouched. `GateAnswer`: the host maps it to
     `answer` XOR `selections`. */
  onAnswerQuestion?: (
    answer: GateAnswer, questionId?: number, comment?: string, attachmentIds?: number[],
  ) => void
  // Dismiss one outstanding round. Separately optional: the handler's presence IS the capability check
  // (no dead button).
  onIgnoreQuestion?: (questionId: number) => void
  /* Reopen a SETTLED round (`POST /tasks/{id}/revise-question`), offered per entry by `QATrail`. Presence
     is the capability check; `readOnly` withholds it too. The Activity › Q&A subtab offers it as well,
     since this form is gone once the last round settles. */
  onReviseQuestion?: (questionId: number) => void
  /* A failed Revise, apart from `error` (the host raises it from two tabs); rendered by `QATrail`. */
  reviseError?: string
  /* This form is the ONLY surface rendering what a gate asks, and asking is a `tasks:view` READ
     (`GET /api/tasks/{id}/questions`). `readOnly` withholds exactly the `tasks:edit` verbs: option buttons,
     answer textarea, Submit, Ignore, Revise. One prop for the set (one server fact); a prop, not `useCan()`
     here, so tests need no permission mock (as `TaskDependencies`/`TaskAttachmentsField`). */
  readOnly?: boolean
  /* Drops the multi-round `🛑 task #<id> · <persona>` row, for a host that already names the task (the
     console chat). */
  hideTaskHeader?: boolean
  /* Show the open rounds as pages (`GateSteps`, the task page) instead of one list: the last page's Send hands
     every round's answer or skip here at once. Presence is the switch. */
  onSendRounds?: (sends: RoundSend[]) => void
  /* Where the task's recorded trail is (`to`, and the `label` the link reads), for the card a gate with no question
     text would otherwise leave without a next step. The host picks the trail (Q&A when there are entries,
     else the Timeline); one with no such tab (the console chat) passes none. */
  trailLink?: { to: string; label: string }
  /* The Timeline row that parked the gate (`at`, and the person who did, `actor`, null for a machine row), shown on that
     same empty card so the reader needs no second click. A host with no feed passes none. */
  parkEvent?: { at: string; actor: string | null }
}) {
  const questions = task.questions ?? []
  /* `readOnly ||` is load-bearing: without it a reader falls to the legacy branch, which knows nothing of
     `questions`, and the rounds vanish for the one principal with no other surface. */
  const multiRound = (readOnly || onAnswerQuestion !== undefined || onSendRounds !== undefined) && questions.length > 0

  if (multiRound) {
    /* One pass, no sort: the API returns rounds oldest-first and `round` is an ordinal over that order.
       Anything not `open` is history (the safe direction for a future status).
       `outstanding` stays oldest-first (a work queue); `QATrail` reverses the history to newest-first.
       The history IS the Q&A trail: `settledEntries` filters out entries whose round is open above. The
       open list reads the LIVE `task_questions` rows (what answers post against); the history reads the
       note-derived `qa_entries`, as the Q&A subtab does. They agree on the settled set. */
    const outstanding = questions.filter(q => q.status === 'open')
    const resolved = settledEntries(task.qa_entries ?? [], questions)
    const copyAll = (
      <GateCopyAll
        title="Copy every open question and its options"
        text={() => outstandingQuestionsText(task, outstanding, answerUrl(task))}
      />
    )
    return (
      /* The root FILLS the panel: `DetailSection fill` (`ui/detail`, the single home for the chain) gives a
         definite height, so `height: 100%` + `minHeight: 0` lets the two scrollports divide it instead of
         growing the page. At <=480px it resolves to auto and the shell scrolls. */
      <Box
        data-testid="gate-form"
        sx={{ height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0, gap: 1.33 }}
      >
        {!hideTaskHeader && <GateTaskHeading task={task} sx={{ flexShrink: 0 }} />}

        {outstanding.length > 0 && onSendRounds ? (
          <GateSteps
            rounds={outstanding}
            readOnly={readOnly}
            submitting={submitting}
            taskId={task.id}
            onSend={onSendRounds}
            copyAll={copyAll}
          />
        ) : outstanding.length > 0 ? (
          <>
            {/* The count rides in the heading (no scrolling to learn how many are waiting); `flexShrink: 0`
                keeps the heading out of the height negotiation. */}
            <Box sx={{ flexShrink: 0, display: 'flex', alignItems: 'baseline', gap: 1, flexWrap: 'wrap' }}>
              <Typography variant="body2" sx={{ fontWeight: 600, color: 'text.primary' }}>
                Outstanding questions ({outstanding.length})
              </Typography>
              {/* Read-only, the sub-line says why there is nothing to press. */}
              {readOnly && (
                <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                  waiting on someone who can answer
                </Typography>
              )}
              {/* In the heading: it copies the whole list. */}
              {copyAll}
            </Box>
            <Box
              component="ol"
              role="list"
              data-testid="gate-open-list"
              sx={{
                ...LIST_RESET_SX,
                ...SCROLLPORT_SX,
                // The working surface takes the slack and shrinks before the page does.
                flex: '1 1 auto',
                // A full gap between blocks (vs 0.33 inside an entry), so questions read as units.
                gap: 1.75,
                // Clearance for the scrollbar, including overlay scrollbars that take no layout width.
                pr: 0.75,
                '@media (max-width:480px)': { ...SCROLLPORT_PHONE_SX, pr: 0 },
              }}
            >
              {/* Keyed by round ID, not position: answering from the middle would otherwise carry a half-typed
                  draft into another round's box. */}
              {/* Rounds from one `AskUserQuestion` call sharing a header sit under that header once
                  (`batchGroups`). Grouped over EVERY round, then filtered to the open ones, and keyed
                  by the call's first round: answering one of a batch must not re-parent (and so remount, losing
                  a half-typed draft in) the rest. */}
              {batchGroups(questions, q => q).map(call => {
                const group = call.filter(q => q.status === 'open')
                if (group.length === 0) return null
                const rounds = group.map(q => (
                  <OutstandingRound
                    key={q.id}
                    q={q}
                    submitting={submitting}
                    readOnly={readOnly}
                    onAnswer={onAnswerQuestion}
                    onIgnore={onIgnoreQuestion}
                    taskId={task.id}
                    batched={outstanding.length > 1}
                    inBatch={call.length > 1}
                  />
                ))
                return call.length > 1
                  ? <RoundBatch key={`batch-${call[0].id}`} header={call[0].header} ordered gap={1.75} testId="gate-round-batch">{rounds}</RoundBatch>
                  : rounds[0]
              })}
            </Box>
          </>
        ) : (
          <Typography variant="caption" sx={{ flexShrink: 0, color: 'text.secondary', fontStyle: 'italic' }}>
            All rounds answered — waiting on the runner's next step.
          </Typography>
        )}

        {/* Directly under the answer area, where the operator just clicked (not below the history). Clears
            when the row's `answer_delivering` does — no timer. */}
        {delivering && (
          <Box sx={{ flexShrink: 0 }}>
            <Banner severity="info" testId="gate-delivering">
              Delivering your answer to the runner…
            </Banner>
          </Box>
        )}

        {/* No history, no box. */}
        {resolved.length > 0 && (
          <>
            <Typography
              variant="caption"
              sx={{
                flexShrink: 0,
                fontWeight: 600,
                color: 'text.secondary',
                textTransform: 'uppercase',
                letterSpacing: '0.04em',
                borderTop: '1px solid',
                borderColor: 'divider',
                pt: 1.33,
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
                   `3.5rem` ≈ two compact entries. Declared after the `SCROLLPORT_SX` spread to replace its
                   `minHeight: 0` with a bounded shrink. */
                minHeight: '3.5rem',
                /* The cap lifted outright at phone width — `33%` already computes to `none` there, but saying
                   so locally keeps it correct if the panel ever gains a definite height. */
                '@media (max-width:480px)': { ...SCROLLPORT_PHONE_SX, maxHeight: 'none' },
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
    <Box data-testid="gate-form" sx={{ display: 'flex', flexDirection: 'column', gap: 1.33 }}>
      {/* A compact 🛑 identifier; the question is the headline. */}
      {/* The identifier row doubles as the heading, so `Copy all` rides here (same name as multi-round). */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
        <GateTaskHeading task={task} />
        {body && (
          <GateCopyAll title="Copy this question and its options as text" text={() => singleQuestionText(task, answerUrl(task))} />
        )}
      </Box>
      {body && (
        <>
          {questionLine && (
            <Typography variant="body2" data-testid="gate-question-line" sx={{ fontWeight: 600, mb: '0.35rem' }}>
              {questionLine}
            </Typography>
          )}
          {/* Open by default for a READER: the writeup is collapsed for an editor because it pushed the CONTROLS
              off the panel, and a reader has none. The editor's default is unchanged. */}
          <Accordion
            disableGutters
            elevation={0}
            variant="outlined"
            defaultExpanded={readOnly}
            sx={{ mb: 1.33 }}
          >
            <AccordionSummary
              expandIcon="▾"
              sx={{
                // `auto` at desktop; at <=480px the 44px touch floor wins.
                minHeight: 'auto',
                '@media (max-width:480px)': { minHeight: 44 },
                color: 'text.secondary',
                '&.Mui-expanded': { minHeight: 'auto', '@media (max-width:480px)': { minHeight: 44 } },
                '& .MuiAccordionSummary-content': { margin: '4px 0' },
                '& .MuiAccordionSummary-content.Mui-expanded': { margin: '4px 0' },
              }}
            >
              <Typography variant="caption">Show details</Typography>
            </AccordionSummary>
            <AccordionDetails sx={{ p: 0 }}>
              {/* Raw body as plain text (never dangerouslySetInnerHTML: runner-authored, XSS), whitespace-preserving. */}
              <CodeBlock
                bordered
                maxHeight={200}
                sx={{
                  py: 1.75, px: 2, borderRadius: 0, bgcolor: 'background.paper',
                  borderLeft: '3px solid', borderLeftColor: 'info.main', lineHeight: 1.65, color: 'text.secondary',
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
          /* No `optionDetails` (a bare `string[]`, so labels only) and `multiSelect={false}` said out loud:
             this legacy choice-set has neither; multi-choice arrives as a `task_questions` round. */
          multiSelect={false}
          submitting={submitting}
          taskId={task.id}
          /* The narrowing STATES to the type checker that `answer` is text (no cast; `POST /answer` never
             takes `selections`). `...rest` forwards verbatim so each legacy branch keeps its own arity. */
          onSubmit={(answer, ...rest) => {
            if (typeof answer !== 'string') return
            onSubmit(answer, ...rest)
          }}
        />
      )}
      {/* The single-shot branch's copy of the delivering banner. */}
      {delivering && (
        <Banner severity="info" testId="gate-delivering">Delivering your answer to the runner…</Banner>
      )}
      {error && <Banner severity="error">{error}</Banner>}
    </Box>
  )
}
