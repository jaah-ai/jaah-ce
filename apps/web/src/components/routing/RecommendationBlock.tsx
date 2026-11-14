// The task form's Recommend-mode pick: what the agent router would run this `coder` draft on, and why, with "Use this".
// Reads `POST /api/routing/preview` (`apps/api/app/routes/routing.py`), debounced over the draft; indicative only,
// it writes nothing. The host owns what "Use this" does (it sets its own runner picker and sends the pick back as
// `routing_accepted_config` on its next write). The reason is shown in full and wraps; it is never truncated.
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { type JSX, useEffect, useState } from 'react'
import { post } from '../../api'
import { writeAsOnce } from '../../lib/actAs'
import { RADIUS, grid } from '../../theme'
import type { OrgRef } from '../../types'
import { FactsGrid } from '../../ui/FactsGrid'
import { StatusChip } from '../../ui/StatusChip'
import { Fact, type RoutingConfig, WRAP } from './RoutingPanel'

export interface RoutingRecommendation {
  config: RoutingConfig
  /** The router's Config, sent back as `routing_accepted_config` once accepted. */
  pick: Record<string, unknown>
  reason: string
  exploratory: boolean
  response: 'accepted' | 'overridden' | null
}

export interface RoutingPreview {
  mode: 'shadow' | 'recommend' | 'auto' | null
  recommendation: RoutingRecommendation | null
}

/** Quiet time after the last draft change before the preview is asked. */
export const PREVIEW_DEBOUNCE_MS = 600

/** The persona the router routes; the block is drawn for no other. */
const ROUTED_PERSONA = 'coder'

export function RecommendationBlock({ projectId, persona, title, body, taskId = null, org = null, accepted, onUse, onShown }: {
  projectId: number | null
  persona: string
  title: string
  body: string
  /** A saved task: its own live recommendation answers. */
  taskId?: number | null
  /** An operator's form on another org's project: the preview asks as that org (`lib/actAs.ts::writeAsOnce`), as the
   *  route resolves the project in the caller's org and answers 404 for any other. Null for the viewer's own org. */
  org?: OrgRef | null
  /** The host holds this pick as accepted ("Use this" landed and its picker still agrees). */
  accepted: boolean
  onUse: (pick: Record<string, unknown>) => void
  /** Whether a recommendation is on screen: the host's writes then answer it (taken, or the person's own pick). */
  onShown?: (shown: boolean) => void
}): JSX.Element | null {
  const [rec, setRec] = useState<RoutingRecommendation | null>(null)
  const routed = projectId !== null && persona === ROUTED_PERSONA
  const shown = routed && rec !== null
  useEffect(() => { onShown?.(shown) }, [shown, onShown])
  useEffect(() => {
    if (!routed) {
      setRec(null)
      return
    }
    let stale = false
    const timer = setTimeout(() => {
      Promise.resolve()
        .then(() => post<RoutingPreview>('/api/routing/preview', { project_id: projectId, persona, title, body, task_id: taskId },
          ...writeAsOnce(org)))
        .then(preview => { if (!stale) setRec(preview?.recommendation ?? null) })
        // Indicative only: a refused or failed preview shows nothing rather than an error over the form.
        .catch(() => { if (!stale) setRec(null) })
    }, PREVIEW_DEBOUNCE_MS)
    return () => {
      stale = true
      clearTimeout(timer)
    }
  }, [routed, projectId, persona, title, body, taskId, org?.id])

  if (!routed || rec === null) return null
  const answer = accepted ? 'accepted' : rec.response
  return (
    <Box component="section" data-testid="routing-recommendation" aria-label="Recommended"
      sx={{ border: 1, borderColor: 'divider', borderRadius: RADIUS.md, p: 1.5, mb: 2, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 1 }}>
      {/* The answer rides the heading line, not the block's foot: on a phone the block opens just above the form's
          sticky Save/Go bar, which covered a foot button until the person scrolled into it. */}
      <Stack direction="row" sx={{ flexWrap: 'wrap', alignItems: 'center', columnGap: 1, rowGap: 0.5 }}>
        <Typography variant="subtitle2" component="h4">Recommended</Typography>
        {rec.exploratory && <StatusChip label="Exploring" tone="info" />}
        <Box sx={{ ml: 'auto' }}>
          {answer ? (
            <StatusChip label={answer === 'accepted' ? 'Accepted' : 'Overridden'} tone={answer === 'accepted' ? 'success' : 'neutral'}
              testId="routing-recommendation-response" />
          ) : (
            <Button variant="outlined" size="small" type="button" onClick={() => onUse(rec.pick)} data-testid="routing-recommendation-use">
              Use this
            </Button>
          )}
        </Box>
      </Stack>
      <FactsGrid minTile={grid(32)} testId="routing-recommendation-facts">
        <Fact label="Runner" value={rec.config.runner} testId="routing-recommendation-runner" />
        {rec.config.tier && <Fact label="Tier" value={rec.config.tier} testId="routing-recommendation-tier" />}
        {rec.config.model && <Fact label="Model" value={rec.config.model} />}
        <Fact label="Review" value={rec.config.review} testId="routing-recommendation-review" />
      </FactsGrid>
      <Typography variant="body2" data-testid="routing-recommendation-reason" sx={WRAP}>{rec.reason}</Typography>
    </Box>
  )
}
