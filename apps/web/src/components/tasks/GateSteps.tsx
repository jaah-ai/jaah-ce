import { useEffect, useId, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
import ExternalLink from '../../ui/ExternalLink'
import LinkButton from '../../ui/LinkButton'
import FormControlLabel from '@mui/material/FormControlLabel'
import Radio from '@mui/material/Radio'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { alpha } from '@mui/material/styles'
import type { TaskQuestion } from '../../types'
import AnswerAttachments from './AnswerAttachments'
import {
  clampDetail,
  hasGlyph,
  MAX_SELECTIONS,
  nonBlank,
  optionDescription,
  optionDetail,
  OptionPreview,
  RecommendedBadge,
  RoundHeaderChip,
  showsRecommended,
} from './questionParts'
import type { GateAnswer } from './roundAnswer'
import { EMPTY_DRAFT as EMPTY, useRoundDrafts, type Draft } from './useRoundDrafts'
import { tokenizeUrls } from '../../lib/commentText'
import { FIELD_SX, TEXTAREA_RESIZE_SX } from '../../ui/formDensity'
import { PHONE, ELLIPSIS_SX } from '../../ui/listStyles'
import { modEnterCapture } from '../../ui/modEnter'
import { usePickAdvance } from '../../hooks/usePickAdvance'
import { MOD_ENTER_LABEL } from '../../ui/modifierKey'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT, RADIUS, SIZE, grid } from '../../theme'
import Tip from '../../ui/Tip'

/* ── THE OPEN ROUNDS AS PAGES ──────────────────────────────────────────────────────────────────────────
   One page per open round. Clicking a radio picks AND moves on, as Next would (on the last page, as Send
   would), after a short pause (`hooks/usePickAdvance.ts`); a Next pressed in that pause is ignored, so a pick
   followed by Next never skips the page after it. A checkbox, a keyboard pick or a pick that needs a typed value
   only selects. Nothing posts until
   the last page's Send, which hands every page's answer to the host at once — the CP holds answers until no round is open
   anyway (`routes/tasks/answers.py::_settle_gate_if_nothing_outstanding`), so the runner sees no difference
   and the pages never shrink under the reader. Back keeps a page's choice.
   A page with nothing picked is skipped (ignored) on Send, and the step strip marks one the reader has
   passed so the skip is visible before Send; a note with nothing picked is the answer itself.
   So there is no Ignore and no Comment only.
   Cmd/Ctrl+Enter anywhere in the pages presses the shown Next or Send, under the gates that button has (the task
   form's Go shortcut, `TaskEditForm`), and the button names the key the same way. */

/** One open round's outcome, as Send hands it to the host: an answer, or a skip. */
export type RoundSend =
  | { questionId: number; ignore: true }
  | { questionId: number; ignore?: false; answer: GateAnswer; comment?: string; attachmentIds?: number[] }

const touched = (d: Draft) => d.picked.length > 0 || d.note.trim() !== '' || d.files.length > 0

/* The page's send, or a skip. Exported for the unit test. */
export function roundSend(q: TaskQuestion, d: Draft): RoundSend {
  const note = d.note.trim()
  const attachmentIds = d.files.length > 0 ? d.files.map(f => f.id) : undefined
  const extra = attachmentIds ? { attachmentIds } : {}
  if (!touched(d)) return { questionId: q.id, ignore: true }
  const options = q.options ?? []
  // Posted in the round's own option order, so the recorded resolution reads as seen.
  const picked = [...d.picked].sort((a, b) => a - b).map(i => options[i])
  if (picked.length === 0) return { questionId: q.id, answer: note, ...extra }
  if (q.multi_select) return { questionId: q.id, answer: { selections: picked }, ...(note ? { comment: note } : {}), ...extra }
  return { questionId: q.id, answer: picked[0], ...(note ? { comment: note } : {}), ...extra }
}

/* A step's mark: the shown page, an answered one, one the reader passed unanswered (skipped on Send), or one ahead. */
type StepMark = 'current' | 'answered' | 'skipped' | 'ahead'
const STEP_COLOR: Record<StepMark, string> = {
  current: 'primary.main', answered: 'success.main', skipped: 'warning.main', ahead: 'divider',
}

function StepStrip({ marks }: { marks: StepMark[] }) {
  return (
    <Box data-testid="gate-step-strip" aria-hidden sx={{ display: 'flex', gap: 0.5, flex: '1 1 auto', maxWidth: grid(32) }}>
      {marks.map((mark, i) => (
        <Box
          key={i}
          data-step={mark}
          sx={{ flex: 1, height: grid(0.5), borderRadius: RADIUS.md, backgroundColor: STEP_COLOR[mark] }}
        />
      ))}
    </Box>
  )
}

// One line per option on a wide panel; on a phone the description takes its own (clamped) line.
const OPTION_ROW_SX = {
  m: 0,
  pr: 1,
  py: 0,
  borderRadius: RADIUS.sm,
  border: '1px solid',
  minWidth: 0,
  '& .MuiFormControlLabel-label': { minWidth: 0, flex: '1 1 auto' },
  [PHONE]: { minHeight: SIZE.tapTarget, alignItems: 'flex-start' },
} as const
const OPTION_TEXT_SX = {
  display: 'flex', alignItems: 'baseline', gap: 0.5, minWidth: 0,
  [PHONE]: { flexWrap: 'wrap', rowGap: 0 },
} as const
const OPTION_DESC_SX = {
  color: 'text.secondary', ...ELLIPSIS_SX,
  [PHONE]: { flexBasis: '100%', whiteSpace: 'normal', display: '-webkit-box', WebkitBoxOrient: 'vertical', WebkitLineClamp: 2 },
} as const

export default function GateSteps({
  rounds,
  readOnly,
  submitting,
  taskId,
  onSend,
  copyAll,
}: {
  // The open rounds, oldest first.
  rounds: TaskQuestion[]
  readOnly: boolean
  submitting: boolean
  taskId: number
  onSend: (sends: RoundSend[]) => void
  // The heading's Copy all control.
  copyAll: ReactNode
}) {
  // Kept on the server until Send, so closing the task loses nothing (`useRoundDrafts`).
  const { drafts, updateDraft } = useRoundDrafts(taskId, rounds, readOnly)
  // The shown page, by round id, so a round arriving mid-way does not move the reader.
  const [pageId, setPageId] = useState<number | null>(null)
  const [uploading, setUploading] = useState(false)
  // A picked option that needs typed input, with the note empty: what to type.
  const [prompt, setPrompt] = useState<string | null>(null)
  const noteRef = useRef<HTMLTextAreaElement>(null)
  // MUI names the helper `${id}-helper-text`; the input's own `aria-describedby` (set below) must list it.
  const noteId = useId()

  /* When the shown round leaves the open set (answered elsewhere), the page stays at its POSITION. */
  const lastAt = useRef(0)
  const found = rounds.findIndex(q => q.id === pageId)
  const at = found >= 0 ? found : Math.min(lastAt.current, rounds.length - 1)
  lastAt.current = at
  const q = rounds[at]
  const draft = drafts[q.id] ?? EMPTY
  const last = at === rounds.length - 1
  const busy = submitting || uploading
  const setDraft = (next: Partial<Draft>) => {
    setPrompt(null)
    updateDraft(q.id, next)
  }

  // The first page whose pick needs a note it does not have, with what to type.
  const missingNote = (from: TaskQuestion[], all: Record<number, Draft>) => {
    for (const r of from) {
      const d = all[r.id] ?? EMPTY
      const need = d.picked.map(i => optionDetail(r.option_details, i)).find(det => det?.needs_comment)
      if (need && d.note.trim() === '') return { id: r.id, text: need.description ?? `Type a value for “${need.label}”.` }
    }
    return null
  }
  const go = (i: number) => { setPrompt(null); setPageId(rounds[i].id) }
  // A prompt moves focus to the note it asks for, once that page has rendered.
  useEffect(() => { if (prompt) noteRef.current?.focus() }, [prompt, pageId])
  const next = () => {
    const need = missingNote(last ? rounds : [q], drafts)
    if (need) {
      setPageId(need.id)
      setPrompt(need.text)
      return
    }
    if (!last) go(at + 1)
    else onSend(rounds.map(r => roundSend(r, drafts[r.id] ?? EMPTY)))
  }
  // After a click moved the page, focus goes to the new question, not to a radio of the old one.
  const focusHeadline = useRef(false)
  const advance = usePickAdvance(() => { if (busy) return; focusHeadline.current = true; next() })
  useEffect(() => {
    if (!focusHeadline.current) return
    focusHeadline.current = false
    document.getElementById(`gate-q-${rounds[at].id}`)?.focus()
  }, [pageId, rounds, at])
  const back = () => { advance.cancel(); go(at - 1) }
  // A page behind the shown one with nothing on it is passed unanswered: Send skips it. A reader sends nothing.
  const marks: StepMark[] = rounds.map((r, i) =>
    i === at ? 'current' : touched(drafts[r.id] ?? EMPTY) ? 'answered' : i < at && !readOnly ? 'skipped' : 'ahead')
  const firstSkipped = marks.indexOf('skipped')
  const skippedCount = marks.filter(m => m === 'skipped').length

  // The nav button's press: Next or Send, or (read-only) paging forward; null when the button is absent or disabled.
  const press = busy || (readOnly && last) ? null : readOnly ? () => go(at + 1) : () => { if (!advance.guarding()) next() }

  const labelId = `gate-q-${q.id}`
  const options = q.options ?? []
  const rows = options.map((opt, source) => ({ opt, source })).filter(row => !q.multi_select || hasGlyph(row.opt))
  const atCap = draft.picked.length >= MAX_SELECTIONS
  const pick = (source: number) => {
    if (!q.multi_select) {
      if (draft.picked[0] === source) { advance.cancel(); return setDraft({ picked: [] }) }
      setDraft({ picked: [source] })
      if (!optionDetail(q.option_details, source)?.needs_comment) advance.picked()
      return
    }
    if (draft.picked.includes(source)) return setDraft({ picked: draft.picked.filter(n => n !== source) })
    if (!atCap) setDraft({ picked: [...draft.picked, source] })
  }

  return (
    <Box
      data-testid="gate-steps"
      onKeyDownCapture={modEnterCapture(() => press?.(), press !== null)}
      sx={{ display: 'flex', flexDirection: 'column', gap: 1, minHeight: 0, flex: '1 1 auto' }}>
      <Box sx={{ flexShrink: 0, display: 'flex', alignItems: 'center', gap: 1 }}>
        <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.semibold, color: 'text.primary' }}>Questions</Typography>
        {rounds.length > 1 && (
          <>
            <StepStrip marks={marks} />
            <Typography variant="caption" data-testid="gate-step-count" aria-live="polite" sx={{ color: 'text.secondary', whiteSpace: 'nowrap' }}>
              {at + 1} of {rounds.length}
            </Typography>
            {!readOnly && skippedCount > 0 && (
              <LinkButton
                title={skippedCount > 1 ? 'Skipped on Send. Select to go back to the first.' : 'Skipped on Send. Select to go back to it.'}
                variant="caption"
                data-testid="gate-step-skipped"
                disabled={busy}
                onClick={() => { advance.cancel(); go(firstSkipped) }}
                sx={{ color: 'warning.main', whiteSpace: 'nowrap' }}
              >
                {skippedCount} unanswered
              </LinkButton>
            )}
          </>
        )}
        {readOnly && (
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>waiting on someone who can answer</Typography>
        )}
        {copyAll}
      </Box>
      <Box
        component="section"
        data-testid="gate-open-list"
        aria-labelledby={labelId}
        sx={{
          // Content-sized, so the nav row follows the card instead of the panel's foot; shrinks (and scrolls) on a long page.
          flex: '0 1 auto',
          minHeight: 0,
          overflowY: 'auto',
          overscrollBehavior: 'auto',
          [PHONE]: { overflowY: 'visible' },
        }}
      >
        <Box
          data-testid="gate-round"
          data-round-state="open"
          sx={{
            py: 1, px: 1.5,
            backgroundColor: 'background.paper',
            border: '1px solid', borderColor: 'divider', borderLeft: `${grid(0.5)} solid`, borderLeftColor: 'info.main', borderRadius: RADIUS.sm,
            display: 'flex', flexDirection: 'column', gap: 0.5,
          }}
        >
          <Typography
            id={labelId}
            tabIndex={-1}
            data-testid="gate-plain-headline"
            sx={{
              fontWeight: FONT_WEIGHT.semibold, fontSize: FONT_SIZE.lg, lineHeight: LINE_HEIGHT.snug, color: 'text.primary', overflowWrap: 'anywhere',
              whiteSpace: 'pre-wrap',
            }}
          >
            {tokenizeUrls(q.question.trim()).map((token, i) => token.kind === 'link' ? (
              /* A control-plane round can name where to fix its cause (`checkout_gate.retry_refusal`). */
              <ExternalLink key={i} href={token.href}>{token.text}</ExternalLink>
            ) : token.text)}
            {/* The round's short name rides the headline's line instead of a row of its own. */}
            {nonBlank(q.header) && <Box component="span" sx={{ ml: 1 }}><RoundHeaderChip header={q.header} /></Box>}
          </Typography>
          {rows.length > 0 && (
            <Box
              key={q.id}
              role={q.multi_select ? 'group' : 'radiogroup'}
              aria-labelledby={labelId}
              data-testid="gate-options"
              {...(q.multi_select ? {} : advance.groupProps)}
              data-answer-shape={q.multi_select ? 'multi-select' : 'single'}
              sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}
            >
              {rows.map(({ opt, source }) => {
                const detail = optionDetail(q.option_details, source)
                const shown = opt
                // Bounded at the render boundary like every placement (`OPTION_DETAIL_MAX_CHARS`).
                const described = optionDescription(opt, detail)
                const description = described === null ? null : clampDetail(described)
                const preview = nonBlank(detail?.preview)
                const checked = draft.picked.includes(source)
                const Control = q.multi_select ? Checkbox : Radio
                // The description and badge describe the option; its name stays the label alone.
                const descriptionId = description ? `${labelId}-desc-${source}` : undefined
                const recommendedId = showsRecommended(shown, detail) ? `${labelId}-rec-${source}` : undefined
                return (
                  <Box key={source} sx={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                    <Tip title={[opt, description].filter(Boolean).join(' — ')}><FormControlLabel
                      data-testid="gate-option"
                      data-checked={checked ? 'true' : 'false'}
                      // The runner's label and the whole description, where the row cuts them.
                      disabled={readOnly || busy || (q.multi_select && atCap && !checked)}
                      control={
                        <Control
                          size="small"
                          checked={checked}
                          name={q.multi_select ? undefined : labelId}
                          // A radio cannot untick itself; a second click on the picked one clears it here.
                          onClick={q.multi_select ? undefined : () => { if (checked) pick(source) }}
                          onChange={() => { if (q.multi_select || !checked) pick(source) }}
                          slotProps={{ input: {
                            'aria-label': shown,
                            'aria-describedby': [recommendedId, descriptionId].filter(Boolean).join(' ') || undefined,
                          } }}
                          sx={{ p: 0.5 }}
                        />
                      }
                      label={
                        <Box component="span" sx={OPTION_TEXT_SX}>
                          <Box component="span" sx={{ fontWeight: FONT_WEIGHT.semibold, flexShrink: 0, maxWidth: '100%', overflowWrap: 'anywhere' }}>{shown}</Box>
                          {recommendedId && <RecommendedBadge id={recommendedId} />}
                          {description && <Typography component="span" id={descriptionId} variant="body2" sx={OPTION_DESC_SX}>{description}</Typography>}
                        </Box>
                      }
                      sx={theme => ({
                        ...OPTION_ROW_SX,
                        borderColor: checked ? 'primary.main' : 'divider',
                        backgroundColor: checked ? alpha(theme.palette.primary.main, 0.12) : 'transparent',
                      })}
                    /></Tip>
                    {preview && <OptionPreview text={preview} />}
                  </Box>
                )
              })}
            </Box>
          )}
          {!readOnly && (
            <TextField
              multiline
              minRows={rows.length > 0 ? 1 : 3}
              value={draft.note}
              disabled={submitting}
              onChange={e => setDraft({ note: e.target.value })}
              placeholder={rows.length > 0 ? 'Note (optional)' : 'Type your answer…'}
              id={noteId}
              inputRef={noteRef}
              helperText={prompt ?? undefined}
              fullWidth
              slotProps={{
                formHelperText: { role: 'status' },
                htmlInput: {
                  'aria-label': rows.length > 0 ? 'Note' : 'Answer',
                  'aria-describedby': [labelId, prompt ? `${noteId}-helper-text` : null].filter(Boolean).join(' '),
                },
              }}
              sx={{ ...FIELD_SX, '& .MuiInputBase-root': { alignItems: 'flex-start' }, ...TEXTAREA_RESIZE_SX }}
            />
          )}
        </Box>
      </Box>
      <Box data-testid="gate-step-nav" sx={{ flexShrink: 0, display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 1 }}>
        {rounds.length > 1 && (
          <Button variant="outlined" disabled={busy || at === 0} onClick={back} sx={NAV_SX}>Back</Button>
        )}
        <Box sx={{ flex: '1 1 auto' }} />
        {!readOnly && (
          <AnswerAttachments
            // One per page: its upload state belongs to that page.
            key={q.id}
            taskId={taskId}
            files={draft.files}
            onChange={files => setDraft({ files })}
            onBusyChange={setUploading}
            disabled={submitting}
          />
        )}
        {(!readOnly || !last) && (
          <Button
            data-testid={last ? 'gate-send' : 'gate-next'}
            variant={last ? 'contained' : 'outlined'}
            disabled={press === null}
            onClick={() => press?.()}
            aria-keyshortcuts="Control+Enter Meta+Enter"
            sx={NAV_SX}
          >
            {last && submitting ? 'Sending…' : (
              <>
                {last ? 'Send' : 'Next'}
                {/* Out of the accessible name (`aria-keyshortcuts` carries the key); a phone has no keyboard. */}
                <Box component="span" aria-hidden data-testid="gate-nav-shortcut" sx={{ ml: 0.5, [PHONE]: { display: 'none' } }}>
                  ({MOD_ENTER_LABEL})
                </Box>
              </>
            )}
          </Button>
        )}
      </Box>
    </Box>
  )
}

const NAV_SX = {
  [PHONE]: { minHeight: SIZE.tapTarget },
  // Hit-testable while disabled (see `GateForm`'s `BUTTON_SX`).
  '&.Mui-disabled': { pointerEvents: 'auto' },
} as const
