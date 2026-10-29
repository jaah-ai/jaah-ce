import { useEffect, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { get } from '../../api'
import { useApiRead } from '../../hooks/useApiRead'
import { listProjects } from '../../hooks/useRosters'
import { useSubmit } from '../../hooks/useSubmit'
import { actAsOnce } from '../../lib/actAs'
import type { OrgRef, Project } from '../../types'
import Banner from '../../ui/Banner'
import { HomeChip } from '../../ui/MarkChip'
import EmptyState from '../../ui/EmptyState'
import LoadingState from '../../ui/LoadingState'
import { fieldTapTargetSx, tapTargetSx } from '../../ui/listStyles'
import { useConfirm } from '../ConfirmDialog'
import {
  attachCredential,
  detachCredential,
  getCredentialAccess,
  type CredentialAccess,
} from './api'

// A credential's Access tab: its owner and the projects whose runners receive it. Attach/Detach are
// server-gated (a 403 shows its own message); nothing here decides who may. `otherOrg`: the credential's org when
// an operator views another org's row (`ui/OrgIndicator.tsx::useOtherOrg`) — the picker lists that org's projects
// and every write goes as that org for the one request (`lib/actAs.ts::actAsOnce`).
export default function CredentialAccessTab({ credentialId, otherOrg = null }: {
  credentialId: string
  otherOrg?: OrgRef | null
}) {
  const asOrg = actAsOnce(otherOrg)
  // The picker re-reads on the org's id, never on a parent's fresh `OrgRef` object.
  const otherOrgId = otherOrg?.id
  const { data: access, loading, error: loadError, reload } = useApiRead(
    ['credential-access-tab', credentialId],
    () => getCredentialAccess(credentialId),
    'Failed to load access',
  )
  const [projects, setProjects] = useState<Project[]>([])
  const [choice, setChoice] = useState('')
  const { busy, formError: error, run } = useSubmit()
  const [confirm, confirmDialog] = useConfirm()

  useEffect(() => {
    // The picker is optional: a failed project list leaves Attach empty, not the tab broken.
    const read = asOrg ? get<Project[]>('/api/projects', { headers: asOrg }) : listProjects()
    read
      .then(rows => setProjects(Array.isArray(rows) ? rows : []))
      .catch(() => setProjects([]))
  }, [otherOrgId])

  async function handleAttach() {
    if (!choice) return
    await run('Failed to attach', async () => {
      await attachCredential(choice, credentialId, asOrg)
      await reload()
      setChoice('')
    })
  }

  async function handleDetach(project: CredentialAccess['projects'][number]) {
    const ok = await confirm({
      title: 'Detach credential?',
      message: `${project.name}'s runners stop receiving this credential.`,
      confirmLabel: 'Detach',
      destructive: true,
    })
    if (ok) await run('Failed to detach', async () => {
      await detachCredential(project.id, credentialId, asOrg)
      await reload()
    })
  }

  if (loading && access === null) return <LoadingState />

  const attached = new Set((access?.projects ?? []).map(p => String(p.id)))
  const available = projects.filter(p => !attached.has(String(p.id)))

  return (
    <Box data-testid="credential-access" sx={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {error && <Banner severity="error">{error}</Banner>}

      {access && (
        <>
          <Box>
            <Typography variant="subtitle2" component="h3">Owner</Typography>
            <Typography data-testid="credential-owner" sx={{ overflowWrap: 'anywhere' }}>
              {access.owner
                ? <Link component={RouterLink} to={`/users/${access.owner.id}`}>{access.owner.name}</Link>
                : access.system ? 'System' : 'Organization'}
            </Typography>
          </Box>

          <Box>
            <Typography variant="subtitle2" component="h3">Projects</Typography>
            {access.projects.length === 0
              ? <EmptyState title="No projects." />
              : (
                <Stack component="ul" sx={{ listStyle: 'none', p: 0, m: 0 }}>
                  {access.projects.map(project => (
                    <Box
                      component="li"
                      key={project.id}
                      data-testid="credential-project-row"
                      sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1, minWidth: 0 }}
                    >
                      <Box sx={{ minWidth: 0 }}>
                        <Link
                          component={RouterLink}
                          to={`/projects/${project.id}`}
                          sx={{ minWidth: 0, overflowWrap: 'anywhere' }}
                        >
                          {project.name}
                        </Link>
                        {project.home && <HomeChip />}
                      </Box>
                      {/* The home project is not detached here: the Details tab's Home moves it (the API 409s). */}
                      {!project.home && (
                      <Button
                        data-testid="credential-detach"
                        variant="text"
                        color="error"
                        disabled={busy}
                        onClick={() => void handleDetach(project)}
                        sx={tapTargetSx}
                      >
                        Detach
                      </Button>
                      )}
                    </Box>
                  ))}
                </Stack>
              )}
          </Box>

          {access.system ? (
            <Typography sx={{ color: 'text.secondary' }} data-testid="credential-system-note">
              System credentials cannot be attached to a project.
            </Typography>
          ) : (
          <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
            <TextField
              id="credential-attach-project"
              label="Project"
              select
              size="small"
              value={choice}
              onChange={event => setChoice(event.target.value)}
              slotProps={{ select: { native: true }, inputLabel: { shrink: true } }}
              sx={[fieldTapTargetSx, { flex: '1 1 12rem', minWidth: 0 }]}
            >
              <option value="">—</option>
              {available.map(project => (
                <option key={project.id} value={String(project.id)}>{project.name}</option>
              ))}
            </TextField>
            <Button
              data-testid="credential-attach"
              variant="contained"
              disabled={busy || !choice}
              onClick={() => void handleAttach()}
              sx={tapTargetSx}
            >
              Attach
            </Button>
          </Box>
          )}
        </>
      )}
      {confirmDialog}
    </Box>
  )
}
