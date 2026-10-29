import { useEffect, useRef, useState, type JSX } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { useAuth } from '../../../auth'
import { usePolicyQuery } from '../../../hooks/usePolicyQuery'
import { useSubmit } from '../../../hooks/useSubmit'
import { planSurfaceShown } from '../../../lib/planSwitches'
import Banner from '../../../ui/Banner'
import { tapTargetSx } from '../../../ui/listStyles'
import { NODELINK_STORE_URL } from '../../nodes/nodeLinkStore/api'
import { CLOUD_CONNECTIONS_URL } from '../cloudConnections/api'
import SavedNote from '../../../ui/SavedNote'
import SubmitButton from '../../../ui/SubmitButton'
import { getResidency, putResidency, residencyKey } from './api'

/* Settings › Retention › Data residency (and a customer's Security › Data residency): which region the org's
 * transcripts and task artifacts live in (`./api.ts`). The server says what stays behind and what is not covered,
 * and — once data exists in the region — why it can no longer move. A tenant whose plan leaves Data residency out
 * gets nothing drawn and no fetch; the operator is never refused. The lock's counts ("3 files") link to
 * where each kind lives. */

export function ResidencySection({ orgId }: { orgId: string }): JSX.Element {
  const { admin } = useAuth()
  if (!planSurfaceShown(admin, 'data_residency_enabled')) return <></>
  return <Editor orgId={orgId} operator={admin?.is_operator === true} />
}

function Editor({ orgId, operator }: { orgId: string; operator: boolean }): JSX.Element {
  const client = useQueryClient()
  const { gate, data } = usePolicyQuery({ queryKey: residencyKey(orgId), queryFn: () => getResidency(orgId), operator, rows: 2, failure: 'Could not load the data region.' })
  const [draft, setDraft] = useState('')
  // A refetch (window focus) refreshes the select only while nothing is being edited.
  const editing = useRef(false)
  useEffect(() => { if (data && !editing.current) setDraft(data.effective_region) }, [data])
  const { busy, formError, run, saved: justSaved, clearSaved } = useSubmit()

  if (gate) return gate

  // The select shows the region in use: an org that never chose one is in the control plane's.
  const current = draft || data.effective_region
  const changed = current !== data.effective_region
  const locked = data.locked !== null
  // The region in use may have left the operator's list: still shown, so the select never reads another region.
  const options = data.regions.some(r => r.region === data.effective_region)
    ? data.regions
    : [{ region: data.effective_region, label: data.effective_label }, ...data.regions]

  function pick(region: string) {
    editing.current = true
    setDraft(region)
    clearSaved()
  }

  const save = () =>
    run('Could not save the data region.', async () => {
      const next = await putResidency(orgId, current)
      editing.current = false
      client.setQueryData(residencyKey(orgId), next)
    })

  return (
    <Stack
      component="form"
      noValidate
      spacing={2}
      data-testid="org-residency"
      onSubmit={e => {
        e.preventDefault()
        if (changed && !locked) void save()
      }}
      sx={{ maxWidth: 560, minWidth: 0 }}
    >
      {data.locked && <Banner severity="info" testId="org-residency-locked"><LinkedCounts text={data.locked} /></Banner>}

      <TextField
        select
        size="small"
        label="Region"
        value={current}
        onChange={e => pick(e.target.value)}
        disabled={busy || locked || options.length < 2}
        slotProps={{
          select: { native: true },
          inputLabel: { shrink: true },
          htmlInput: { 'data-testid': 'org-residency-region' },
        }}
        sx={{ maxWidth: 360, width: '100%' }}
      >
        {options.map(r => <option key={r.region} value={r.region}>{r.label}</option>)}
      </TextField>

      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5, minWidth: 0 }}>
        <Typography variant="body2" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }} data-testid="org-residency-stays">
          {data.stays}
        </Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }} data-testid="org-residency-not-covered">
          {data.not_covered}
        </Typography>
      </Box>

      {formError && <Banner severity="error" testId="org-residency-error">{formError}</Banner>}

      {!locked && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1.5 }}>
          <SubmitButton busy={busy} disabled={!changed} testId="org-residency-save">Save</SubmitButton>
          <Button
            disabled={busy || !changed}
            onClick={() => { editing.current = false; setDraft(data.effective_region); clearSaved() }}
            data-testid="org-residency-cancel"
            sx={tapTargetSx}
          >
            Cancel
          </Button>
          <SavedNote show={justSaved} />
        </Box>
      )}
    </Stack>
  )
}

/* Where each kind the server counts lives (`data_residency.blockers`): files are task attachments. */
const KIND_URL: Record<string, string> = {
  'cloud connection': CLOUD_CONNECTIONS_URL,
  'NodeLink store': NODELINK_STORE_URL,
  NodeLink: NODELINK_STORE_URL,
  machine: '/nodes',
  session: '/sessions',
  file: '/tasks',
}
// "NodeLink store" before "NodeLink": the longer kind wins.
const COUNT = /\b\d+ (cloud connection|NodeLink store|NodeLink|machine|session|file)s?\b/g

/** The server's lock sentence, each "N kind" a link to where that kind lives. */
function LinkedCounts({ text }: { text: string }): JSX.Element {
  const parts: (string | JSX.Element)[] = []
  let at = 0
  for (const m of text.matchAll(COUNT)) {
    parts.push(text.slice(at, m.index), <Link key={m.index} component={RouterLink} to={KIND_URL[m[1]]}>{m[0]}</Link>)
    at = m.index + m[0].length
  }
  parts.push(text.slice(at))
  return <>{parts}</>
}

export default ResidencySection
