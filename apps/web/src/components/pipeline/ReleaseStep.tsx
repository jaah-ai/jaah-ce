import { type JSX, useEffect, useRef } from 'react'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  getReleaseTab, inFlightLabel, latestByStage, openInto, releaseHistory, releaseKey, requestSurvey, RELEASE_IDLE_POLL_MS,
  RELEASE_POLL_MS, type Release, type ReleaseTab,
} from '../../lib/releases'
import type { ReleaseRun } from '../../lib/pipelineBoard'
import { STEP_ACCENT } from '../../lib/pipeline'
import { shortDate } from '../../lib/relativeTime'
import { RADIUS, SIZE } from '../../theme'
import { ReleaseIcon } from '../../ui/icons'
import { PHONE } from '../../ui/listStyles'
import Tip from '../../ui/Tip'
import { refusal } from './DeployDialog'
import StepGlyph from './StepGlyph'

/* THE RELEASE STEP'S ONE MARK — on the pipeline page's strip (`StageStrip.tsx`), in place of the Release icon: a truck
 * on a tinted blue disc at rest (`STEP_ACCENT`). It turns while a release is open and goes red when the last release
 * failed; it carries no count (the source stage's block shows what waits), and its hover says the state ("Queued",
 * "1/2 approvals", "Failed at deploy › Migrate"). Pressing it picks the step, as any strip pick (pressed while
 * picked); the list then holds what
 * waits and every release action (`ReleasePick.tsx`). It reads
 * the release seam (`lib/releases.ts`, `GET …/release`), polling fast while a release is open, slow otherwise, and, once
 * per project for a viewer who may release, asks for a fresh count (`requestSurvey`). */

const MARK_SX = {
  p: 0.5,
  borderRadius: RADIUS.round,
  '&:hover': { bgcolor: 'action.hover' },
  '&[aria-pressed="true"]': { outline: '2px solid', outlineColor: 'primary.main' },
  '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main' },
  [PHONE]: { minHeight: SIZE.tapTarget, minWidth: SIZE.tapTarget },
} as const

/** Where a failed run failed: "Failed at deploy › Migrate", else the release's own message. */
export function failedWhere(release: Release, run: ReleaseRun | null): string {
  if (release.failed_gate) return `Failed at gate \u203a ${release.failed_gate}`
  if (run?.failing_job) return `Failed at ${run.failing_job}${run.failing_step ? ` › ${run.failing_step}` : ''}`
  return release.failure?.message || 'Release failed'
}

/** The release tab, polled fast while a release is open; and the history its last failure is read from. */
export function useReleaseTab(projectId: number) {
  const tab = useQuery({
    queryKey: releaseKey(projectId),
    queryFn: () => getReleaseTab(projectId),
    refetchInterval: query => {
      const data = query.state.data
      if (!data) return false
      return data.open.length > 0 || data.executor.state !== 'idle' ? RELEASE_POLL_MS : RELEASE_IDLE_POLL_MS
    },
  })
  const openKey = (tab.data?.open ?? []).map(r => `${r.id}:${r.state}`).join(',')
  const history = useQuery({
    queryKey: [...releaseKey(projectId), 'history', openKey],
    queryFn: () => releaseHistory(projectId),
    /* A project with no repository reads an empty tab (no stages): it has no history to ask for. */
    enabled: (tab.data?.stages.length ?? 0) > 0,
    placeholderData: previous => previous,
  })
  return { tab, history }
}

/** The step's stages, its open release and its last failure, from the tab. */
export function releaseStepOf(data: ReleaseTab, history: Release[], fromStageId: string, toStageId: string) {
  /* The release's source is the branch stage before its target (`data.stages` holds branch stages only), which is the
     arrow's own left stage unless a no-branch stage sits between them (`board.py::_step_out`). */
  const to = data.stages.find(s => s.id === toStageId)
  const toIndex = to ? data.stages.indexOf(to) : -1
  const from = data.stages.find(s => s.id === fromStageId) ?? (toIndex > 0 ? data.stages[toIndex - 1] : undefined)
  if (!from || !to) return null
  const open = openInto(data.open, to.id)
  const latest = latestByStage(history).get(to.id)
  const failed = !open && latest?.state === 'failed' ? latest : undefined
  return { from, to, toIndex, open, failed }
}

export default function ReleaseStep({ projectId, fromStageId, toStageId, run, pressed = false, onRelease }: {
  projectId: number
  fromStageId: string
  toStageId: string
  /** The step's release run as the pipeline view reports it (its failing job and step). */
  run: ReleaseRun | null
  /** The step is the strip's pick. */
  pressed?: boolean
  /** Pick the step on the release's source stage. */
  onRelease: (fromStageId: string) => void
}): JSX.Element {
  const queryClient = useQueryClient()
  const { tab, history } = useReleaseTab(projectId)
  const data = tab.data

  /* Once per project, a deployer asks for a fresh count: a merge made on the forge leaves no row, so only a survey shows
     it. The server files one only when the last is stale; the automatic survey (`tick.py`) is the backstop. */
  const surveyAsked = useRef<number | null>(null)
  const surveyDue = data !== undefined && data.can_deploy && data.stages.length > 1
  useEffect(() => {
    if (!surveyDue || surveyAsked.current === projectId) return
    surveyAsked.current = projectId
    void (async () => {
      try {
        if ((await requestSurvey(projectId))?.filed) void queryClient.invalidateQueries({ queryKey: releaseKey(projectId) })
      } catch {
        /* The automatic survey (`tick.py`) is the backstop: a refused ask changes nothing on the strip. */
      }
    })()
  }, [surveyDue, projectId, queryClient])

  const step = data ? releaseStepOf(data, history.data?.items ?? [], fromStageId, toStageId) : null
  let words = 'Release'
  const accent = STEP_ACCENT.release ?? undefined
  let glyph = <StepGlyph icon={<ReleaseIcon />} accent={accent} />
  if (data && step) {
    const { to, open, failed } = step
    if (open) {
      /* A queue the executor cannot start says why on hover, its ring in the warning tone (style guide §Status prose). */
      const held = open.state === 'queued' ? data.executor.wait_reason : null
      words = held ? `${inFlightLabel(open)} — ${held}` : inFlightLabel(open)
      glyph = <StepGlyph icon={<ReleaseIcon />} active tone={held ? 'warning' : 'info'} />
    } else {
      if (failed) words = failedWhere(failed, run)
      else if (to.frozen_until) words = `Frozen until ${shortDate(to.frozen_until, new Date(), true)}`
      glyph = <StepGlyph icon={<ReleaseIcon />} error={!!failed} accent={accent} />
    }
  }
  /* Until the tab is read — or with no release to send (a project with no repository) — the icon alone: nothing to
     open yet. A failed read says so on hover. */
  if (!step) {
    const title = tab.isError ? refusal(tab.error, 'Could not load') : words
    return (
      <Tip title={title} describeChild={false}>
        <Box component="span" role="img" aria-label={title} data-testid="release-step-idle" sx={{ display: 'inline-flex', p: 0.5 }}>
          {glyph}
        </Box>
      </Tip>
    )
  }
  return (
    <Tip title={words} describeChild={false}>
      <ButtonBase data-testid="release-step" data-state={step.open?.state ?? (step.failed ? 'failed' : 'idle')}
        aria-label={words} aria-pressed={pressed} onClick={() => onRelease(step.from.id)} sx={MARK_SX}>
        {glyph}
      </ButtonBase>
    </Tip>
  )
}
