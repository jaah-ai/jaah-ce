import { useState, type JSX } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { usePolicyEditor } from '../../../hooks/usePolicyEditor'
import { planSurface } from '../../../lib/planSwitches'
import type { Explain } from '../../../ui/explain'
import FormTextField from '../../../ui/FormTextField'
import PolicyForm from '../../../ui/PolicyForm'
import { getRetention, putRetention, retentionKey, type Retention, type RetentionClass, type RetentionInput } from './api'
import { SIZE } from '../../../theme'

/* Settings › Retention › Your organization (and a customer's Security › Retention): how long the viewer's own org keeps its transcripts, activity and task artifacts
 * (`./api.ts`). Shorten only — each field's range is the server's. A tenant whose plan leaves Retention out gets
 * nothing drawn and no fetch; the operator is never refused. */

const RETENTION_DOCS = 'administration/security-and-audit/settings'

const CLASSES: readonly { id: RetentionClass; label: string; help: Explain }[] = [
  { id: 'transcripts', label: 'Session transcripts', help: { tip: 'Days before agent session transcripts are deleted.', docs: RETENTION_DOCS } },
  {
    id: 'events',
    label: 'Activity events',
    help: { tip: 'Days before activity events are deleted.', more: 'Security events stay at least 365 days.', docs: RETENTION_DOCS },
  },
  { id: 'artifacts', label: 'Task artifacts', help: { tip: 'Days before files tasks produced are deleted.', docs: RETENTION_DOCS } },
]

type Draft = Record<RetentionClass, string>

const toDraft = (saved: Retention): Draft => ({
  transcripts: saved.transcripts.days?.toString() ?? '',
  events: saved.events.days?.toString() ?? '',
  artifacts: saved.artifacts.days?.toString() ?? '',
})

/** The field's refusal, or '' when it may be saved: empty, or a whole number inside the range. */
function fieldError(raw: string, w: Retention[RetentionClass]): string {
  const text = raw.trim()
  if (!text) return ''
  const days = Number(text)
  if (!Number.isInteger(days) || days < w.min_days || days > w.max_days) return `Enter ${w.min_days} to ${w.max_days} days, or leave empty.`
  return ''
}

export const RetentionSection = planSurface('org_retention_enabled', (admin, { orgId }: { orgId: string }) => <Editor orgId={orgId} operator={admin?.is_operator === true} />)

function Editor({ orgId, operator }: { orgId: string; operator: boolean }): JSX.Element {
  const { gate, data, draft, edit, busy, formError, save, saveBar } = usePolicyEditor({
    queryKey: retentionKey(orgId), queryFn: () => getRetention(orgId), operator, rows: 3, failure: 'Could not load retention.',
    toDraft, initial: { transcripts: '', events: '', artifacts: '' },
  })
  // The clamped values the last save wrote, named beside "Saved." — the user typed none of them.
  const [adjusted, setAdjusted] = useState('')

  if (gate) return gate

  const errors = Object.fromEntries(CLASSES.map(c => [c.id, fieldError(draft[c.id], data[c.id])])) as Draft
  const changed = CLASSES.some(c => draft[c.id].trim() !== (data[c.id].days?.toString() ?? ''))
  const valid = CLASSES.every(c => !errors[c.id])

  const store = () =>
    save('Could not save retention.', async () => {
      const body = Object.fromEntries(
        CLASSES.map(c => [c.id, draft[c.id].trim() ? Number(draft[c.id].trim()) : null]),
      ) as RetentionInput
      const next = await putRetention(orgId, body)
      setAdjusted(CLASSES
        .filter(c => data[c.id].clamped_from !== null && body[c.id] === data[c.id].days)
        .map(c => `${c.label} set to ${body[c.id]} days.`)
        .join(' '))
      return next
    })

  return (
    <PolicyForm testId="org-retention" canSave={changed && valid} onSave={() => void store()} error={formError}
      bar={{ ...saveBar, dirty: changed, disabled: !valid, savedDetail: adjusted }}>
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        Older data is deleted for good each night.
      </Typography>

      {CLASSES.map(c => {
        const w = data[c.id]
        const empty = !draft[c.id].trim()
        /* What empty keeps is the platform's window, which the operator's ceiling may sit below (`org_retention.py::
         * bounds`): then it is not a "default" of the range, and the hint says what empty keeps instead.
         * Inside the range, empty shows the default as its placeholder, so the hint repeats only the range; a default
         * AT the ceiling is said once. */
        const inRange = w.default_days >= w.min_days && w.default_days <= w.max_days
        const range = !inRange ? `${w.min_days} to ${w.max_days} days. Empty keeps the platform's ${w.default_days} days.`
          : empty ? `${w.min_days} to ${w.max_days} days.`
            : w.default_days === w.max_days ? `${w.min_days} to ${w.max_days} days (default).`
              : `${w.min_days} to ${w.max_days} days · default ${w.default_days}.`
        // A value the range has since moved past reads as its clamp, so Save is never blocked by a field not edited.
        const clamped = w.clamped_from !== null && draft[c.id].trim() === String(w.days)
          ? ` Was ${w.clamped_from}; the allowed range changed.` : ''
        return (
          <FormTextField
            key={c.id}
            label={c.label}
            help={c.help}
            value={draft[c.id]}
            onChange={v => edit(d => ({ ...d, [c.id]: v }))}
            disabled={busy}
            placeholder={inRange ? `Default (${w.default_days} days)` : `Platform (${w.default_days} days)`}
            error={errors[c.id]}
            hint={`${range}${clamped}`}
            slotProps={{
              htmlInput: { inputMode: 'numeric', 'data-testid': `org-retention-${c.id}` },
              input: { endAdornment: empty ? undefined : <Box component="span" sx={{ color: 'text.secondary', ml: 1 }}>days</Box> },
              formHelperText: { sx: { mx: 0, whiteSpace: 'normal', overflowWrap: 'anywhere' } },
            }}
            sx={{ maxWidth: SIZE.measureNarrow, width: '100%' }}
          />
        )
      })}
    </PolicyForm>
  )
}

export default RetentionSection
