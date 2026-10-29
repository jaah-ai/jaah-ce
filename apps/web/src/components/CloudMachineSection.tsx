import { type JSX, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import TextField from '@mui/material/TextField'
import { ApiError, post, put } from '../api'
import { useSubmit } from '../hooks/useSubmit'
import type { NodeCloud, NodeCloudAction, NodeCloudStopRefusal } from '../types'
import Banner from '../ui/Banner'
import { DetailField, DetailSection } from '../ui/detail/DetailPage'
import { Mono } from '../ui/DetailFields'
import FormDialog from '../ui/FormDialog'
import RelativeTime from '../ui/RelativeTime'
import { RunningSpinner } from '../ui/RunningSpinner'
import StatusChip, { type Tone } from '../ui/StatusChip'
import { fieldTapTargetSx, PHONE } from '../ui/listStyles'
import { useConfirm } from './ConfirmDialog'

/* A CLOUD-LAUNCHED NODE'S MACHINE: its type, disk and power state, and the verbs on it — every org
 * admin's, beside the operator-only Cloud instance section on the node's Overview.
 *
 * THE VERBS ARE `cloud.actions`, NEVER A CLIENT RULE: the server knows the provider, the plan, the state and any
 * change in flight; a button drawn from `state` here would offer a 409. Delete is in the page's More menu (the
 * `delete` action is the server's, the menu's gate is the page's).
 *
 * FORCE IS EARNED: Stop and Grow disk send no `force`; only their 409 — which names the tasks the write would kill —
 * offers Force stop / Force grow, and that second confirm names every task. Every write answers 202 with the new
 * block (`onChanged`), and the page re-reads the node.
 *
 * IN FLIGHT: Starting… and Stopping… are chips with a spinner; a type change that stopped reporting
 * (`stuck`) reads as failed and says where to look; a grown disk's root filesystem growing (`root_grow`) is a banner
 * until the node reports it done. */

const STATE: Record<NonNullable<NodeCloud['state']>, { label: string; tone: Tone }> = {
  running: { label: 'Running', tone: 'success' },
  stopped: { label: 'Stopped', tone: 'neutral' },
  starting: { label: 'Starting…', tone: 'info' },
  pending: { label: 'Starting…', tone: 'info' },
  stopping: { label: 'Stopping…', tone: 'info' },
}

/** What a grown disk's root filesystem growing says (`root_grow`). */
export const ROOT_GROW_FAILED = 'The node couldn’t grow its root filesystem into the disk.'

/* The 409's body, when it is the running-tasks refusal (any other 409 is an ordinary error). */
function tasksRefusal(err: unknown): NodeCloudStopRefusal | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null
  const body = err.body as Partial<NodeCloudStopRefusal> | null
  if (!body || !Array.isArray(body.tasks)) return null
  return { detail: typeof body.detail === 'string' ? body.detail : '', tasks: body.tasks }
}

/* Labels wrap, never clip: the row wraps first, then a label inside its button (390px). */
const ACTION_SX = {
  whiteSpace: 'normal',
  textAlign: 'center',
  minWidth: 0,
  [PHONE]: { minHeight: 44 },
} as const

const VERBS: { action: NodeCloudAction; label: string; testId: string }[] = [
  { action: 'start', label: 'Start', testId: 'cloud-start' },
  { action: 'stop', label: 'Stop', testId: 'cloud-stop' },
  { action: 'change_type', label: 'Change type', testId: 'cloud-change-type' },
  { action: 'grow_disk', label: 'Grow disk', testId: 'cloud-grow-disk' },
]

export default function CloudMachineSection({ nodeId, nodeName, cloud, onChanged }: {
  /** The node's uuid: every `/api/nodes/{id}/cloud/...` door takes it. */
  nodeId: string
  nodeName: string
  cloud: NodeCloud
  /** The 202's block; the page adopts it and re-reads the node. */
  onChanged: (cloud: NodeCloud) => void
}): JSX.Element {
  const [confirm, confirmDialog] = useConfirm()
  const { busy, formError, run } = useSubmit()
  const [dialog, setDialog] = useState<'type' | 'disk' | null>(null)
  const base = `/api/nodes/${encodeURIComponent(nodeId)}/cloud`
  const state = cloud.state ? STATE[cloud.state] : { label: 'Unknown', tone: 'neutral' as const }
  const change = cloud.change && cloud.change.state !== 'done' ? cloud.change : null
  // Stuck reads as failed: neither will finish on its own.
  const changeFailed = change?.state === 'failed' || change?.state === 'stuck'
  const lastStep = change?.steps.at(-1)
  const rootGrow = cloud.root_grow ?? null

  async function stop(force: boolean) {
    const out: { refusal?: NodeCloudStopRefusal } = {}
    await run(`Couldn’t stop ${nodeName}`, async () => {
      try {
        onChanged(await post<NodeCloud>(`${base}/stop`, force ? { force: true } : undefined))
      } catch (err) {
        const refusal = force ? null : tasksRefusal(err)
        if (!refusal) throw err
        out.refusal = refusal
      }
    })
    if (out.refusal) await askForce(out.refusal, { title: 'Force stop?', confirmLabel: 'Force stop', retry: () => stop(true) })
  }

  /* Grow's own 409 (the resize needs the machine stopped while tasks run on it) reaches here from its dialog. */
  async function forceGrow(refusal: NodeCloudStopRefusal, size: number) {
    await askForce(refusal, {
      title: 'Force grow?',
      confirmLabel: 'Force grow',
      retry: () => run(`Couldn’t grow ${nodeName}’s disk`, async () => {
        onChanged(await put<NodeCloud>(`${base}/disk`, { size_gb: size, force: true }))
      }),
    })
  }

  async function askStop() {
    const ok = await confirm({
      title: 'Stop machine?',
      identity: nodeName,
      message: 'The node goes offline until started.',
      confirmLabel: 'Stop',
    })
    if (ok) await stop(false)
  }

  async function askForce(
    refusal: NodeCloudStopRefusal,
    verb: { title: string; confirmLabel: string; retry: () => Promise<unknown> },
  ) {
    const ok = await confirm({
      title: verb.title,
      identity: nodeName,
      message: (
        <>
          {refusal.detail && <Box component="p" sx={{ m: 0, mb: 1 }}>{refusal.detail}</Box>}
          <Box component="ul" data-testid="cloud-stop-tasks" sx={{ m: 0, pl: 2.5, mb: 1, overflowWrap: 'anywhere' }}>
            {refusal.tasks.map(t => (
              <li key={t.id}>
                <Link component={RouterLink} to={`/tasks/${t.id}`} underline="hover">{t.title}</Link>
              </li>
            ))}
          </Box>
          {refusal.tasks.length === 1 ? 'Its run stops and it goes back to Ready.' : 'Their runs stop and they go back to Ready.'}
        </>
      ),
      confirmLabel: verb.confirmLabel,
      destructive: true,
    })
    if (ok) await verb.retry()
  }

  function onVerb(action: NodeCloudAction) {
    if (action === 'stop') void askStop()
    else if (action === 'start') void run(`Couldn’t start ${nodeName}`, async () => onChanged(await post<NodeCloud>(`${base}/start`)))
    else if (action === 'change_type') setDialog('type')
    else if (action === 'grow_disk') setDialog('disk')
  }

  const verbs = VERBS.filter(v => cloud.actions.includes(v.action))

  return (
    <DetailSection
      title={`${cloud.provider_label} machine`}
      testId="node-cloud-machine"
      actions={verbs.length > 0 && (
        <Box data-testid="cloud-actions" sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, minWidth: 0 }}>
          {verbs.map(v => (
            <Button
              key={v.action}
              size="small"
              variant="outlined"
              color={v.action === 'stop' ? 'warning' : 'primary'}
              disabled={busy}
              onClick={() => onVerb(v.action)}
              aria-label={`${v.label} ${nodeName}`}
              data-testid={v.testId}
              sx={ACTION_SX}
            >
              {v.label}
            </Button>
          ))}
        </Box>
      )}
    >
      {/* A row on desktop; one fact per line on a phone, as the Status grid above (label beside value). */}
      <Box component="dl" sx={{ m: 0, display: 'flex', flexWrap: 'wrap', columnGap: 3, rowGap: 1, minWidth: 0, [PHONE]: { flexDirection: 'column', rowGap: 0.5 } }}>
        <DetailField label="Type">
          {cloud.instance_type ? <Mono testId="cloud-type">{cloud.instance_type}</Mono> : 'Unknown'}
        </DetailField>
        <DetailField label="Disk">
          <span data-testid="cloud-disk">{cloud.disk_gb != null ? `${cloud.disk_gb} GB` : 'Unknown'}</span>
        </DetailField>
        <DetailField label="State">
          {/* Starting and Stopping are in progress: the spinner says so. */}
          <StatusChip
            label={state.label}
            tone={state.tone}
            glyph={cloud.state === 'starting' || cloud.state === 'pending' || cloud.state === 'stopping' ? <RunningSpinner /> : undefined}
            data-status={cloud.state ?? 'unknown'}
          />
        </DetailField>
      </Box>
      {change && (
        <Banner severity={changeFailed ? 'error' : 'info'} testId="cloud-change">
          {changeFailed ? 'Type change failed' : 'Changing type'}: {change.from_type} → {change.to_type}
          {lastStep && (
            <>
              {' · '}{lastStep.step}{lastStep.detail ? ` — ${lastStep.detail}` : ''}{' · '}
              <RelativeTime iso={lastStep.at} compact />
            </>
          )}
          {changeFailed && change.error && (
            <Box component="span" data-testid="cloud-change-error" sx={{ display: 'block', overflowWrap: 'anywhere' }}>
              {change.error}
            </Box>
          )}
          {change.state === 'stuck' && (
            <Box component="span" data-testid="cloud-change-stuck" sx={{ display: 'block' }}>
              The change stopped reporting. Check the machine in the {cloud.provider_label} console.
            </Box>
          )}
        </Banner>
      )}
      {rootGrow && (
        <Banner severity={rootGrow.state === 'failed' ? 'error' : 'info'} testId="cloud-root-grow">
          {rootGrow.state === 'failed'
            ? <Box component="span" sx={{ overflowWrap: 'anywhere' }}>{rootGrow.error || ROOT_GROW_FAILED}</Box>
            : `Growing the root filesystem to ${rootGrow.size_gb} GB…`}
        </Banner>
      )}
      {formError && <Banner severity="error" testId="cloud-action-error">{formError}</Banner>}
      {dialog === 'type' && (
        <ChangeTypeDialog base={base} nodeName={nodeName} cloud={cloud} onChanged={onChanged} onClose={() => setDialog(null)} />
      )}
      {dialog === 'disk' && (
        <GrowDiskDialog
          base={base}
          nodeName={nodeName}
          cloud={cloud}
          onChanged={onChanged}
          onClose={() => setDialog(null)}
          onRefused={(refusal, size) => { setDialog(null); void forceGrow(refusal, size) }}
        />
      )}
      {confirmDialog}
    </DetailSection>
  )
}

type DialogProps = {
  base: string
  nodeName: string
  cloud: NodeCloud
  onChanged: (cloud: NodeCloud) => void
  onClose: () => void
}

/* One-shot action dialogs (the style guide's exception): the write is the whole act, so no Save bar. */
function ChangeTypeDialog({ base, nodeName, cloud, onChanged, onClose }: DialogProps): JSX.Element {
  const choices = cloud.instance_types.filter(t => t !== cloud.instance_type)
  const [type, setType] = useState(choices[0] ?? '')
  const { busy, formError, run } = useSubmit()
  return (
    <FormDialog
      title="Change machine type"
      submitLabel="Change"
      busyLabel="Changing…"
      busy={busy}
      error={formError}
      submitDisabled={!type}
      onSubmit={() => void run(`Couldn’t change ${nodeName}’s type`, async () => {
        onChanged(await put<NodeCloud>(`${base}/instance-type`, { instance_type: type }))
        onClose()
      })}
      onClose={onClose}
      maxWidth="xs"
      testId="cloud-type-dialog"
      submitTestId="cloud-type-submit"
    >
      <TextField
        select
        label="Type"
        value={type}
        onChange={e => setType(e.target.value)}
        helperText="The machine restarts."
        sx={fieldTapTargetSx}
        slotProps={{ select: { native: true }, htmlInput: { 'data-testid': 'cloud-type-select' } }}
      >
        {/* The current type is shown, never chosen. */}
        {cloud.instance_type && <option value={cloud.instance_type} disabled>{cloud.instance_type} (current)</option>}
        {choices.map(t => <option key={t} value={t}>{t}</option>)}
      </TextField>
    </FormDialog>
  )
}

function GrowDiskDialog({ base, nodeName, cloud, onChanged, onClose, onRefused }: DialogProps & {
  /** The 409 naming the tasks on the node: the section asks Force grow. */
  onRefused: (refusal: NodeCloudStopRefusal, size: number) => void
}): JSX.Element {
  const current = cloud.disk_gb
  const min = (current ?? 0) + 1
  // The field starts at the disk's size, so the person edits up from what is there; an unknown disk offers none.
  const [raw, setRaw] = useState(current != null ? String(current) : '')
  const [edited, setEdited] = useState(false)
  const { busy, formError, run } = useSubmit()
  const size = Number(raw)
  const valid = raw.trim() !== '' && Number.isInteger(size) && size >= min
  // The untouched pre-fill is not an error yet: only Grow waits for a larger number.
  const showError = !valid && edited
  const message = !showError ? undefined
    : current != null ? `More than ${current} GB` : 'Whole number of GB'
  return (
    <FormDialog
      title="Grow disk"
      submitLabel="Grow"
      busyLabel="Growing…"
      busy={busy}
      error={formError}
      submitDisabled={!valid}
      onSubmit={() => {
        if (!valid) return
        void run(`Couldn’t grow ${nodeName}’s disk`, async () => {
          let answer: NodeCloud
          try {
            answer = await put<NodeCloud>(`${base}/disk`, { size_gb: size })
          } catch (err) {
            const refusal = tasksRefusal(err)
            if (!refusal) throw err
            onRefused(refusal, size)
            return
          }
          onChanged(answer)
          onClose()
        })
      }}
      onClose={onClose}
      maxWidth="xs"
      testId="cloud-disk-dialog"
      submitTestId="cloud-disk-submit"
    >
      <TextField
        label="Size (GB)"
        type="number"
        value={raw}
        onChange={e => { setRaw(e.target.value); setEdited(true) }}
        error={showError}
        helperText={message ?? (current != null ? `Now ${current} GB. Disks only grow.` : 'Disks only grow.')}
        autoFocus
        sx={fieldTapTargetSx}
        slotProps={{
          inputLabel: { shrink: true },
          htmlInput: { min, step: 1, 'data-testid': 'cloud-disk-size' },
          formHelperText: { 'data-testid': 'cloud-disk-hint' } as object,
        }}
      />
    </FormDialog>
  )
}
