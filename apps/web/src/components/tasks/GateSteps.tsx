import { useEffect, useId, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
import FormControlLabel from '@mui/material/FormControlLabel'
import Radio from '@mui/material/Radio'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { alpha } from '@mui/material/styles'
import type { Attachment, QuestionPlain, TaskQuestion } from '../../types'
import AnswerAttachments from './AnswerAttachments'
import {
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
import type { GateAnswer } from './GateForm'
import { FIELD_SX, TEXTAREA_RESIZE_SX } from '../../ui/formDensity'
import { PHONE } from '../../ui/listStyles'

/* ── THE OPEN ROUNDS AS PAGES ──────────────────────────────────────────────────────────────────────────
   One page per open round. Picking only selects (radio / checkbox); nothing posts until the last page's
   Send, which hands every page's answer to the host at once — the CP holds answers until no round is open
   anyway (`routes/tasks/answers.py::_settle_gate_if_nothing_outstanding`), so the runner sees no difference
   and the pages never shrink under the reader. Back keeps a page's choice.
   A page with nothing picked is skipped (ignored) on Send; a note with nothing picked is the answer itself.
   So there is no Ignore and no Comment only. */

/** One open round's outcome, as Send hands it to the host: an answer, or a skip. */
export type RoundSend =
  | { questionId: number; ignore: true }
  | { questionId: number; ignore?: false; answer: GateAnswer; comment?: string; attachmentIds?: number[] }

/* A page's draft. `picked` is SOURCE indices into the round's `options`; `note` is the free-text answer on a
   round with no options, else the comment riding with the pick. */
type Draft = { picked: number[]; note: string; files: Attachment[] }
const EMPTY: Draft = { picked: [], note: '', files: [] }

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

/* The page's words: the reworded headline only when it is no longer than the runner's own question (else the
   runner's text), and its context line only when the two together stay that short and the line does not
   restate the headline. The runner's wording is on hover over a reworded headline. */
export function pageWords(question: string, plain: QuestionPlain | null | undefined): { headline: string; why: string | null; reworded: boolean } {
  const raw = question.trim()
  const headline = nonBlank(plain?.headline)
  if (!headline || headline.length > raw.length) return { headline: raw, why: null, reworded: false }
  const why = nonBlank(plain?.why)
  if (!why || headline.length + why.length > raw.length || restates(headline, why)) return { headline, why: null, reworded: true }
  return { headline, why, reworded: true }
}

// Half or more of the line's words (4+ letters) already in the headline.
function restates(headline: string, line: string): boolean {
  const words = (s: string) => new Set(s.toLowerCase().match(/[\p{L}\p{N}]{4,}/gu) ?? [])
  const said = words(headline)
  const own = [...words(line)]
  return own.length > 0 && own.filter(w => said.has(w)).length * 2 >= own.length
}

function StepStrip({ done, at }: { done: boolean[]; at: number }) {
  return (
    <Box data-testid="gate-step-strip" aria-hidden sx={{ display: 'flex', gap: 0.75, flex: '1 1 auto', maxWidth: '12rem' }}>
      {done.map((d, i) => (
        <Box
          key={i}
          sx={{ flex: 1, height: 4, borderRadius: 2, backgroundColor: i === at ? 'primary.main' : d ? 'success.main' : 'divider' }}
        />
      ))}
    </Box>
  )
}

// One line per option on a wide panel; on a phone the description takes its own (clamped) line.
const OPTION_ROW_SX = {
  m: 0,
  pr: 1,
  py: 0.25,
  borderRadius: 1,
  border: '1px solid',
  minWidth: 0,
  '& .MuiFormControlLabel-label': { minWidth: 0, flex: '1 1 auto' },
  [PHONE]: { minHeight: 44, alignItems: 'flex-start' },
} as const
const OPTION_TEXT_SX = {
  display: 'flex', alignItems: 'baseline', gap: 0.75, minWidth: 0,
  [PHONE]: { flexWrap: 'wrap', rowGap: 0 },
} as const
const OPTION_DESC_SX = {
  color: 'text.secondary', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
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
  const [drafts, setDrafts] = useState<Record<number, Draft>>({})
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
    setDrafts(prev => ({ ...prev, [q.id]: { ...(prev[q.id] ?? EMPTY), ...next } }))
  }

  // The first page whose pick needs a note it does not have, with what to type.
  const missingNote = (from: TaskQuestion[]) => {
    for (const r of from) {
      const d = drafts[r.id] ?? EMPTY
      const need = d.picked.map(i => optionDetail(r.option_details, i)).find(det => det?.needs_comment)
      if (need && d.note.trim() === '') return { id: r.id, text: need.description ?? `Type a value for “${need.label}”.` }
    }
    return null
  }
  const go = (i: number) => { setPrompt(null); setPageId(rounds[i].id) }
  // A prompt moves focus to the note it asks for, once that page has rendered.
  useEffect(() => { if (prompt) noteRef.current?.focus() }, [prompt, pageId])
  const next = () => {
    const need = missingNote(last ? rounds : [q])
    if (need) {
      setPageId(need.id)
      setPrompt(need.text)
      return
    }
    if (!last) go(at + 1)
    else onSend(rounds.map(r => roundSend(r, drafts[r.id] ?? EMPTY)))
  }

  const words = pageWords(q.question, q.plain)
  const plainOptions = words.reworded ? q.plain?.options : undefined
  const labelId = `gate-q-${q.id}`
  const options = q.options ?? []
  const rows = options.map((opt, source) => ({ opt, source })).filter(row => !q.multi_select || hasGlyph(row.opt))
  const atCap = draft.picked.length >= MAX_SELECTIONS
  const pick = (source: number) => {
    if (!q.multi_select) return setDraft({ picked: draft.picked[0] === source ? [] : [source] })
    if (draft.picked.includes(source)) return setDraft({ picked: draft.picked.filter(n => n !== source) })
    if (!atCap) setDraft({ picked: [...draft.picked, source] })
  }

  return (
    <Box data-testid="gate-steps" sx={{ display: 'flex', flexDirection: 'column', gap: 1, minHeight: 0, flex: '1 1 auto' }}>
      <Box sx={{ flexShrink: 0, display: 'flex', alignItems: 'center', gap: 1.33 }}>
        <Typography variant="body2" sx={{ fontWeight: 600, color: 'text.primary' }}>Questions</Typography>
        {rounds.length > 1 && (
          <>
            <StepStrip done={rounds.map(r => touched(drafts[r.id] ?? EMPTY))} at={at} />
            <Typography variant="caption" data-testid="gate-step-count" aria-live="polite" sx={{ color: 'text.secondary', whiteSpace: 'nowrap' }}>
              {at + 1} of {rounds.length}
            </Typography>
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
            py: 1.25, px: 1.5,
            backgroundColor: 'background.paper',
            border: '1px solid', borderColor: 'divider', borderLeft: '3px solid', borderLeftColor: 'info.main', borderRadius: 1,
            display: 'flex', flexDirection: 'column', gap: 0.75,
          }}
        >
          <Typography
            id={labelId}
            tabIndex={-1}
            data-testid="gate-plain-headline"
            // The runner's own wording, on hover over a reworded headline.
            title={words.reworded ? q.question : undefined}
            sx={{
              fontWeight: 650, fontSize: '1rem', lineHeight: 1.35, color: 'text.primary', overflowWrap: 'anywhere',
              whiteSpace: words.reworded ? undefined : 'pre-wrap', cursor: words.reworded ? 'help' : undefined,
            }}
          >
            {words.headline}
            {/* The round's short name rides the headline's line instead of a row of its own. */}
            {nonBlank(q.header) && <Box component="span" sx={{ ml: 1, verticalAlign: '0.1em' }}><RoundHeaderChip header={q.header} /></Box>}
          </Typography>
          {words.why && (
            <Typography variant="body2" data-testid="gate-plain-why" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
              {words.why}
            </Typography>
          )}
          {rows.length > 0 && (
            <Box
              role={q.multi_select ? 'group' : 'radiogroup'}
              aria-labelledby={labelId}
              data-testid="gate-options"
              data-answer-shape={q.multi_select ? 'multi-select' : 'single'}
              sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}
            >
              {rows.map(({ opt, source }) => {
                const detail = optionDetail(q.option_details, source)
                const plain = plainOptions?.[source]
                const shown = nonBlank(plain?.short) ?? opt
                const description = optionDescription(opt, detail) ?? nonBlank(plain?.gist)
                const preview = nonBlank(detail?.preview)
                const checked = draft.picked.includes(source)
                const Control = q.multi_select ? Checkbox : Radio
                // The description and badge describe the option; its name stays the label alone.
                const descriptionId = description ? `${labelId}-desc-${source}` : undefined
                const recommendedId = showsRecommended(shown, detail) ? `${labelId}-rec-${source}` : undefined
                return (
                  <Box key={source} sx={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                    <FormControlLabel
                      data-testid="gate-option"
                      data-checked={checked ? 'true' : 'false'}
                      // The runner's label and the whole description, where the row cuts them.
                      title={[opt, description].filter(Boolean).join(' — ')}
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
                          sx={{ p: 0.75 }}
                        />
                      }
                      label={
                        <Box component="span" sx={OPTION_TEXT_SX}>
                          <Box component="span" sx={{ fontWeight: 600, flexShrink: 0, maxWidth: '100%', overflowWrap: 'anywhere' }}>{shown}</Box>
                          {recommendedId && <RecommendedBadge id={recommendedId} />}
                          {description && <Typography component="span" id={descriptionId} variant="body2" sx={OPTION_DESC_SX}>{description}</Typography>}
                        </Box>
                      }
                      sx={theme => ({
                        ...OPTION_ROW_SX,
                        borderColor: checked ? 'primary.main' : 'divider',
                        backgroundColor: checked ? alpha(theme.palette.primary.main, 0.12) : 'transparent',
                      })}
                    />
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
          <Button variant="outlined" disabled={busy || at === 0} onClick={() => go(at - 1)} sx={NAV_SX}>Back</Button>
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
            disabled={busy || (readOnly && last)}
            onClick={readOnly ? () => go(at + 1) : next}
            sx={NAV_SX}
          >
            {last ? (submitting ? 'Sending…' : 'Send') : 'Next'}
          </Button>
        )}
      </Box>
    </Box>
  )
}

const NAV_SX = {
  [PHONE]: { minHeight: 44 },
  // Hit-testable while disabled (see `GateForm`'s `BUTTON_SX`).
  '&.Mui-disabled': { pointerEvents: 'auto' },
} as const
