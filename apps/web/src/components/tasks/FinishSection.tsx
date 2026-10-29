import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { EpicMetrics } from '../../types'
import { humanDuration } from '../../lib/relativeTime'
import { personaLabel } from './personaRegistry'
import { TaskPrList } from './TaskPrList'

// What the FINISHER found — the body of the Details tab's "Finisher Notes":
// the verify verdict and unresolved items, per-phase breakdown, re-exploration/data-loss benchmarks and
// Finish-filed follow-ups. A pure read of the epic's `metrics` JSONB (apps/api's `_task_out`), no fetch.
// Run totals live in `TaskRunMetrics.tsx` (computed for ANY task); restating Finisher-authored totals here
// would recreate the ambiguity the split removed.
// BA D6 — graceful NULL: `metrics=null` or no `finish_report` renders a plain note, never a crash.
export default function FinishSection({ metrics }: { metrics: EpicMetrics | null | undefined }) {
  const report = metrics?.finish_report
  if (!report) {
    // A plain caption, not `ui/EmptyState` (too tall). Names this panel's absence (the Finisher's notes),
    // not the run figures, which are available above.
    return (
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>
        No finisher notes yet.
      </Typography>
    )
  }

  const { verify, time, tokens, benchmarks } = report

  const perPhaseSeconds = new Map<string, number>()
  for (const entry of Object.values(time.per_child)) {
    if (entry.seconds == null) continue
    const label = personaLabel(entry.persona)
    perPhaseSeconds.set(label, (perPhaseSeconds.get(label) ?? 0) + entry.seconds)
  }

  const tokenPhases = Object.entries(tokens.per_phase)

  const unresolvedCount =
    verify.missing_analysis_artifacts.length +
    verify.unresolved_coders.length +
    verify.orphan_fanout_children.length +
    (verify.append_tasks_pending ? 1 : 0)

  // Reported, NEVER added to `unresolvedCount`: apps/api excludes never-dispatched Finish-filed follow-ups
  // from `unresolved_coders` so a properly owned gap does not stay red forever. `?? []` for older reports.
  const filedFollowUps = verify.filed_follow_ups ?? []
  // Every PR the build children recorded, one per repository; `?? []` for older reports.
  const prs = report.prs ?? []

  return (
    /* No padding of its own: the `ui/detail` `DetailSection` supplies the panel inset; doubling it pushed this
       block past its heading and siblings. Line spacing is the `gap`. */
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: '0.375rem' }}>
      <Stack direction="row" useFlexGap sx={{ flexWrap: 'wrap', gap: '4px 12px', fontSize: '0.8125rem' }}>
        {/* The verify verdict heads this row, a wrapping `Stack` so another verdict-shaped finding can join it. */}
        <Typography component="span" variant="inherit" sx={{ color: 'text.secondary' }}>
          Verify:{' '}
          <Box component="strong" sx={{ color: verify.all_resolved ? 'success.main' : 'error.main' }}>
            {verify.all_resolved ? 'all resolved' : `${unresolvedCount} unresolved`}
          </Box>
        </Typography>
      </Stack>

      {perPhaseSeconds.size > 0 && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          Per-phase time:{' '}
          {Array.from(perPhaseSeconds.entries())
            .map(([label, secs]) => `${label} ${humanDuration(secs, true)}`)
            .join(' · ')}
        </Typography>
      )}

      {tokenPhases.length > 0 && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          Per-phase tokens:{' '}
          {tokenPhases
            .map(([persona, p]) =>
              `${personaLabel(persona)} ${p.tokens_in.toLocaleString()}/${p.tokens_out.toLocaleString()}` +
              (p.capture_status === 'missing' ? ' (capture missing)' : ''),
            )
            .join(' · ')}
        </Typography>
      )}

      <Typography variant="caption" sx={{ color: 'text.secondary' }}>
        Re-exploration: {benchmarks.re_exploration.duplicate_target_count} duplicate target
        {benchmarks.re_exploration.duplicate_target_count === 1 ? '' : 's'}
        {benchmarks.re_exploration.duplicated_targets.length > 0
          ? ` (${benchmarks.re_exploration.duplicated_targets.join(', ')})`
          : ''}
        {' · '}
        Data loss between phases:{' '}
        {benchmarks.data_loss_between_phases.length === 0
          ? 'none'
          : `${benchmarks.data_loss_between_phases.length} gap${
              benchmarks.data_loss_between_phases.length === 1 ? '' : 's'
            }`}
      </Typography>

      {unresolvedCount > 0 && (
        <Typography component="ul" variant="caption" sx={{ m: 0, pl: '1.25rem', color: 'text.secondary' }}>
          {verify.missing_analysis_artifacts.map(m => (
            <li key={`artifact-${m.child_id}`}>
              Missing analysis artifact — child #{m.child_id} ({personaLabel(m.persona)})
            </li>
          ))}
          {verify.unresolved_coders.map(m => (
            <li key={`impl-${m.child_id}`}>
              Unresolved coder child #{m.child_id} ({m.state})
            </li>
          ))}
          {verify.orphan_fanout_children.map(m => (
            <li key={`orphan-${m.child_id}`}>
              Orphaned fan-out — child #{m.child_id} ({m.fanout_attempts} attempts)
            </li>
          ))}
          {verify.append_tasks_pending && <li>Plan → coder append still pending</li>}
        </Typography>
      )}

      {prs.length > 0 && (
        <Box data-testid="finish-prs" sx={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>Pull requests</Typography>
          <TaskPrList
            prs={prs}
            lead={pr => (
              <Typography component="span" variant="caption" sx={{ color: 'text.secondary', flex: '0 0 auto' }}>
                #{pr.child_id}
              </Typography>
            )}
          />
        </Box>
      )}

      {filedFollowUps.length > 0 && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          Finish-filed follow-up{filedFollowUps.length === 1 ? '' : 's'}:{' '}
          {filedFollowUps.map(f => `#${f.child_id} (${f.state})`).join(' · ')}
        </Typography>
      )}
    </Box>
  )
}
