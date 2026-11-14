import type { JSX, ReactNode } from 'react'
import type { ArrivalState, StageArrival } from '../../lib/pipelineBoard'
import StatusChip, { type Tone } from '../../ui/StatusChip'
import { AlertIcon, ClockIcon, ErrorCircleIcon } from '../../ui/icons'

/* A STAGE'S ARRIVAL AS ONE CHIP — the newest release on a stage, deployed then verified (`StageArrival`): the strip's
 * stage block (`StageStrip.tsx`) and the Release view's target (`ReleasePick.tsx`) draw it alike. The single home of
 * the state → word and tone map. Arrived, verified and superseded draw nothing: the stage reads as usual. A failed
 * chip's hover is the arrival's report. */

type Shown = { label: (arrival: StageArrival) => string; tone: Tone; glyph: ReactNode }

const SHOWN: Partial<Record<ArrivalState, Shown>> = {
  deploying: { label: () => 'Deploying', tone: 'neutral', glyph: <ClockIcon /> },
  verifying: { label: a => (a.total > 0 ? `Verifying · ${a.passed} of ${a.total}` : 'Verifying'), tone: 'info', glyph: <ClockIcon /> },
  deploy_failed: { label: () => 'Deploy failed', tone: 'error', glyph: <ErrorCircleIcon /> },
  verify_failed: { label: () => 'Verify failed', tone: 'error', glyph: <ErrorCircleIcon /> },
  rolled_back: { label: () => 'Rolled back', tone: 'warning', glyph: <AlertIcon /> },
}

/** A failed arrival: its report is worth reading. */
export const arrivalFailed = (arrival: StageArrival | null | undefined): boolean =>
  arrival?.state === 'deploy_failed' || arrival?.state === 'verify_failed'

/** The arrival's chip, or nothing for a state that needs no word. */
export default function ArrivalChip({ arrival }: { arrival: StageArrival | null | undefined }): JSX.Element | null {
  const shown = arrival ? SHOWN[arrival.state] : undefined
  if (!arrival || !shown) return null
  const label = shown.label(arrival)
  const report = arrivalFailed(arrival) && arrival.report ? arrival.report : undefined
  return (
    <StatusChip label={label} tone={shown.tone} glyph={shown.glyph} title={report}
      ariaLabel={report ? `${label}: ${report}` : undefined}
      testId="stage-arrival" data-status={arrival.state} />
  )
}
