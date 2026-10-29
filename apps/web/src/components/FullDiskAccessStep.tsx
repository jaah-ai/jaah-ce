import { useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { ApiError, apiDetail, apiFetch, noticeRefusal } from '../api'
import { CopyableValue } from '../ui/CopyableValue'
import { DownloadGlyph } from '../ui/DownloadGlyph'
import { downloadText } from '../lib/downloadText'
import type { NodeOsGuide } from '../lib/nodeOs'

const PROFILE_FILE = 'jaah-full-disk-access.mobileconfig'

/* macOS, once the line has run: grant the one launcher every Jaah job runs through Full Disk Access (macOS keeps
 * launchd jobs off a network share without it), by hand — or, on a Mac an MDM manages, as the API's profile.
 * Read through `apiFetch`, not `getText`, so a 404 (no Team ID configured) shows the API's own detail. */
export function FullDiskAccessStep({ grant }: { grant: NonNullable<NodeOsGuide['fullDiskAccess']> }) {
  const [error, setError] = useState<string | null>(null)
  const download = async () => {
    setError(null)
    try {
      const resp = await apiFetch(grant.profilePath, { credentials: 'same-origin' })
      if (!resp.ok) {
        const body = await resp.clone().json().catch(() => null)
        await noticeRefusal(resp)
        throw new ApiError(resp.status, body?.detail ?? null, body)
      }
      downloadText(PROFILE_FILE, await resp.text(), 'application/x-apple-aspen-config')
    } catch (err) {
      setError(apiDetail(err, 'Could not download the profile'))
    }
  }
  return (
    <Box data-testid="node-onboarding-fda">
      <Typography variant="body2" sx={{ fontWeight: 600 }}>Then allow Full Disk Access — NodeLinks need it.</Typography>
      <Box component="ol" sx={{ m: 0, mt: 0.5, pl: 3, '& li': { mb: 0.5 } }}>
        <Typography component="li" variant="body2">Open System Settings → Privacy &amp; Security → Full Disk Access.</Typography>
        <Typography component="li" variant="body2">Click +, then press ⌘⇧G.</Typography>
        <Typography component="li" variant="body2">
          Paste
          <CopyableValue value={grant.path} copyLabel="Copy path" sx={{ overflowWrap: 'anywhere' }} />
        </Typography>
        <Typography component="li" variant="body2">Turn it on.</Typography>
      </Box>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          Managed by MDM? Install the access profile instead.
        </Typography>
        <Button
          variant="text"
          size="small"
          onClick={download}
          startIcon={<DownloadGlyph size={14} />}
          data-testid="node-onboarding-fda-profile"
        >
          Download profile
        </Button>
      </Box>
      {error && (
        <Typography variant="body2" sx={{ color: 'error.main' }} data-testid="node-onboarding-fda-profile-error">
          {error}
        </Typography>
      )}
    </Box>
  )
}
