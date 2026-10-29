import { useEffect, useRef, useState, type JSX } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { useAuth } from '../../../auth'
import { usePolicyQuery } from '../../../hooks/usePolicyQuery'
import { useSubmit } from '../../../hooks/useSubmit'
import { planSurfaceShown } from '../../../lib/planSwitches'
import Banner from '../../../ui/Banner'
import { tapTargetSx } from '../../../ui/listStyles'
import SavedNote from '../../../ui/SavedNote'
import SubmitButton from '../../../ui/SubmitButton'
import { getRetention, putRetention, retentionKey, type Retention, type RetentionClass, type RetentionInput } from './api'

/* Settings › Retention › Your organization (and a customer's Security › Retention): how long the viewer's own org keeps its transcripts, activity and task artifacts
 * (`./api.ts`). Shorten only — each field's range is the server's. A tenant whose plan leaves Retention out gets
 * nothing drawn and no fetch; the operator is never refused. */

const CLASSES: readonly { id: RetentionClass; label: string; note?: string }[] = [
  { id: 'transcripts', label: 'Session transcripts' },
  { id: 'events', label: 'Activity events', note: 'Security events stay at least 365 days.' },
  { id: 'artifacts', label: 'Task artifacts' },
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

export function RetentionSection({ orgId }: { orgId: string }): JSX.Element {
  const { admin } = useAuth()
  if (!planSurfaceShown(admin, 'org_retention_enabled')) return <></>
  return <Editor orgId={orgId} operator={admin?.is_operator === true} />
}

function Editor({ orgId, operator }: { orgId: string; operator: boolean }): JSX.Element {
  const client = useQueryClient()
  const { gate, data } = usePolicyQuery({ queryKey: retentionKey(orgId), queryFn: () => getRetention(orgId), operator, rows: 3, failure: 'Could not load retention.' })
  const [draft, setDraft] = useState<Draft>({ transcripts: '', events: '', artifacts: '' })
  // A refetch (window focus) refreshes the fields only while nothing is being edited.
  const editing = useRef(false)
  useEffect(() => { if (data && !editing.current) setDraft(toDraft(data)) }, [data])
  const { busy, formError, run, saved: justSaved, clearSaved } = useSubmit()
  // The clamped values the last save wrote, named beside "Saved." — the user typed none of them.
  const [adjusted, setAdjusted] = useState('')

  if (gate) return gate

  const errors = Object.fromEntries(CLASSES.map(c => [c.id, fieldError(draft[c.id], data[c.id])])) as Draft
  const changed = CLASSES.some(c => draft[c.id].trim() !== (data[c.id].days?.toString() ?? ''))
  const valid = CLASSES.every(c => !errors[c.id])

  function edit(id: RetentionClass, value: string) {
    editing.current = true
    setDraft(d => ({ ...d, [id]: value }))
    clearSaved()
  }

  const save = () =>
    run('Could not save retention.', async () => {
      const body = Object.fromEntries(
        CLASSES.map(c => [c.id, draft[c.id].trim() ? Number(draft[c.id].trim()) : null]),
      ) as RetentionInput
      const next = await putRetention(orgId, body)
      setAdjusted(CLASSES
        .filter(c => data[c.id].clamped_from !== null && body[c.id] === data[c.id].days)
        .map(c => `${c.label} set to ${body[c.id]} days.`)
        .join(' '))
      editing.current = false
      client.setQueryData(retentionKey(orgId), next)
    })

  return (
    <Stack
      component="form"
      noValidate
      spacing={2}
      data-testid="org-retention"
      onSubmit={e => {
        e.preventDefault()
        if (changed && valid) void save()
      }}
      sx={{ maxWidth: 560, minWidth: 0 }}
    >
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
        const help = `${range}${clamped}${c.note ? ` ${c.note}` : ''}`
        return (
          <TextField
            key={c.id}
            size="small"
            label={c.label}
            value={draft[c.id]}
            onChange={e => edit(c.id, e.target.value)}
            disabled={busy}
            placeholder={inRange ? `Default (${w.default_days} days)` : `Platform (${w.default_days} days)`}
            error={Boolean(errors[c.id])}
            helperText={errors[c.id] || help}
            slotProps={{
              htmlInput: { inputMode: 'numeric', 'data-testid': `org-retention-${c.id}` },
              // The label stays raised so the empty state's "Default (N days)" is visible.
              inputLabel: { shrink: true },
              input: { endAdornment: empty ? undefined : <Box component="span" sx={{ color: 'text.secondary', ml: 1 }}>days</Box> },
              formHelperText: { sx: { mx: 0, whiteSpace: 'normal', overflowWrap: 'anywhere' } },
            }}
            sx={{ maxWidth: 360, width: '100%' }}
          />
        )
      })}

      {formError && <Banner severity="error" testId="org-retention-error">{formError}</Banner>}

      <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1.5 }}>
        <SubmitButton busy={busy} disabled={!changed || !valid} testId="org-retention-save">Save</SubmitButton>
        <Button
          disabled={busy || !changed}
          onClick={() => { editing.current = false; setDraft(toDraft(data)); clearSaved() }}
          data-testid="org-retention-cancel"
          sx={tapTargetSx}
        >
          Cancel
        </Button>
        <SavedNote show={justSaved} detail={adjusted} />
      </Box>
    </Stack>
  )
}

export default RetentionSection
