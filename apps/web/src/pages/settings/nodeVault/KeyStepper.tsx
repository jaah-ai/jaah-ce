import { useCallback, useEffect, useRef, useState, type JSX, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import LinearProgress from '@mui/material/LinearProgress'
import Link from '@mui/material/Link'
import Step from '@mui/material/Step'
import StepLabel from '@mui/material/StepLabel'
import Stepper from '@mui/material/Stepper'
import Typography from '@mui/material/Typography'
import useMediaQuery from '@mui/material/useMediaQuery'
import { apiDetail } from '../../../api'
import { useConfirm } from '../../../components/ConfirmDialog'
import BusyButton from '../../../ui/BusyButton'
import Banner from '../../../ui/Banner'
import { PHONE_QUERY } from '../../../ui/listStyles'
import { shortHash, type NodeVaultKey } from '../../../lib/nodeVaultKeys'
import { abandonOperation, keyRecipients, type NodeVaultOperation, type NodeVaultStatus } from './api'
import BackupStep from './BackupStep'
import { NODES_KEPT_CHANGING, submitSetup } from './distribute'
import ExplanationStep, { ROTATION_ADVICE } from './ExplanationStep'
import KeyStep, { type KeySource } from './KeyStep'
import MigrateStep from './MigrateStep'
import NodeGrantList from './NodeGrantList'
import ResultStep from './ResultStep'
import { KEY_CHANGED, onlineHolders, runningOperationId, submitRotation } from './rotate'

/* The two key steppers — setup and rotation — share one implementation; `kind` picks the step list and copy.
 * Setup (AC-4.1–4.8): 1 Explanation → 2 Key → 3 Backup → 4 Distribute → 5 Migrate → 6 Result.
 * Rotation (AC-6.1–6.7): 1 Key → 2 Backup → 3 Distribute → 4 Migrate →
 * 5 Rekey → 6 Result.
 * Explanation, Key, Backup and the Distribute submit live in this page — nothing has been sent, so a reload
 * restarts at the first step, and the private key is held by `NodeVaultKey` alone and disposed once the
 * wrapped copies (setup) or the chain blob (rotation) are accepted, or when the page leaves. From the send on,
 * THE STEP IS THE SERVER'S: the running operation's `step` (`GET /api/nodevault`) places the stepper, so a
 * reload or a CP restart lands on the same step with the same numbers (AC-4.5).
 * A rotation's new key goes out wrapped under the CURRENT key (E2) — only a node holding it can open it
 * (AC-6.2). Abandon is offered only before the new key is promoted; after that the way forward is Skip. */

export const STEPS = ['Explanation', 'Key', 'Backup', 'Distribute', 'Migrate', 'Result'] as const
export const ROTATION_STEPS = ['Key', 'Backup', 'Distribute', 'Migrate', 'Rekey', 'Result'] as const
type StepName = (typeof STEPS)[number] | (typeof ROTATION_STEPS)[number]

export const SAME_KEY = 'This is your current key. Generate a new key or upload a different backup.'

/** How long a running setup may show no progress before Abandon is offered outside Distribute (§3.5). */
const STALL_MS = 15 * 60 * 1000

function serverStep(op: NodeVaultOperation, rotation: boolean): StepName {
  if (op.step === 'distribute' || op.step === 'promote') return 'Distribute'
  if (op.step === 'migrate_choice' || op.step === 'migrating') return 'Migrate'
  if (op.step === 'rekey' && rotation) return 'Rekey'
  return rotation || op.step === 'result' ? 'Result' : 'Migrate'
}

/** Rekey progress: the old key's remaining credentials are read off the status (the rows themselves), the
 *  rekeyed and failed ones off the operation's items — a failed one stays on the old key. */
function RekeyProgress({ status, op }: { status: NodeVaultStatus; op: NodeVaultOperation }): JSX.Element {
  const remaining = status.previous_keys.find(k => k.hash === op.prev_key_hash)?.credential_count ?? 0
  const rekeyed = op.counts.rekeyed
  const failed = op.counts.rekey_failed ?? 0
  const total = rekeyed + remaining
  const done = Math.min(total, rekeyed + failed)
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }} data-testid="nodevault-rekey">
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        A node that holds both keys re-seals each secret under the new key. Jaah never sees the secrets.
      </Typography>
      <Box>
        <Typography variant="body2" sx={{ mb: 1 }} data-testid="nodevault-rekey-progress">
          {total === 0 ? 'Checking for credentials on the old key…' : `Rekeyed ${rekeyed} of ${total}`}
        </Typography>
        <LinearProgress
          variant={total === 0 ? 'indeterminate' : 'determinate'}
          value={total === 0 ? undefined : (100 * done) / total}
          aria-label="Rekey progress"
        />
      </Box>
      {op.waiting_for_holder && (
        <Banner severity="warning" testId="nodevault-rekey-waiting">
          Waiting for a node that holds both keys to come online. Start one and the rekey continues.
        </Banner>
      )}
      {failed > 0 && (
        <Typography variant="body2" sx={{ color: 'error.main' }}>
          {failed === 1 ? '1 credential couldn’t be rekeyed.' : `${failed} credentials couldn’t be rekeyed.`} The result says what to do.
        </Typography>
      )}
    </Box>
  )
}

export default function KeyStepper({
  kind,
  status,
  orgId,
  resultOperationId,
  onRefresh,
  onStarted,
  onExit,
}: {
  kind: 'setup' | 'rotation'
  status: NodeVaultStatus
  orgId: string
  /** An operation that has closed and whose Result is still on screen (kept in the URL, so a reload keeps it). */
  resultOperationId: string | null
  onRefresh: () => Promise<void> | void
  /** Rotation: the CP accepted it — remember its id, so its Result is reachable even if it closes before a read. */
  onStarted?: (operationId: string) => void
  /** Leave the stepper: cancelled before sending, abandoned, or Done on the result. */
  onExit: () => void
}): JSX.Element {
  const rotation = kind === 'rotation'
  const steps: readonly StepName[] = rotation ? ROTATION_STEPS : STEPS
  const phone = useMediaQuery(PHONE_QUERY)
  const [confirm, confirmDialog] = useConfirm()
  const op = status.operation?.kind === kind ? status.operation : null
  const currentHash = status.current_key?.hash ?? null

  // ── Browser-only state: before the key is sent ─────────────────────────────────────────────────
  const [localStep, setLocalStep] = useState<StepName>(steps[0])
  const [key, setKey] = useState<NodeVaultKey | null>(null)
  const [source, setSource] = useState<KeySource>('generate')
  const [backupReady, setBackupReady] = useState(false)
  const [sending, setSending] = useState(false)
  // The operation the CP accepted, until a refetch shows it: never back to a fresh Key step.
  const [sent, setSent] = useState(false)
  const [error, setError] = useState('')
  // AC-6.6: the rotation's send was refused because another operation runs.
  const [elsewhere, setElsewhere] = useState(false)
  const keyRef = useRef<NodeVaultKey | null>(null)
  keyRef.current = key

  // Whatever way the page leaves — unmount, or (rotation) a tab closed or reloaded — the private key goes with it.
  useEffect(() => {
    const wipe = () => keyRef.current?.dispose()
    if (rotation) window.addEventListener('pagehide', wipe)
    return () => {
      window.removeEventListener('pagehide', wipe)
      wipe()
    }
  }, [rotation])

  // Once the server shows a running rotation, a refusal from before it no longer applies, and a key this page
  // still holds will never be sent.
  const opId = op?.id ?? null
  useEffect(() => {
    if (!opId || !rotation) return
    setError('')
    setElsewhere(false)
    keyRef.current?.dispose()
    setKey(null)
  }, [opId, rotation])

  const dropKey = useCallback(() => {
    keyRef.current?.dispose()
    setKey(null)
  }, [])

  // ── Server-owned position ──────────────────────────────────────────────────────────────────────
  let step = localStep
  if (op) step = serverStep(op, rotation)
  else if (resultOperationId) step = 'Result'
  else if (sent) step = 'Distribute'
  const stepIndex = steps.indexOf(step)

  // A stalled running setup (no change in its counts or step) offers Abandon after `STALL_MS`.
  const progress = op ? JSON.stringify([op.step, op.counts]) : ''
  const lastProgress = useRef({ progress, at: Date.now() })
  if (lastProgress.current.progress !== progress) lastProgress.current = { progress, at: Date.now() }
  const stalled = !rotation && op !== null && Date.now() - lastProgress.current.at > STALL_MS

  function onKey(next: NodeVaultKey, from: KeySource) {
    keyRef.current?.dispose()
    if (rotation) {
      if (next.keyHash === currentHash) {
        next.dispose()
        setKey(null)
        setError(SAME_KEY)
        return
      }
      setError('')
    }
    setKey(next)
    setSource(from)
    setBackupReady(false)
    // An uploaded key already has its backup: that file.
    setLocalStep(from === 'upload' ? 'Distribute' : 'Backup')
  }

  async function send() {
    if (!key || (rotation && !currentHash)) return
    setSending(true)
    setError('')
    setElsewhere(false)
    try {
      const id = rotation
        ? await submitRotation({ key, orgId, source, currentHash: currentHash as string })
        : await submitSetup({ key, orgId, source, nodes: status.nodes })
      // Accepted: the key's work in this page is done. The server-owned Distribute step takes over once a
      // refetch shows the operation; until then the page says the key was sent (never offers a new one).
      dropKey()
      setSent(true)
      if (rotation) onStarted?.(id)
      await onRefresh()
    } catch (err) {
      if (rotation && runningOperationId(err)) {
        // Another operation owns the org now: this key will never be sent.
        dropKey()
        setElsewhere(true)
      }
      const known = rotation ? KEY_CHANGED : NODES_KEPT_CHANGING
      setError(err instanceof Error && err.message === known ? known : apiDetail(err, 'Couldn’t send the key. Try again.'))
      void onRefresh()
    } finally {
      setSending(false)
    }
  }

  function cancel() {
    dropKey()
    onExit()
  }

  async function abandon() {
    if (!op) return
    const ok = await confirm({
      title: rotation ? 'Abandon rotation?' : 'Abandon setup?',
      message: rotation
        ? 'The new key is removed from every node that received it. Your current key stays in use.'
        : op.step === 'migrating'
          ? 'Credentials not moved yet stay as they are. The key stays in use.'
          : 'The new key is removed from every node that received it. Nothing has been sealed with it yet.',
      confirmLabel: 'Abandon',
      destructive: true,
    })
    if (!ok) return
    setError('')
    try {
      await abandonOperation(op.id)
      onExit()
    } catch (err) {
      setError(apiDetail(err, rotation ? 'Couldn’t abandon the rotation. Try again.' : 'Couldn’t abandon setup. Try again.'))
      if (rotation) void onRefresh()
    }
  }

  // Setup sends to every online node that can hold keys; a rotation's chain needs an online holder of the current key.
  const senders = rotation ? onlineHolders(status.nodes, currentHash) : keyRecipients(status.nodes).filter(n => n.online)
  // Out of the nodes asked to hold this key (a grant each), not the org's roster: one that can't hold keys never will.
  const granted = op ? status.nodes.filter(n => n.grants.some(g => g.key_hash === op.key_hash)) : []
  const delivered = granted.filter(n => n.grants.some(g => g.key_hash === op?.key_hash && g.state === 'delivered')).length
  const newKey = rotation ? 'the new key' : 'the key'
  const cancelButton = <Button onClick={cancel}>Cancel</Button>

  let heading: string
  let body: ReactNode
  let actions: ReactNode = null
  if (step === 'Explanation') {
    heading = 'How NodeVault works'
    body = <ExplanationStep />
    actions = (
      <>
        <Button variant="contained" onClick={() => setLocalStep('Key')}>Next</Button>
        {cancelButton}
      </>
    )
  } else if (step === 'Key' && rotation) {
    heading = 'New key'
    body = (
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <Banner severity="info" testId="nodevault-rotation-advice">{ROTATION_ADVICE}</Banner>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          Make a new key. Your nodes move every NodeVault secret to it; the current key stays on them until you retire it.
        </Typography>
        <KeyStep onKey={onKey} orgId={orgId} actions={cancelButton} />
      </Box>
    )
  } else if (step === 'Key') {
    heading = 'Key'
    body = (
      <KeyStep onKey={onKey} orgId={orgId} actions={
        <>
          <Button onClick={() => setLocalStep('Explanation')}>Back</Button>
          {cancelButton}
        </>
      } />
    )
  } else if (step === 'Backup' && key) {
    heading = 'Backup'
    body = <BackupStep nodeVaultKey={key} orgId={orgId} onReadyChange={setBackupReady} />
    actions = (
      <>
        <Button variant="contained" disabled={!backupReady} onClick={() => setLocalStep('Distribute')}>Next</Button>
        <Button onClick={() => { dropKey(); setLocalStep('Key') }}>Back</Button>
        {cancelButton}
      </>
    )
  } else if (step === 'Distribute' && !op && key) {
    heading = 'Distribute'
    body = (
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          {rotation ? (
            <>
              Key {key.shortHash} is wrapped in this browser under your current key{currentHash ? ` ${shortHash(currentHash)}` : ''}.
              Only your nodes can open it; Jaah relays it without being able to read it.
            </>
          ) : <>Key {key.shortHash} is wrapped in this browser for each node below. Jaah relays only the wrapped copies.</>}
        </Typography>
        <NodeGrantList nodes={status.nodes} keyHash={null} onChanged={onRefresh} preview />
        {senders.length === 0 && (
          <Banner severity="warning">
            {rotation ? 'No node that holds the current key is online.' : 'No node that can hold the key is online.'} Start one, then send.
          </Banner>
        )}
      </Box>
    )
    actions = (
      <>
        <BusyButton variant="contained" busy={sending} disabled={senders.length === 0} onClick={() => void send()}>
          {sending ? 'Sending…' : 'Next'}
        </BusyButton>
        <Button disabled={sending} onClick={() => { if (source === 'upload') { dropKey(); setLocalStep('Key') } else setLocalStep('Backup') }}>Back</Button>
        <Button disabled={sending} onClick={cancel}>Cancel</Button>
      </>
    )
  } else if (step === 'Distribute' && op) {
    heading = 'Distribute'
    body = (
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <Typography variant="body2" sx={rotation ? undefined : { color: 'text.secondary' }} data-testid="nodevault-distribute-note">
          {delivered === 0
            ? `Waiting for a node to confirm it holds ${newKey}. You can leave this page.`
            : `${delivered} of ${granted.length} nodes hold ${newKey}.`}
        </Typography>
        {rotation && (
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>
            The new key goes into use once every online node holds it. Until then your current key stays in use.
          </Typography>
        )}
        <NodeGrantList nodes={status.nodes} keyHash={op.key_hash} onChanged={onRefresh} />
      </Box>
    )
    actions = <Button color="error" onClick={() => void abandon()}>Abandon</Button>
  } else if (step === 'Migrate' && op?.step === 'migrate_choice') {
    heading = 'Migrate'
    body = <MigrateStep frozen={!status.allowed} onChosen={onRefresh} rotation={rotation} />
  } else if (step === 'Migrate' && op) {
    heading = 'Migrate'
    body = <ResultStep operationId={op.id} />
    if (stalled) actions = <Button color="error" onClick={() => void abandon()}>Abandon</Button>
  } else if (step === 'Rekey' && op) {
    heading = 'Rekey'
    body = <RekeyProgress status={status} op={op} />
  } else if (step === 'Result') {
    heading = 'Result'
    body = <ResultStep operationId={op?.id ?? (resultOperationId as string)} onDone={op ? undefined : onExit} />
  } else if (sent) {
    heading = 'Distribute'
    body = (
      <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="nodevault-sent">
        Key sent. Loading node states…
      </Typography>
    )
  } else {
    // A key-holding step whose key is gone: restart at Key.
    heading = rotation ? 'New key' : 'Key'
    body = <KeyStep onKey={onKey} orgId={orgId} actions={rotation ? cancelButton : undefined} />
  }

  const backup = steps.indexOf('Backup')
  return (
    <Box
      component="section"
      aria-labelledby={rotation ? 'nodevault-rotation-title' : 'nodevault-step-title'}
      data-testid={`nodevault-${kind}`}
      data-step={step}
      sx={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}
    >
      {rotation && <Typography variant="body2" sx={{ fontWeight: 600 }}>Rotate key</Typography>}
      {phone ? (
        <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="nodevault-step-count">
          Step {stepIndex + 1} of {steps.length}: {step}
        </Typography>
      ) : (
        <Stepper activeStep={stepIndex} aria-label="Steps" alternativeLabel>
          {steps.map((label, i) => (
            <Step key={label} completed={i < stepIndex}>
              <StepLabel optional={i === backup && source === 'upload' && stepIndex > backup && !(rotation && op) ? <Typography variant="caption">Not needed</Typography> : undefined}>
                {label}
              </StepLabel>
            </Step>
          ))}
        </Stepper>
      )}
      <Typography id={rotation ? 'nodevault-rotation-title' : 'nodevault-step-title'} component="h3" variant="subtitle1" sx={{ fontWeight: 600 }}>{heading}</Typography>
      {body}
      {error && (
        <Banner severity="error" testId={`nodevault-${kind}-error`}>
          {error}
          {elsewhere && (
            <>
              {' '}
              <Link component="button" type="button" variant="body2" onClick={cancel} sx={{ verticalAlign: 'baseline' }}>
                Show it
              </Link>
            </>
          )}
        </Banner>
      )}
      {actions && <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>{actions}</Box>}
      {confirmDialog}
    </Box>
  )
}
