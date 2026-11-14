import { type JSX, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import FormControlLabel from '@mui/material/FormControlLabel'
import Link from '@mui/material/Link'
import Radio from '@mui/material/Radio'
import RadioGroup from '@mui/material/RadioGroup'
import Typography from '@mui/material/Typography'
import { ApiError, apiDetail } from '../../api'
import { useSubmit } from '../../hooks/useSubmit'
import { hotfix, type Change, type Stage } from '../../lib/releases'
import { shortDate } from '../../lib/relativeTime'
import { FONT_WEIGHT } from '../../theme'
import { FormDialog } from '../../ui/FormDialog'
import { RelativeTime } from '../../ui/RelativeTime'

/* THE RELEASE STEP'S HOTFIX CONFIRM (`ReleaseStep.tsx`): picks the one change a hotfix sends straight to the last
 * stage. A release has no dialog: it is the Tasks page's release mode (`ReleasePick.tsx`). Refusals read as words
 * (`refusal`, shared by both). No pull request links. */

/** What a refusal of a release write says: the freeze's end, else the API's word. */
export function refusal(err: unknown, fallback: string): string {
  if (err instanceof ApiError && err.status === 409 && typeof err.detail === 'object' && err.detail !== null) {
    const detail = err.detail as { code?: string; until?: string; state?: string }
    if (detail.code === 'frozen' && detail.until) return `Frozen until ${shortDate(detail.until, new Date(), true)}. Pick a later time.`
    if (detail.code === 'open') return 'A release is already in progress.'
    if (detail.code === 'already_live') return 'Already in production.'
    if (detail.code === 'older_than_live') return 'A newer version is already there.'
    if (detail.code === 'state') return 'Already changed. Reload.'
  }
  if (err instanceof ApiError && err.status === 422 && (err.detail as { code?: string } | null)?.code === 'no_changes') return 'Nothing picked.'
  if (err instanceof ApiError && err.status === 403) return `${fallback}: not allowed.`
  return apiDetail(err, fallback)
}

export type Pending = { kind: 'hotfix'; changes: Change[]; to: Stage }

function ChangeLine({ change }: { change: Change }): JSX.Element {
  return (
    <>
      <Typography variant="body2" data-testid="release-change-title" sx={{ fontWeight: FONT_WEIGHT.medium, overflowWrap: 'anywhere' }}>
        {change.task_id !== null
          ? <Link component={RouterLink} to={`/tasks/${change.task_id}`} underline="hover" color="inherit">{change.title}</Link>
          : change.title}
      </Typography>
      <Typography variant="body2" component="div" sx={{ color: 'text.secondary', display: 'flex', flexWrap: 'wrap', columnGap: 1 }}>
        {change.author?.name && <span>{change.author.name}</span>}
        {change.merged_at && <RelativeTime iso={change.merged_at} />}
      </Typography>
    </>
  )
}

export default function DeployDialog({ projectId, pending, onClose, onDone }: {
  projectId: number
  pending: Pending
  onClose: () => void
  onDone: () => void
}): JSX.Element {
  const { busy, formError, run } = useSubmit()
  const [picked, setPicked] = useState<string>(() => (pending.changes.length === 1 ? pending.changes[0].change_id : ''))
  const submit = () => void run('Could not release', async () => {
    await hotfix(projectId, picked)
    onDone()
  }, { mapError: refusal })
  return (
    <FormDialog
      title={`Hotfix to ${pending.to.name.toLowerCase()}`}
      submitLabel="Hotfix"
      busyLabel="Releasing…"
      busy={busy}
      error={formError}
      onSubmit={submit}
      onClose={onClose}
      submitDisabled={!picked}
      submitAutoFocus
      testId="release-deploy-dialog"
      submitTestId="release-deploy-confirm"
    >
      <RadioGroup value={picked} onChange={(_, value) => setPicked(value)} aria-label="Change" data-testid="release-hotfix-pick">
        {pending.changes.map(change => (
          <FormControlLabel
            key={change.change_id}
            value={change.change_id}
            control={<Radio size="small" disabled={busy} />}
            label={<Box sx={{ py: 0.5 }}><ChangeLine change={change} /></Box>}
            sx={{ alignItems: 'flex-start', mr: 0, '& .MuiRadio-root': { mt: 0 } }}
          />
        ))}
      </RadioGroup>
    </FormDialog>
  )
}
