import { useEffect, useState, type JSX } from 'react'
import Box from '@mui/material/Box'
import Checkbox from '@mui/material/Checkbox'
import FormControlLabel from '@mui/material/FormControlLabel'
import Typography from '@mui/material/Typography'
import BusyButton from '../../../ui/BusyButton'
import Banner from '../../../ui/Banner'
import FormTextField from '../../../ui/FormTextField'
import { useSubmit } from '../../../hooks/useSubmit'
import { downloadText } from '../../../lib/downloadText'
import { createBackup, MIN_PASSPHRASE_LENGTH, passphraseAcceptable, type NodeVaultKey } from '../../../lib/nodeVaultKeys'

/* The mandatory Backup step (AC-4.3, D4). Reused by Setup and Rotation. The file is encrypted IN THE
 * BROWSER under the passphrase (entered twice, at least `MIN_PASSPHRASE_LENGTH`) and handed to the browser's
 * download — it is never sent anywhere. `onReadyChange(true)` only once the download has fired for the
 * CURRENT passphrase AND the confirmation is ticked; the caller's Next reads it. Jaah keeps no copy, so
 * there is no recovery without this file. */

export const BACKUP_CONFIRM = 'I stored the backup; Jaah cannot recover it'

export function backupFileName(key: NodeVaultKey): string {
  return `jaah-nodevault-backup-${key.shortHash}.json`
}

export default function BackupStep({
  nodeVaultKey,
  orgId,
  onReadyChange,
}: {
  nodeVaultKey: NodeVaultKey
  orgId: string
  onReadyChange: (ready: boolean) => void
}): JSX.Element {
  const [passphrase, setPassphrase] = useState('')
  const [again, setAgain] = useState('')
  const [downloaded, setDownloaded] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const { busy, formError: error, run } = useSubmit()

  const tooShort = passphrase !== '' && !passphraseAcceptable(passphrase)
  const mismatch = again !== '' && again !== passphrase
  const valid = passphraseAcceptable(passphrase) && again === passphrase

  useEffect(() => { onReadyChange(downloaded && confirmed) }, [downloaded, confirmed, onReadyChange])

  function edit(setter: (v: string) => void, value: string) {
    setter(value)
    // A file saved under the previous passphrase is not the one the admin will remember.
    setDownloaded(false)
  }

  const save = () =>
    run('Couldn’t create the backup. Try again.', async () => {
      const backup = await createBackup(nodeVaultKey, { orgId, passphrase })
      downloadText(backupFileName(nodeVaultKey), JSON.stringify(backup, null, 2))
      setDownloaded(true)
    }, { mapError: (_err, fallback) => fallback })

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, maxWidth: 420 }} data-testid="nodevault-backup-step">
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        Jaah keeps no copy of this key. Lose every node and this file, and NodeVault secrets are gone.
      </Typography>
      <FormTextField
        label="Passphrase" error={tooShort ? `At least ${MIN_PASSPHRASE_LENGTH} characters` : undefined}
        type="password"
        autoComplete="new-password"
        value={passphrase}
        onChange={v => edit(setPassphrase, v)}
        testId="nodevault-backup-passphrase"
      />
      <FormTextField
        label="Repeat passphrase" error={mismatch ? 'Passphrases don’t match' : undefined}
        type="password"
        autoComplete="new-password"
        value={again}
        onChange={v => edit(setAgain, v)}
        testId="nodevault-backup-passphrase-again"
      />
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
        <BusyButton variant="outlined" onClick={() => void save()} busy={busy} disabled={!valid}>
          {busy ? 'Encrypting…' : 'Download backup'}
        </BusyButton>
        {downloaded && <Typography variant="body2" sx={{ color: 'success.main' }} data-testid="nodevault-backup-downloaded">Downloaded</Typography>}
      </Box>
      <FormControlLabel
        control={<Checkbox checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />}
        label={BACKUP_CONFIRM}
      />
      {error && <Banner severity="error">{error}</Banner>}
    </Box>
  )
}
