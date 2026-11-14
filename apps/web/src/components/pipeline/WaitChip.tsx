/* WHY A STEP WAITS ON THE TASKS PIPELINE (B4): the strip's chip (the first blocking reason, ≤ 2 words) opening the
 * full list on hover, focus or a tap; the same list above a step's waiting items; and the Freeze now banner (R7.6).
 * The words are `lib/waitReasons.ts`'s; the reasons are the view's (`ViewStep.reasons`). */
import { useEffect, useRef, useState, type JSX } from 'react'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import { MINUTE_MS, useNow } from '../../hooks/useNow'
import Banner from '../../ui/Banner'
import Tip, { WaitReasons, closeTapTips, tapClosers } from '../../ui/Tip'
import { FONT_SIZE, LINE_HEIGHT, RADIUS, SIZE } from '../../theme'
import { PHONE } from '../../ui/listStyles'
import type { PipelineView, ViewStep, WaitReason } from '../../lib/pipelineBoard'
import { allWaitLines, firstBlocking, frozenNowText, hasDeadline, waitChip } from '../../lib/waitReasons'

/* The tap target: the phone's 44px floor, transparent; the visible chip is its inner pill (`PILL_SX`). */
const CHIP_SX = {
  borderRadius: RADIUS.md,
  '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main' },
  '&:hover > span': { bgcolor: 'action.hover' },
  [PHONE]: { minHeight: SIZE.tapTarget },
} as const
const PILL_SX = {
  px: 1,
  py: 0.5,
  border: '1px solid',
  borderColor: 'warning.main',
  borderRadius: RADIUS.round,
  fontSize: FONT_SIZE.xs,
  lineHeight: LINE_HEIGHT.snug,
  color: 'text.primary',
  whiteSpace: 'nowrap',
} as const

/** A release's states that wait on the step (never one running, failed or finished). */
const OPEN_WAITING: ReadonlySet<string> = new Set(['awaiting_approval', 'scheduled', 'queued'])

/** Whether a step has something waiting that its reasons speak for: items before it, or a release not yet running. */
export function stepWaits(step: ViewStep): boolean {
  if (step.waiting > 0) return true
  const state = step.release?.state
  return step.action === 'release' && state !== undefined && OPEN_WAITING.has(state)
}

/** The step's chip: its first blocking reason in one or two words; pressing it (a tap on a phone) opens the list. */
export function WaitChip({ reasons, testId = 'step-wait' }: {
  reasons: readonly WaitReason[] | undefined
  testId?: string
}): JSX.Element | null {
  const [open, setOpen] = useState(false)
  // A countdown re-draws each minute (the view re-reads too: `Tasks.tsx`).
  const now = new Date(useNow(hasDeadline(reasons) ? MINUTE_MS : null))
  // Opened by a press (a tap): it stays until a press closes it; one opened by hover closes as the pointer leaves.
  const pinned = useRef(false)
  const box = useRef<HTMLButtonElement | null>(null)
  const close = useRef(() => { pinned.current = false; setOpen(false) })
  useEffect(() => {
    const closer = close.current
    tapClosers.add(closer)
    return () => { tapClosers.delete(closer) }
  }, [])
  // A press elsewhere closes it (a touch screen sends no mouse leave).
  useEffect(() => {
    if (!open) return undefined
    const away = (event: PointerEvent) => { if (!box.current?.contains(event.target as Node)) close.current() }
    document.addEventListener('pointerdown', away, true)
    return () => document.removeEventListener('pointerdown', away, true)
  }, [open])
  const first = firstBlocking(reasons)
  if (!reasons || !first) return null
  const label = waitChip(first, now)
  return (
    <Tip
      title={<WaitReasons lines={allWaitLines(reasons, now)} />}
      open={open}
      onOpen={() => setOpen(true)}
      // A tapped (pinned) list closes only on a press, Escape or blur: never on MUI's touch or mouse leave timers.
      onClose={(event) => { if (!pinned.current || event.type === 'keydown' || event.type === 'blur') close.current() }}
      disableTouchListener
    >
      <ButtonBase
        ref={box}
        data-testid={testId}
        aria-label={`Why it waits: ${label}`}
        onClick={() => {
          if (pinned.current) { close.current(); return }
          closeTapTips()
          pinned.current = true
          setOpen(true)
        }}
        sx={CHIP_SX}
      >
        <Box component="span" sx={PILL_SX}>{label}</Box>
      </ButtonBase>
    </Tip>
  )
}

/** Atop a step's waiting items: the same chip, so the reasons open from the list too (no prose on the page). */
export function WaitNote({ reasons }: { reasons: readonly WaitReason[] | undefined }): JSX.Element | null {
  if (!reasons || !firstBlocking(reasons)) return null
  return (
    <Box data-testid="wait-note" sx={{ display: 'flex', alignItems: 'center', gap: 1, px: 1.5, py: 0.5 }}>
      <Box component="span" sx={{ fontSize: FONT_SIZE.sm, color: 'text.secondary' }}>Waiting on</Box>
      <WaitChip reasons={reasons} testId="wait-note-chip" />
    </Box>
  )
}

/** "Frozen now" while the project's Freeze now is on, naming who and since when (R7.6); nothing when off. */
export function FrozenNowBanner({ view }: { view: PipelineView | undefined }): JSX.Element | null {
  const text = view ? frozenNowText(view.steps.flatMap(s => s.reasons ?? [])) : null
  return text ? <Banner severity="warning" testId="frozen-now-banner">{text}</Banner> : null
}

/** The step out of `stageId` (the next stage's), as the view reports it. */
export function stepFrom(view: PipelineView | undefined, stageId: string | null): ViewStep | undefined {
  if (!view || stageId === null) return undefined
  const next = view.stages[view.stages.findIndex(s => s.id === stageId) + 1]
  return next ? view.steps.find(s => s.to_stage_id === next.id) : undefined
}
