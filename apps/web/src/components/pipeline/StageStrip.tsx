/* THE STAGE STRIP — the pipeline page's top band, above search (AC-4/5/6/22): ONE row. Each stage is a one-line block —
 * its name, its count in bold (a stage with a live version shows that version and its age instead), then a red coin
 * for its failed tasks and an amber one for its rows asking (`ui/CountPill`, their words on hover), then its newest
 * arrival's chip (`ArrivalChip.tsx`) and, where the server offers it, Roll back (one confirm); pressing it picks
 * the stage (pressing it again keeps it picked). Between two
 * blocks a step is a small icon in the step's colour (`STEP_ACCENT`), its action word on hover. A lane step (Merge, Release) is ONE mark (`StepGlyph.tsx`):
 * its icon turns while the lane works, the count in motion on its coin; pressing the Merge mark filters the list to
 * what merges. On the Release step the mark is the release's own (`ReleaseStep.tsx`; without it, the turning mark),
 * and pressing it picks the step (`?only=release` on its source stage), as any pick. Start and Move follow their icon with their counts as coins
 * (failed red, waiting grey). The buckets (Not started,
 * Done, Archived) are the toolbar's Show page, not here. A step with something waiting ends with its wait chip — the
 * first blocking reason in one or two words, the full list on hover or a tap (`WaitChip.tsx`, B4). Setup never shows here: only a gear to Project settings ›
 * Pipeline (AC-13), at the row's end.
 * On the phone — or in a box too narrow for one row, as beside an open task — the row scrolls sideways: the blocks,
 * a step's counts after its source block, a lane's mark while it works, the Release mark, and the gear. On
 * the phone every target is at least 44px, and the picked block scrolls into view (a project can open on its last stage).
 * Each scrolled edge shows whole pieces only, with a chevron where more lies (`edgeMaskSx`, as the page tabs do): a
 * stage the edge would cut ("Produ") is hidden whole until scrolled to. */
import { useEffect, useState, type ReactNode } from 'react'
import { useElementWidth } from '../../hooks/useHasWidth'
import Button from '@mui/material/Button'
import ButtonBase from '@mui/material/ButtonBase'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import { Link as RouterLink } from 'react-router-dom'
import { compactAge } from '../../lib/relativeTime'
import { ACTION_LABEL, STEP_ACCENT, type StageHealth, type StepAction, type TargetKind } from '../../lib/pipeline'
import { ApiError } from '../../api'
import {
  RELEASE_ONLY, rollbackStage, type PipelineView, type StageArrival, type StageLive, type ViewStage, type ViewStep,
} from '../../lib/pipelineBoard'
import { danger, useConfirm } from '../ConfirmDialog'
import { edgeMaskSx, markEdgeCuts } from '../Tabs'
import { PHONE } from '../../ui/listStyles'
import { usePhone } from '../../hooks/usePhone'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT, RADIUS, SIZE, tint } from '../../theme'
import Tip from '../../ui/Tip'
import CountPill, { type CountTone } from '../../ui/CountPill'
import { ArrowRightIcon, CogIcon, MergeIcon, PlayIcon, ReleaseIcon } from '../../ui/icons'
import StepGlyph from './StepGlyph'
import HealthDot, { NO_ADDRESS } from './HealthDot'
import { WaitChip, stepWaits } from './WaitChip'
import ArrivalChip from './ArrivalChip'
import { refusal } from './DeployDialog'
import { firstBlocking } from '../../lib/waitReasons'

/** What the strip has picked: a stage id or a bucket, and an in-stage group a step's count chose. */
export type StripPick = { stage: string | null; only: string | null }

/** One count on a step: its words, the group it filters to, and its coin (`count`, `tone`, turning while in flight);
 *  approvals, a ratio, have no coin (`count` null) and show their words. */
type StepCount = {
  key: string; label: string; stage: string; only: string | null; count: number | null; tone: CountTone; spinning?: boolean
}

const ROW_SX = { display: 'flex', alignItems: 'stretch', gap: 0.5, pt: 1 } as const
/* The compact row (phone, or a box too narrow for one row): it scrolls sideways, never wraps. */
const COMPACT_ROW_SX = { display: 'flex', alignItems: 'center', gap: 1, pt: 1, overflowX: 'auto' } as const
/* The scrolling row's whole-piece edges and their chevrons, on the strip's host. */
const COMPACT_HOST_SX = edgeMaskSx('& > nav')
/* What the row lays out: its direct pieces, a stage's group (`display: contents`) read as the pieces it holds. */
const GROUP_ATTR = 'data-strip-group'
const rowPieces = (row: HTMLElement): Element[] =>
  [...row.children].flatMap(child => (child.hasAttribute(GROUP_ATTR) ? [...child.children] : [child]))
/** One row's floor per block, per step and for the gear, in px: below their sum the strip goes compact. */
const STAGE_MIN_PX = 104
const STEP_MIN_PX = 40
const GEAR_PX = 40

const STAGE_SX = {
  flex: '1 1 0',
  minWidth: 0,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'flex-start',
  flexWrap: 'wrap',
  columnGap: 1,
  px: 1.5,
  py: 0.5,
  textAlign: 'left',
  border: '1px solid',
  borderColor: 'divider',
  borderRadius: RADIUS.md,
  bgcolor: 'background.paper',
  minHeight: SIZE.thumb,
  '&:hover': { borderColor: 'text.secondary' },
  '&[aria-pressed="true"]': { borderColor: 'primary.main', bgcolor: tint('primary', 0.08) },
  '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main' },
  [PHONE]: { minHeight: SIZE.tapTarget },
} as const
/* In the scrolling row a block keeps its line whole: it takes its content's width and never wraps. */
const COMPACT_STAGE_SX = { ...STAGE_SX, flex: '0 0 auto', flexWrap: 'nowrap', whiteSpace: 'nowrap', alignSelf: 'stretch' } as const
/* A block with Roll back: the frame is the block (border, pick ring), holding the pick target and the button — never a
   button inside a button. In one row the button wraps below the name when the block is narrow. */
const FRAME_EXTRA_SX = {
  px: 0.5,
  rowGap: 0.5,
  columnGap: 0.5,
  '&:has(> [aria-pressed="true"])': { borderColor: 'primary.main', bgcolor: tint('primary', 0.08) },
} as const
const FRAMED_PICK_SX = {
  flex: '1 1 auto',
  minWidth: 0,
  alignSelf: 'stretch',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'flex-start',
  flexWrap: 'wrap',
  columnGap: 1,
  px: 1,
  textAlign: 'left',
  borderRadius: RADIUS.sm,
  '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main' },
} as const
const COMPACT_FRAMED_PICK_SX = { ...FRAMED_PICK_SX, flexWrap: 'nowrap', whiteSpace: 'nowrap' } as const
/* In the scrolling row the button sits below the name: the block stays as narrow as its line. */
const COMPACT_FRAME_EXTRA_SX = { ...FRAME_EXTRA_SX, flexDirection: 'column', alignItems: 'flex-start' } as const
const ROLLBACK_SX = { flex: '0 0 auto', whiteSpace: 'nowrap', [PHONE]: { minHeight: SIZE.tapTarget } } as const

/** What a refused rollback says. */
function rollbackRefusal(err: unknown, fallback: string): string {
  if (err instanceof ApiError && err.status === 409 && (err.detail as { code?: string } | null)?.code === 'not_offered') {
    return 'Roll back is no longer offered. Reload.'
  }
  return refusal(err, fallback)
}

/* Names stay whole at every width (AC-4): they wrap, never ellipsise. */
const STAGE_NAME_SX = {
  fontSize: FONT_SIZE.sm,
  fontWeight: FONT_WEIGHT.semibold,
  lineHeight: LINE_HEIGHT.snug,
  color: 'text.secondary',
  overflowWrap: 'anywhere',
} as const
/* The dot and the name as one piece: the dot never wraps away from the name it marks, and the name still wraps whole. */
const DOT_NAME_SX = { display: 'inline-flex', alignItems: 'center', gap: 0.5, minWidth: 0 } as const
const STAGE_COUNT_SX = { fontSize: FONT_SIZE.md, fontWeight: FONT_WEIGHT.bold, lineHeight: LINE_HEIGHT.snug } as const
const STAGE_META_SX = { fontSize: FONT_SIZE.xs, lineHeight: LINE_HEIGHT.snug, color: 'text.secondary' } as const

/* A step: its icon, then its counts and the Release slot, on one line between two blocks. */
const STEP_SX = {
  flex: '0 0 auto',
  display: 'flex',
  alignItems: 'center',
  gap: 0.5,
  px: 0.5,
} as const
const COMPACT_STEP_SX = { ...STEP_SX, flexWrap: 'nowrap', whiteSpace: 'nowrap', px: 0 } as const
const STEP_ICON_SX = { display: 'inline-flex', alignItems: 'center', color: 'text.secondary', fontSize: FONT_SIZE.md } as const

/* A step's count: its coin in a round target; pressed, ringed in the primary colour. */
const COUNT_SX = {
  p: 0.5,
  borderRadius: RADIUS.round,
  fontSize: FONT_SIZE.xs,
  lineHeight: LINE_HEIGHT.snug,
  color: 'text.secondary',
  whiteSpace: 'nowrap',
  '&:hover': { bgcolor: 'action.hover' },
  '&[aria-pressed="true"]': { outline: '2px solid', outlineColor: 'primary.main' },
  '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main' },
  [PHONE]: { minHeight: SIZE.tapTarget, minWidth: SIZE.tapTarget },
} as const

const GEAR_SX = { flex: '0 0 auto', alignSelf: 'center', color: 'text.secondary', [PHONE]: { width: SIZE.tapTarget, height: SIZE.tapTarget } } as const

const STEP_ICON: Record<StepAction, typeof PlayIcon> = {
  start: PlayIcon, start_merge: MergeIcon, merge: MergeIcon, release: ReleaseIcon, move: ArrowRightIcon,
}

/** A step's icon colour at rest: its accent, else grey. */
const stepColor = (action: StepAction): string => {
  const accent = STEP_ACCENT[action]
  return accent ? `${accent}.main` : 'text.secondary'
}

/** A lane step: Merge (and Start and merge) or Release — drawn as one mark, never icon plus coins. */
export const isLane = (action: StepAction): boolean => action === 'merge' || action === 'start_merge' || action === 'release'

/** A lane's mark: what is in motion, the filter pressing it picks. Failed and waiting work is not the lane's to show
 *  (a failed merge is a retry: its task reads Working on its stage, `board.py::view`). Null while nothing moves. */
export function laneFlight(step: ViewStep, source: ViewStage): StepCount | null {
  if (!isLane(step.action) || step.in_flight <= 0) return null
  return step.action === 'release'
    ? { key: 'flight', label: `${step.in_flight} releasing`, stage: source.id, only: RELEASE_ONLY, count: step.in_flight, tone: 'info' }
    : { key: 'flight', label: `${step.in_flight} merging`, stage: source.id, only: 'merging', count: step.in_flight, tone: 'info' }
}

/** A Start or Move step's counts, each a filter: what is in flight, failed, waiting, and approvals; a lane step's
 *  approvals only (the release's are the Release step's list's). */
export function stepCounts(step: ViewStep, source: ViewStage): StepCount[] {
  const out: StepCount[] = []
  if (step.action === 'release') return out
  if (!isLane(step.action)) {
    if (step.in_flight > 0) {
      out.push({ key: 'flight', label: `${step.in_flight} merging`, stage: source.id, only: 'merging',
        count: step.in_flight, tone: 'info', spinning: true })
    }
    if (step.failed > 0) {
      out.push({ key: 'failed', label: `${step.failed} failed`, stage: source.id, only: 'failed', count: step.failed, tone: 'error' })
    }
    if (step.waiting > 0) {
      out.push({ key: 'waiting', label: `${step.waiting} waiting`, stage: source.id, only: 'waiting', count: step.waiting, tone: 'neutral' })
    }
  }
  // An older server without `reasons`: approvals as a count. With them, the step's wait chip names it (`WaitChip`).
  if (step.reasons === undefined && step.approvals.need > 0 && step.waiting > 0) {
    out.push({ key: 'approvals', label: `${step.approvals.have}/${step.approvals.need} approvals`, stage: source.id,
      only: 'waiting', count: null, tone: 'neutral' })
  }
  return out
}

/** A coin on a block; its words are the hover and, as the anchor's label, what a screen reader says. */
function BlockCoin({ count, tone, words, testId }: { count: number; tone: CountTone; words: string; testId: string }) {
  if (count <= 0) return null
  return (
    <Tip title={words} describeChild={false}>
      <Box component="span" role="img" data-testid={testId} sx={{ display: 'inline-flex' }}><CountPill count={count} tone={tone} /></Box>
    </Tip>
  )
}

/** A block's one line: its health dot (a stage with an address, `HealthDot`), name, the live version and its age (else
 *  the count), then its failed and asking coins. A stage that deploys but has no address says so on its name's hover. */
function BlockLine({ name, count, failed = 0, asking = 0, live = null, health = null, target = null, arrival = null }: {
  name: string; count: number; failed?: number; asking?: number; live?: StageLive | null
  health?: StageHealth | null; target?: TargetKind | null; arrival?: StageArrival | null
}) {
  const nameSpan = <Box component="span" sx={STAGE_NAME_SX}>{name}</Box>
  return (
    <>
      {health ? (
        <Box component="span" sx={DOT_NAME_SX}><HealthDot health={health} target={target} />{nameSpan}</Box>
      ) : target ? (
        <Tip title={NO_ADDRESS} describeChild={false}>
          <Box component="span" data-testid="stage-no-address" sx={STAGE_NAME_SX}>{name}</Box>
        </Tip>
      ) : nameSpan}
      {live && live.version !== '' ? (
        <>
          <Box component="span" data-testid="stage-version" sx={STAGE_COUNT_SX}>{live.version}</Box>
          {live.since && <Box component="span" sx={STAGE_META_SX}>{compactAge(live.since)}</Box>}
        </>
      ) : (
        <Box component="span" data-testid="stage-count" sx={STAGE_COUNT_SX}>{count}</Box>
      )}
      <BlockCoin count={failed} tone="error" words={`${failed} failed`} testId="stage-failed" />
      <BlockCoin count={asking} tone="warning" words={`${asking} to answer`} testId="stage-asking" />
      <ArrivalChip arrival={arrival} />
    </>
  )
}

export default function StageStrip({
  view,
  pick,
  onPick,
  settingsHref,
  releaseSlot,
  projectId,
  onRolledBack,
}: {
  view: PipelineView
  pick: StripPick
  /** Pick a stage or a step's count. */
  onPick: (next: StripPick) => void
  /** Project settings › Pipeline; absent for a viewer who cannot open them. */
  settingsHref?: string
  /** The Release step's own mark (`ReleaseStep.tsx`), drawn in place of its icon. */
  releaseSlot?: (step: ViewStep, from: ViewStage) => ReactNode
  /** The project the strip draws: with it, a stage whose arrival may be rolled back offers Roll back. */
  projectId?: number
  /** After a rollback landed: re-read the board. */
  onRolledBack?: () => void
}) {
  const [confirm, confirmDialog] = useConfirm()
  /* ONE CONFIRM naming the version it reverts; the write runs inside it, so a refusal reads there. */
  const rollBack = (stage: ViewStage, arrival: StageArrival) => {
    if (projectId === undefined) return
    void confirm(danger('Roll back', {
      title: `Roll ${stage.name} back to ${arrival.rollback_to_version ?? 'the previous version'}?`,
      message: `Reverts ${arrival.version ?? arrival.sha.slice(0, 7)} on ${stage.name}.`,
      run: { action: () => rollbackStage(projectId, stage.id), failed: 'Could not roll back', busyLabel: 'Rolling back…',
        mapError: rollbackRefusal },
    })).then(done => { if (done) onRolledBack?.() })
  }
  /* ONE ROW NEEDS ITS ROOM: each block a short name's width and each step its icon's; narrower, the scrolling row. */
  const [box, setBox] = useState<HTMLElement | null>(null)
  const width = useElementWidth(box)
  const rowNeeds = view.stages.length * STAGE_MIN_PX + Math.max(0, view.stages.length - 1) * STEP_MIN_PX + GEAR_PX
  const phone = usePhone({ noSsr: true })
  const compact = phone || (width !== null && width < rowNeeds)
  const pickOf = (stage: string, only: string | null = null) => onPick({ stage, only })
  /* The scrolling row brings the picked block into view: a project the rail opens on Production starts there. */
  useEffect(() => {
    if (!compact || !box || pick.stage === null) return
    const picked = box.querySelector<HTMLElement>(`[data-stage-id="${CSS.escape(pick.stage)}"]`)
    if (!picked) return
    const left = picked.getBoundingClientRect().left - box.getBoundingClientRect().left + box.scrollLeft
    if (left < box.scrollLeft || left + picked.offsetWidth > box.scrollLeft + box.clientWidth) box.scrollLeft = left
  }, [compact, box, pick.stage])
  /* Whole pieces at each scrolled edge, and the chevron toward more (`markEdgeCuts`). */
  const [host, setHost] = useState<HTMLElement | null>(null)
  useEffect(() => {
    if (!compact || !host || !box) return undefined
    const mark = () => markEdgeCuts(host, box, rowPieces(box))
    mark()
    box.addEventListener('scroll', mark, { passive: true })
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(mark)
    observer?.observe(box)
    // A piece that grows inside the row (a run state landing, a wait chip) changes the scroll width, not the row's size.
    rowPieces(box).forEach(piece => observer?.observe(piece))
    return () => {
      box.removeEventListener('scroll', mark)
      observer?.disconnect()
    }
  }, [compact, host, box, view])
  const countButton = (count: StepCount) => (
    <Tip key={count.key} title={count.count === null ? undefined : count.label} describeChild={false}>
      <ButtonBase
        data-testid={`step-count-${count.key}`}
        aria-label={count.label}
        aria-pressed={pick.stage === count.stage && pick.only === count.only}
        onClick={() => pickOf(count.stage, count.only)}
        sx={COUNT_SX}
      >
        {count.count === null ? count.label : <CountPill count={count.count} tone={count.tone} spinning={count.spinning} />}
      </ButtonBase>
    </Tip>
  )
  /* A lane's one mark: pressing it picks what the lane moves; idle, its icon alone, its word on hover. */
  const laneMark = (step: ViewStep, source: ViewStage, Icon: typeof PlayIcon) => {
    const flight = laneFlight(step, source)
    if (!flight) return null
    return (
      <Tip title={flight.label} describeChild={false}>
        <ButtonBase
          data-testid="step-lane"
          aria-label={flight.label}
          aria-pressed={pick.stage === flight.stage && pick.only === flight.only}
          onClick={() => pickOf(flight.stage, flight.only)}
          sx={COUNT_SX}
        >
          <StepGlyph icon={<Icon />} active count={flight.count ?? 0} />
        </ButtonBase>
      </Tip>
    )
  }
  const stepAt = (index: number) => view.steps.find(s => s.to_stage_id === view.stages[index + 1]?.id)
  return (
    <Box ref={setHost} sx={compact ? COMPACT_HOST_SX : undefined}>
      <Box ref={setBox} data-testid="stage-strip" data-compact={compact} component="nav" aria-label="Pipeline"
        sx={compact ? COMPACT_ROW_SX : ROW_SX}>
        {view.stages.map((stage, index) => {
          const out = stepAt(index)
          const slot = out?.action === 'release' && releaseSlot ? releaseSlot(out, stage) : null
          const counts = out ? stepCounts(out, stage) : []
          const StepIcon = out ? STEP_ICON[out.action] : null
          const lane = !slot && out && StepIcon ? laneMark(out, stage, StepIcon) : null
          const wait = out && stepWaits(out) && firstBlocking(out.reasons) ? <WaitChip reasons={out.reasons} /> : null
          const arrival = stage.arrival ?? null
          const rollback = projectId !== undefined && arrival?.rollback_allowed ? arrival : null
          const blockSx = compact ? COMPACT_STAGE_SX : STAGE_SX
          const block = (
            <ButtonBase
              data-testid="stage-block"
              data-stage={stage.name}
              data-stage-id={rollback ? undefined : stage.id}
              aria-pressed={pick.stage === stage.id && pick.only === null}
              onClick={() => pickOf(stage.id)}
              sx={rollback ? (compact ? COMPACT_FRAMED_PICK_SX : FRAMED_PICK_SX) : blockSx}
            >
              <BlockLine name={stage.name} count={stage.count} failed={stage.failed} asking={stage.asking} live={stage.live}
                health={stage.health} target={stage.target} arrival={arrival} />
            </ButtonBase>
          )
          return (
            <Box key={stage.id} {...{ [GROUP_ATTR]: '' }} sx={{ display: 'contents' }}>
              {rollback ? (
                <Box data-testid="stage-frame" data-stage-id={stage.id} sx={{ ...blockSx, ...(compact ? COMPACT_FRAME_EXTRA_SX : FRAME_EXTRA_SX) }}>
                  {block}
                  <Button size="small" color="error" variant="outlined" data-testid="stage-rollback" sx={ROLLBACK_SX}
                    onClick={() => rollBack(stage, rollback)}>Roll back</Button>
                </Box>
              ) : block}
              {out && StepIcon && (!compact || slot || lane || counts.length > 0 || wait) && (
                <Box data-testid="step-arrow" data-action={out.action} sx={compact ? COMPACT_STEP_SX : STEP_SX}>
                  {slot ?? lane}
                  {!compact && !slot && !lane && (
                    <Tip title={ACTION_LABEL[out.action]} describeChild={false}>
                      <Box component="span" role="img" sx={{ ...STEP_ICON_SX, color: stepColor(out.action) }}><StepIcon /></Box>
                    </Tip>
                  )}
                  {counts.map(countButton)}
                  {wait}
                </Box>
              )}
            </Box>
          )
        })}
        {settingsHref && (
          <Tip title="Pipeline settings" describeChild={false}>
            <IconButton size="small" component={RouterLink} to={settingsHref} data-testid="pipeline-settings-link" sx={GEAR_SX}>
              <CogIcon />
            </IconButton>
          </Tip>
        )}
        {confirmDialog}
      </Box>
    </Box>
  )
}

/* Whether the last totals this browser saw had stages: an org with none never holds a band it would then drop. Per
   viewer only, so a failed read or a blocked storage falls back to holding the row. */
const TOTALS_SHOWN_KEY = 'pipeline.totals-shown'
function totalsShownLastTime(): boolean {
  try { return localStorage.getItem(TOTALS_SHOWN_KEY) !== '0' } catch { return true }
}
function rememberTotalsShown(shown: boolean) {
  try { localStorage.setItem(TOTALS_SHOWN_KEY, shown ? '1' : '0') } catch { /* per-viewer convenience only */ }
}

/** The phone row's stand-in while the totals load: one blank block at the row's height, so the header below never
 *  moves when they land. */
function TotalsPlaceholder() {
  return (
    <Box data-testid="stage-totals-placeholder" aria-hidden sx={COMPACT_ROW_SX}>
      <Box sx={{ ...COMPACT_STAGE_SX, width: STAGE_MIN_PX, borderColor: 'transparent', bgcolor: 'action.hover', '&:hover': {} }} />
    </Box>
  )
}

/** All projects (secondary, D7): the stage totals by name. Pressing one picks that stage across projects (`onPick`,
 *  its name); without `onPick` the row is read-only. Like the strip, on the phone or
 *  in a box too narrow for one row every block keeps its line whole and the row scrolls sideways — never some blocks on
 *  one line and others wrapped onto two. */
export function StageTotals({ totals, pick = null, onPick, unfiltered = true }: {
  /** Undefined while the totals load: on the phone the row's height is held meanwhile (`TotalsPlaceholder`). */
  totals: { name: string; count: number }[] | undefined
  /** The picked stage's name (any case), or null. */
  pick?: string | null
  onPick?: (name: string) => void
  /** Whether no filter narrowed these totals: only such an empty answer means the org has no stages. */
  unfiltered?: boolean
}) {
  const [box, setBox] = useState<HTMLElement | null>(null)
  const width = useElementWidth(box)
  const phone = usePhone({ noSsr: true })
  useEffect(() => {
    if (totals && (totals.length > 0 || unfiltered)) rememberTotalsShown(totals.length > 0)
  }, [totals, unfiltered])
  if (totals === undefined) return phone && totalsShownLastTime() ? <TotalsPlaceholder /> : null
  if (totals.length === 0) return null
  const compact = phone || (width !== null && width < totals.length * STAGE_MIN_PX)
  const blockSx = compact ? COMPACT_STAGE_SX : STAGE_SX
  return (
    <Box ref={setBox} data-testid="stage-totals" data-compact={compact} component="nav" aria-label="Pipeline totals"
      sx={compact ? COMPACT_ROW_SX : { ...ROW_SX, gap: 1 }}>
      {totals.map(total => onPick ? (
        <ButtonBase key={total.name} data-testid="stage-block" data-stage={total.name}
          aria-pressed={pick !== null && pick.toLowerCase() === total.name.toLowerCase()}
          onClick={() => onPick(total.name)} sx={blockSx}>
          <BlockLine name={total.name} count={total.count} />
        </ButtonBase>
      ) : (
        // Read-only: no hover, and no phone touch floor to stretch it.
        <Box key={total.name} data-testid="stage-block" data-stage={total.name} sx={{ ...blockSx, '&:hover': {}, [PHONE]: {} }}>
          <BlockLine name={total.name} count={total.count} />
        </Box>
      ))}
    </Box>
  )
}
