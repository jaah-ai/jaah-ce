import { useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import FormControlLabel from '@mui/material/FormControlLabel'
import Radio from '@mui/material/Radio'
import RadioGroup from '@mui/material/RadioGroup'
import Typography from '@mui/material/Typography'
import { get } from '../../api'
import type { DispatchForecast } from '../../types'
import FormDialog from '../../ui/FormDialog'
import { tapTargetSx } from '../../ui/listStyles'
import { DISPATCH_ROUTES } from './controls'

/* "No capacity — stop one to start this one". Opened by Start when
 * `GET /api/tasks/{id}/dispatch-forecast` says the task will not dispatch now — predicting, rather than
 * leaving it silently `ready`. The server re-queues the stopped task (`force-start`).
 * Default pick is the first candidate: the server sorts lowest-priority-first. */

export type PreemptTarget = { id: number; title: string }

/** Whether a forecast should open this dialog: it would NOT dispatch now, stopping a task could help
 *  (`preemptable` — false for a missing worktree or the task's own hold), and something is
 *  running. A type guard because callers read an untrusted payload: `[]`, `{}` or a forecast without
 *  `preemptable` must fall through to the ordinary start, never throw. */
export function shouldOfferPreempt(forecast: unknown): forecast is DispatchForecast {
  if (!forecast || typeof forecast !== 'object') return false
  const f = forecast as Partial<DispatchForecast>
  return (
    f.can_dispatch_now === false &&
    f.preemptable === true &&
    Array.isArray(f.running) &&
    f.running.length > 0
  )
}

/* The forecast every Start door asks before a capacity-blocked start: the forecast when a preempt is worth
   offering, else null. NEVER REJECTS — a failed forecast is a null, so the caller's ordinary start goes ahead. */
export async function preemptForecast(taskId: number): Promise<DispatchForecast | null> {
  const forecast = await get<DispatchForecast>(DISPATCH_ROUTES.forecast(taskId)).catch(() => null)
  return shouldOfferPreempt(forecast) ? forecast : null
}

export default function PreemptDialog({
  open,
  target,
  forecast,
  onForceStart,
  onQueue,
  onCancel,
}: {
  open: boolean
  target: PreemptTarget
  forecast: DispatchForecast
  /** "Stop & force-start" — the chosen running task's id. */
  onForceStart: (stopTaskId: number) => void
  /** "Just queue it" — start it the ordinary way and let it wait. */
  onQueue: () => void
  onCancel: () => void
}) {
  const [stopId, setStopId] = useState<number>(forecast.running[0]?.id)

  return (
    <FormDialog
      open={open}
      title={`No capacity for “${target.title}”`}
      submitLabel="Stop & force-start"
      destructive
      submitAutoFocus
      submitDisabled={stopId === undefined}
      onSubmit={() => onForceStart(stopId)}
      onClose={onCancel}
      testId="preempt-dialog"
      paperSx={{ maxWidth: 420 }}
      contentSx={{ gap: 1, fontSize: '0.875rem' }}
      secondaryAction={
        <Button type="button" onClick={onQueue} variant="outlined" color="inherit" sx={tapTargetSx}>
          Just queue it
        </Button>
      }
    >
      {forecast.reasons.length > 0 && (
        <Box component="ul" sx={{ m: 0, pl: 2.5, color: 'text.secondary' }}>
          {forecast.reasons.map(reason => (
            <li key={reason}>{reason}</li>
          ))}
        </Box>
      )}
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>Stop a running task and force-start it?</Typography>
      <RadioGroup
        aria-label="Running task to stop"
        value={String(stopId)}
        onChange={e => setStopId(Number(e.target.value))}
      >
        {forecast.running.map(candidate => (
          <FormControlLabel
            key={candidate.id}
            value={String(candidate.id)}
            control={<Radio size="small" />}
            sx={{ alignItems: 'flex-start', mr: 0, '& .MuiFormControlLabel-label': { minWidth: 0 } }}
            label={
              <Typography variant="body2" component="span" sx={{ display: 'block', py: 0.5 }}>
                {candidate.title}
                <Typography variant="caption" component="span" sx={{ color: 'text.secondary' }}>
                  {' · '}
                  {candidate.project_name}
                  {' · '}
                  {candidate.priority_label}
                </Typography>
              </Typography>
            }
          />
        ))}
      </RadioGroup>
    </FormDialog>
  )
}
