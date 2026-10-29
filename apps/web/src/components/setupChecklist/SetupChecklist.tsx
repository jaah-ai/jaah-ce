import { useId, useRef, useState, type JSX, type ReactNode } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Collapse from '@mui/material/Collapse'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { tapTargetSx } from '../../ui/listStyles'

/* THE CHECKLIST BODY both onboarding guides draw: the user's "Get started" (`onboarding/getStartedGuide.ts`)
 * and the org's "Set up your company" (`onboarding/companySetupGuide.tsx`). Presentation only — each guide
 * owns which steps exist and whether each is done, and the host (`onboarding/OnboardingWidget.tsx`) owns
 * when anything is drawn and the FRAME: its floating header carries the title, the "N of M" count and the ✕,
 * so this is only the meter and the rows (plus the one-line "could not save" a refused ✕ leaves). The ✕'s
 * single-shot state lives in `useChecklistDismiss` below, shared by the header that shows the button and
 * the body that says it failed.
 * Minimum words: a segmented meter plus rows of a glyph, a 2–4 word label and a one-word destination.
 * Two optional extras, used by the company card only: a step may carry a `status` beyond done/todo (with a
 * `statusNote` under its label), and `highlightNext` marks the one step to do next. A step with `expand`
 * opens its panel in place instead of linking away. `labelLinks` (the Get started card) makes each LABEL the
 * link to its step's page, done rows included, and drops the trailing destination link. Labels and notes WRAP;
 * nothing here truncates.
 */

/** A step's state. Absent, it is `done` ? done : todo — the Get started card never sets one. */
export type SetupChecklistStatus = 'done' | 'todo' | 'blocked' | 'in_progress' | 'failed'

/** What each state's glyph says to assistive tech (the glyph carries it; colour alone is invisible). */
export const STATUS_WORDS: Record<SetupChecklistStatus, string> = {
  done: 'Done',
  todo: 'Not started',
  blocked: 'Waiting',
  in_progress: 'In progress',
  failed: 'Needs attention',
}

export type SetupChecklistStep = {
  id: string
  /** Two to four words. */
  label: string
  /** Where the step is done; the card links there rather than duplicating the affordance. */
  to: string
  /** Link text naming the destination, one or two words. */
  cta: string
  done: boolean
  /** Replaces the link on an outstanding row whose destination the viewer cannot use. */
  note?: string
  /** A state beyond done/todo. `blocked` never links; its `note` stands where the link would be. */
  status?: SetupChecklistStatus
  /** Words under the label saying where the step stands ("Waiting for approval", an error). Wraps. */
  statusNote?: string
  /** An in-place panel: the row offers a `cta` toggle that opens it under the row, instead of a link. */
  expand?: ReactNode
}

/** A step's note: a muted status pill, not link-sized text. */
const NOTE_SX = {
  color: 'text.secondary',
  flex: '0 1 auto',
  px: 1,
  py: 0.25,
  border: 1,
  borderColor: 'divider',
  borderRadius: '10px',
  bgcolor: 'action.hover',
  lineHeight: 1.4,
  textAlign: 'right',
  overflowWrap: 'anywhere',
} as const

/** A step's state: its own `status`, else done/todo. */
export const statusOf = (step: SetupChecklistStep): SetupChecklistStatus => step.status ?? (step.done ? 'done' : 'todo')

/** Where the viewer acts: a blocked step and a viewer-note step never link; `in_progress`/`failed` link only when
 *  the caller left a destination (`to`) to offer. */
export function isStepActionable(step: SetupChecklistStep): boolean {
  return !step.done && step.note === undefined && step.to !== '' && statusOf(step) !== 'blocked'
}

/** The next ACTION: the first open step the viewer can act on here (a link or a panel); failing that, the first
 *  open step at all, so something is always marked while a guide is up. */
export function nextStepId(steps: readonly SetupChecklistStep[]): string | null {
  const outstanding = steps.filter(step => !step.done && statusOf(step) !== 'blocked')
  return (outstanding.find(step => step.note === undefined && step.to !== '') ?? outstanding[0])?.id ?? null
}

/**
 * The state glyph. SVG, not ✓/○ (rule single-homed in `ChatGlyph.tsx`): it must track the palette in both
 * themes. Sized in `em`.
 * CARRIES THE STATE IN WORDS via `role="img"` + name — colour and weight are invisible to assistive tech
 * (WCAG 1.4.1, 1.3.1). In place rather than an sr-only sibling (`SR_ONLY_SX` needs a positioned
 * ancestor). The meter stays `aria-hidden`; "N of M" already speaks it.
 */
function StepGlyph({ status, testIdPrefix }: { status: SetupChecklistStatus; testIdPrefix: string }): JSX.Element {
  const done = status === 'done'
  return (
    <Box
      component="svg"
      viewBox="0 0 16 16"
      role="img"
      aria-label={STATUS_WORDS[status]}
      focusable="false"
      data-testid={
        done ? `${testIdPrefix}-step-done` : status === 'todo' ? `${testIdPrefix}-step-todo` : `${testIdPrefix}-step-${status}`
      }
      sx={{ width: '1.15em', height: '1.15em', display: 'block', flex: '0 0 auto' }}
    >
      {done && (
        <>
          <circle cx="8" cy="8" r="7.25" fill="currentColor" />
          {/* Knocked out with the card's surface colour, not white, so it follows the theme. */}
          <path
            d="m4.75 8.25 2.1 2.1 4.4-4.4"
            fill="none"
            stroke="var(--mui-palette-background-paper)"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </>
      )}
      {status === 'todo' && <circle cx="8" cy="8" r="6.75" fill="none" stroke="currentColor" strokeWidth="1.4" />}
      {/* Dashed: not yours to start yet. */}
      {status === 'blocked' && (
        <circle cx="8" cy="8" r="6.75" fill="none" stroke="currentColor" strokeWidth="1.4" strokeDasharray="2.2 2.2" />
      )}
      {/* Half-filled: under way, nothing for the viewer to do. */}
      {status === 'in_progress' && (
        <>
          <circle cx="8" cy="8" r="6.75" fill="none" stroke="currentColor" strokeWidth="1.4" />
          <path d="M8 3.5a4.5 4.5 0 0 1 0 9z" fill="currentColor" />
        </>
      )}
      {status === 'failed' && (
        <>
          <circle cx="8" cy="8" r="7.25" fill="currentColor" />
          <path
            d="M8 4.5v4.25M8 11.1v.4"
            fill="none"
            stroke="var(--mui-palette-background-paper)"
            strokeWidth="1.8"
            strokeLinecap="round"
          />
        </>
      )}
    </Box>
  )
}

/** The glyph's colour per state. */
const GLYPH_COLOR: Record<SetupChecklistStatus, string> = {
  done: 'success.main',
  todo: 'text.disabled',
  blocked: 'text.disabled',
  in_progress: 'info.main',
  failed: 'error.main',
}

/**
 * The progress meter — SEGMENTED, not `LinearProgress`: the question is "how many are left", and a dropped
 * step simply removes a segment. `aria-hidden` ("N of M" is stated beside it).
 */
function ProgressMeter({
  total,
  done,
  testIdPrefix,
}: {
  total: number
  done: number
  testIdPrefix: string
}): JSX.Element {
  return (
    <Box
      aria-hidden="true"
      data-testid={`${testIdPrefix}-progress`}
      sx={{ display: 'flex', gap: 0.5, mb: 1.25 }}
    >
      {Array.from({ length: total }, (_, i) => (
        <Box
          key={i}
          sx={{
            flex: 1,
            height: 4,
            borderRadius: 2,
            bgcolor: i < done ? 'success.main' : 'divider',
            transition: theme => theme.transitions.create('background-color'),
          }}
        />
      ))}
    </Box>
  )
}

/** "N of M": the count the host's header states and the meter draws. */
export function checklistProgress(steps: readonly SetupChecklistStep[]): { done: number; total: number } {
  return { done: steps.filter(step => step.done).length, total: steps.length }
}

/** A guide's progress in its ONE reading, wherever it is drawn — the dock's caption and rail, the sidebar's Setup
 *  button, the More sheet's row and the More slot's badge: "1/5", or "Done" once every step is (never a "0%"
 *  beside the badge's "0/5"). */
export function progressReading({ done, total }: { done: number; total: number }): string {
  return total > 0 && done === total ? 'Done' : `${done}/${total}`
}

/** The same reading spoken: "1 of 5", "complete". Never the word "done": a control named "… done" collides with
 *  every page's own Done button in a by-name lookup. */
export function progressSpoken({ done, total }: { done: number; total: number }): string {
  return total > 0 && done === total ? 'complete' : `${done} of ${total}`
}

/**
 * The ✕'s state, lifted out of the body because the button now lives in the floating header.
 * SINGLE-SHOT while the write is open: `updatePreferences` PUTs the WHOLE document, so double-clicks race.
 * A refused write needs saying (`failed`): `updatePreferences` is optimistic and REVERTS (`auth.tsx`), so a
 * permanent action that silently came back would read as broken.
 */
export function useChecklistDismiss(onDismiss: () => Promise<boolean>): {
  dismissing: boolean
  failed: boolean
  dismiss: () => void
} {
  const [dismissing, setDismissing] = useState(false)
  const [failed, setFailed] = useState(false)
  /* A ref as well as state: two clicks inside one render would both read `dismissing === false`. */
  const open = useRef(false)
  return {
    dismissing,
    failed,
    dismiss: () => {
      if (open.current) return
      open.current = true
      setDismissing(true)
      void (async () => {
        setFailed(!(await onDismiss()))
        open.current = false
        setDismissing(false)
      })()
    },
  }
}

export function SetupChecklist({
  testIdPrefix,
  steps,
  dismissFailed = false,
  highlightNext = false,
  labelLinks = false,
}: {
  /** Every test id is `<prefix>-…`: `-progress`, `-dismiss-failed`, `-row-<id>`, `-link-<id>`, `-note-<id>`,
   *  `-status-<id>`, `-step-done`/`-step-todo`/`-step-<status>`, `-next`. The host's header adds `-count`
   *  and `-dismiss`. */
  testIdPrefix: string
  steps: readonly SetupChecklistStep[]
  /** The ✕'s write was refused (`useChecklistDismiss().failed`): say so above the meter. */
  dismissFailed?: boolean
  /** Mark the first step neither done nor blocked as the one to do next (`-next` test id, `aria-current`). */
  highlightNext?: boolean
  /** Each row's label IS its link (to `to`, done or not), and no trailing destination link is drawn. A row with
   *  a `note`, an `expand` panel or no `to` keeps a plain label. */
  labelLinks?: boolean
}): JSX.Element {
  /* One open panel at a time (a step's `expand`). */
  const [openId, setOpenId] = useState<string | null>(null)
  const panelIdBase = useId()

  const { done: doneCount } = checklistProgress(steps)
  const nextId = highlightNext ? nextStepId(steps) : null

  return (
    /* CONTENT-SIZED: no width of its own. The floating frame shrinks to the widest row (label + a modest gap +
       its link or note), so no row is stretched across empty space; a narrower viewport caps the frame and
       labels wrap — they never truncate. */
    <Box>
      {/* Not a `Banner`: an app-level alert would read as the PAGE failing. */}
      {dismissFailed && (
        <Typography
          variant="caption"
          role="status"
          color="error"
          data-testid={`${testIdPrefix}-dismiss-failed`}
          sx={{ display: 'block', mb: 0.75 }}
        >
          Could not save that — try again.
        </Typography>
      )}
      <ProgressMeter total={steps.length} done={doneCount} testIdPrefix={testIdPrefix} />
      <Stack component="ul" spacing={0.75} sx={{ listStyle: 'none', m: 0, p: 0 }}>
        {steps.map(step => {
          const { done } = step
          const status = statusOf(step)
          const isNext = step.id === nextId
          const expanded = openId === step.id && step.expand !== undefined
          const panelId = `${panelIdBase}-${step.id}`
          const note = !done ? (status === 'blocked' ? step.note ?? '' : step.note) : undefined
          const actionable = isStepActionable(step)
          const labelLink = labelLinks && note === undefined && step.to !== '' && step.expand === undefined
          return (
            <Box
              component="li"
              key={step.id}
              data-testid={`${testIdPrefix}-row-${step.id}`}
              data-next={isNext ? 'true' : undefined}
              aria-current={isNext ? 'step' : undefined}
              sx={{ minWidth: 0 }}
            >
              <Box
                data-testid={isNext ? `${testIdPrefix}-next` : undefined}
                sx={
                  highlightNext
                    ? {
                        display: 'flex',
                        alignItems: 'center',
                        gap: 1,
                        minWidth: 0,
                        /* Every row carries the same inset, so the highlight moves no text. */
                        mx: -0.75,
                        px: 0.75,
                        py: 0.5,
                        borderRadius: 1,
                        borderLeft: 3,
                        borderColor: isNext ? 'primary.main' : 'transparent',
                        bgcolor: isNext ? 'action.hover' : 'transparent',
                      }
                    : { display: 'flex', alignItems: 'center', gap: 1, minWidth: 0 }
                }
              >
                <Box
                  component="span"
                  sx={{ display: 'flex', color: GLYPH_COLOR[status] }}
                >
                  <StepGlyph status={status} testIdPrefix={testIdPrefix} />
                </Box>
                {/* `mr` widens the label-to-link gap to 16px: the widest row sets the frame's width, and its link
                    must still read as that row's, not float off on its own. The label keeps its longest word
                    (no `minWidth: 0`, `break-word` below): a long note pill must wrap, never crush the label to a
                    letter per line. */}
                <Box sx={{ flex: 1, mr: 1 }}>
                  {labelLink ? (
                    <Link
                      component={RouterLink}
                      to={step.to}
                      data-testid={`${testIdPrefix}-link-${step.id}`}
                      variant="body2"
                      underline="hover"
                      sx={theme => ({
                        ...tapTargetSx(theme),
                        display: 'inline-flex',
                        alignItems: 'center',
                        // A finished row stays a link, quieter: it reads as done, not as a call to act.
                        color: done ? 'text.secondary' : 'primary.main',
                        fontWeight: done ? 400 : 500,
                        overflowWrap: 'break-word',
                      })}
                    >
                      {step.label}
                    </Link>
                  ) : (
                    <Typography
                      variant="body2"
                      sx={{
                        color: done || status === 'blocked' ? 'text.secondary' : 'text.primary',
                        // Not strikethrough: struck lines read as cancelled, not completed.
                        fontWeight: done ? 400 : isNext ? 600 : 500,
                        overflowWrap: 'break-word',
                      }}
                    >
                      {step.label}
                    </Typography>
                  )}
                  {step.statusNote !== undefined && (
                    <Typography
                      variant="caption"
                      data-testid={`${testIdPrefix}-status-${step.id}`}
                      sx={{ color: status === 'failed' ? 'error.main' : 'text.secondary', display: 'block', overflowWrap: 'anywhere' }}
                    >
                      {step.statusNote}
                    </Typography>
                  )}
                </Box>
                {/* A finished row keeps NO link — it could only offer an undo. A note stands where a link the
                    viewer cannot use would be: a muted pill, smaller than a link, so it reads as where the step
                    stands, never as a dead link. It may wrap, never truncate. */}
                {note !== undefined && note !== '' && (
                  <Typography
                    variant="caption"
                    data-testid={`${testIdPrefix}-note-${step.id}`}
                    sx={NOTE_SX}
                  >
                    {note}
                  </Typography>
                )}
                {actionable && step.expand !== undefined && (
                  <ButtonBase
                    data-testid={`${testIdPrefix}-link-${step.id}`}
                    aria-expanded={expanded}
                    aria-controls={panelId}
                    onClick={() => setOpenId(expanded ? null : step.id)}
                    sx={theme => ({
                      ...tapTargetSx(theme),
                      ...theme.typography.body2,
                      flex: '0 0 auto',
                      fontWeight: 500,
                      color: 'primary.main',
                      gap: 0.25,
                      '&:hover': { textDecoration: 'underline' },
                    })}
                  >
                    {step.cta}
                    <Box component="span" aria-hidden="true" sx={{ fontSize: '1em', lineHeight: 1 }}>
                      {expanded ? '▴' : '▾'}
                    </Box>
                  </ButtonBase>
                )}
                {actionable && step.expand === undefined && !labelLink && (
                  <Link
                    component={RouterLink}
                    to={step.to}
                    data-testid={`${testIdPrefix}-link-${step.id}`}
                    variant="body2"
                    underline="hover"
                    sx={theme => ({
                      ...tapTargetSx(theme),
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: 0.25,
                      flex: '0 0 auto',
                      fontWeight: 500,
                    })}
                  >
                    {step.cta}
                    {/* Full size, baseline-nudged: at 0.85em it read as a stray artifact. */}
                    <Box
                      component="span"
                      aria-hidden="true"
                      sx={{ fontSize: '1em', lineHeight: 1, position: 'relative', top: '0.05em' }}
                    >
                      →
                    </Box>
                  </Link>
                )}
              </Box>
              {/* The region stays mounted, so the toggle's `aria-controls` always names an element; its content
                  mounts only once opened (a panel may fetch). */}
              {step.expand !== undefined && (
                <Collapse in={expanded}>
                  <Box id={panelId} sx={{ pt: 0.75, pb: 0.5, pl: highlightNext ? 0 : 3 }}>
                    {expanded && step.expand}
                  </Box>
                </Collapse>
              )}
            </Box>
          )
        })}
      </Stack>
    </Box>
  )
}

export default SetupChecklist
