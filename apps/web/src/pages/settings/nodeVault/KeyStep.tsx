import { useEffect, useRef, useState, type JSX, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import BusyButton from '../../../ui/BusyButton'
import Banner from '../../../ui/Banner'
import FormTextField from '../../../ui/FormTextField'
import SegmentedControl from '../../../ui/SegmentedControl'
import { VISUALLY_HIDDEN } from '../../../ui/visuallyHidden'
import { useSubmit } from '../../../hooks/useSubmit'
import { generateNodeVaultKey, restoreBackup, type NodeVaultKey } from '../../../lib/nodeVaultKeys'

/* The Key step — Generate or Upload (AC-4.2, AC-4.4). Reused by Setup and Rotation. The private key exists
 * only inside the `NodeVaultKey` handed to `onKey`, in this page's memory: nothing here sends anything, and
 * a reload restarts the step (architecture §3.6). Upload opens a backup with its passphrase; ANY failure —
 * a wrong passphrase, a damaged or foreign file — is the one sentence below, because AES-GCM cannot tell
 * them apart and the admin's next action is the same. The step's one Next generates or opens the key and moves
 * on, like every other step's Next; the caller's Back and Cancel sit beside it. */

export type KeySource = 'generate' | 'upload'

const MAX_BACKUP_BYTES = 64 * 1024

export const BACKUP_OPEN_FAILED = 'Couldn’t open this backup — check the passphrase'
export const OTHER_ORG = 'This backup belongs to another organization. Use one made here, or generate a key.'

export default function KeyStep({
  onKey,
  orgId,
  actions,
}: {
  onKey: (key: NodeVaultKey, source: KeySource) => void
  /** This organization: a backup made for another one is refused (a key is never shared across orgs). */
  orgId: string
  /** The stepper's Back / Cancel, after Next. */
  actions?: ReactNode
}): JSX.Element {
  const [mode, setMode] = useState<KeySource>('generate')
  const { busy, formError: error, setFormError: setError, run } = useSubmit()
  const [file, setFile] = useState<File | null>(null)
  const [passphrase, setPassphrase] = useState('')
  // A key that arrives after the admin left this step (Back, Cancel) is disposed here, never handed on.
  const live = useRef(true)
  useEffect(() => {
    live.current = true
    return () => { live.current = false }
  }, [])

  function deliver(key: NodeVaultKey, source: KeySource) {
    if (live.current) onKey(key, source)
    else key.dispose()
  }

  // Any failure is the one sentence (see above): the error is never the exception's own.
  const generate = () =>
    run('Couldn’t generate a key in this browser. Try a current Chrome, Edge, Firefox or Safari.',
      async () => deliver(await generateNodeVaultKey(), 'generate'), { mapError: (_err, fallback) => fallback })

  async function upload() {
    if (!file) return
    await run(BACKUP_OPEN_FAILED, async () => {
      // A real backup is ~4 KB; refuse a huge file before reading it into memory.
      if (file.size > MAX_BACKUP_BYTES) throw new Error('too large')
      const text = await file.text()
      const key = await restoreBackup(text, passphrase)
      if ((JSON.parse(text) as { org_id?: unknown }).org_id !== orgId) {
        key.dispose()
        setError(OTHER_ORG)
        return
      }
      deliver(key, 'upload')
    }, { mapError: (_err, fallback) => fallback })
    setPassphrase('')
  }

  const fromBackup = mode === 'upload'
  return (
    <Box
      component="form"
      onSubmit={e => { e.preventDefault(); void (fromBackup ? upload() : generate()) }}
      sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}
      data-testid="nodevault-key-step"
    >
      <SegmentedControl
        ariaLabel="Key"
        value={mode}
        onChange={next => { setMode(next as KeySource); setError('') }}
        options={[{ value: 'generate', label: 'Generate' }, { value: 'upload', label: 'Upload backup' }]}
      />
      {fromBackup ? (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, maxWidth: 420 }}>
          <Box>
            <Button variant="outlined" component="label">
              {file ? 'Change file' : 'Choose backup file'}
              <input
                style={VISUALLY_HIDDEN}
                type="file"
                accept=".json,application/json"
                data-testid="nodevault-backup-file"
                onChange={e => { setFile(e.target.files?.[0] ?? null); setError('') }}
              />
            </Button>
            {file && <Typography variant="body2" component="span" sx={{ ml: 1.5 }}>{file.name}</Typography>}
          </Box>
          <FormTextField label="Passphrase" type="password" autoComplete="off" value={passphrase}
            onChange={setPassphrase} testId="nodevault-upload-passphrase" />
        </Box>
      ) : (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          Your browser makes the key pair. The private key never leaves this page unencrypted.
        </Typography>
      )}
      {error && <Banner severity="error" testId="nodevault-key-error">{error}</Banner>}
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
        <BusyButton type="submit" variant="contained" busy={busy} disabled={fromBackup && (!file || passphrase === '')}>
          {busy ? (fromBackup ? 'Opening…' : 'Generating…') : 'Next'}
        </BusyButton>
        {actions}
      </Box>
    </Box>
  )
}
