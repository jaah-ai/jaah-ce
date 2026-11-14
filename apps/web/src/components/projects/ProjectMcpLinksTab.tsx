import { useMemo, useState, type JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../api'
import { grid } from '../../theme'
import { useApiRead } from '../../hooks/useApiRead'
import { useSubmit } from '../../hooks/useSubmit'
import { loadCredentialOptions, type Credential } from '../../pages/credentials/api'
import { linkProjectMcp, listProjectMcpLinks, unlinkProjectMcp } from '../../pages/credentials/integrations/api'
import { useCredentialTypes } from '../../pages/credentials/useCredentialTypes'
import Banner from '../../ui/Banner'
import CredentialSelect, { type CredentialOption } from '../../ui/CredentialSelect'
import { credentialMark } from '../../ui/credentialMarks'
import EmptyState from '../../ui/EmptyState'
import FormField from '../../ui/FormField'
import LoadFailedState from '../../ui/LoadFailedState'
import LoadingState from '../../ui/LoadingState'
import { tapTargetSx } from '../../ui/listStyles'

/* A PROJECT'S MCP LINKS (`/projects/:id?tab=mcp`): the MCP connections its sessions reach. Being visible to a project
 * is not enough — a project opts in with a link (`app/mcp_links.py`, routes `routes/credentials/mcp_links.py`).
 * Link picks an MCP connection the viewer can see; Unlink removes the link (the pool attachment stays). Editing needs
 * `projects:edit` here and the connection's owner or an org admin: the server decides. */
export default function ProjectMcpLinksTab({ projectId, canEdit }: { projectId: number; canEdit: boolean }): JSX.Element {
  const links = useApiRead(['project-mcp-links', projectId], () => listProjectMcpLinks(projectId), 'Failed to load MCP links')
  const { types } = useCredentialTypes()
  const mcpTypes = useMemo(() => types.filter(spec => spec.mcp === true).map(spec => spec.slug), [types])
  const [picked, setPicked] = useState<string | null>(null)
  const { busy, formError, setFormError, run } = useSubmit()

  const toOption = (row: Credential): CredentialOption => ({
    id: row.id, name: row.name, hint: row.type_label, icon: credentialMark(row.type, row.type_label),
  })

  async function link() {
    if (picked === null) return
    await run('Failed to link', async () => {
      await linkProjectMcp(projectId, picked)
      setPicked(null)
      await links.reload()
    }, { mapError: (err, fallback) => apiDetail(err, fallback) })
  }

  async function unlink(credentialId: string) {
    setFormError('')
    await run('Failed to unlink', async () => {
      await unlinkProjectMcp(projectId, credentialId)
      await links.reload()
    }, { mapError: (err, fallback) => apiDetail(err, fallback) })
  }

  let list: JSX.Element
  if (links.loading) list = <LoadingState label="Loading MCP links…" />
  else if (links.error) list = <LoadFailedState title="MCP links unavailable" error={links.error} onRetry={() => void links.reload()} />
  else if (!links.data || links.data.length === 0) list = <EmptyState title="No MCP server linked" />
  else {
    list = (
      <Box component="ul" data-testid="project-mcp-links" sx={{ listStyle: 'none', m: 0, p: 0, display: 'flex', flexDirection: 'column', gap: 1 }}>
        {links.data.map(row => (
          <Box
            component="li"
            key={row.credential.id}
            data-testid={`project-mcp-link-${row.credential.id}`}
            sx={{ display: 'flex', alignItems: 'center', gap: 1, minWidth: 0, flexWrap: 'wrap' }}
          >
            <Box sx={{ display: 'flex', flexShrink: 0 }}>{credentialMark(row.credential.type, row.credential.type_label)}</Box>
            <Box sx={{ minWidth: 0, flex: `1 1 ${grid(32)}` }}>
              <Typography component={RouterLink} to={`/credentials/${row.credential.id}?tab=mcp`} variant="body2" sx={{ overflowWrap: 'anywhere' }}>
                {row.credential.name}
              </Typography>
              <Typography variant="caption" component="div" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
                {row.url ?? 'No usable server URL'}
              </Typography>
            </Box>
            {canEdit && (
              <Button
                size="small"
                color="inherit"
                onClick={() => void unlink(row.credential.id)}
                disabled={busy}
                aria-label={`Unlink ${row.credential.name}`}
                sx={tapTargetSx}
              >
                Unlink
              </Button>
            )}
          </Box>
        ))}
      </Box>
    )
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1.5 }} data-testid="project-mcp-tab">
      {formError && <Banner severity="error">{formError}</Banner>}
      {list}
      {canEdit && mcpTypes.length > 0 && (
        <Box sx={{ display: 'flex', alignItems: 'flex-end', gap: 1, flexWrap: 'wrap' }}>
          <Box sx={{ flex: `1 1 ${grid(42.5)}`, minWidth: 0 }}>
            <FormField label="MCP connection" help="Linking lets this project's sessions reach that MCP server.">
              <CredentialSelect
                id="project-mcp-pick"
                testId="project-mcp-pick"
                value={picked}
                onChange={setPicked}
                fetchOptions={(query, signal) => loadCredentialOptions(query, signal, toOption, { types: mcpTypes, sort: true })}
                placeholder="Search by name"
                noOptionsText="No MCP connection — add one under Secrets."
              />
            </FormField>
          </Box>
          <Button
            variant="contained"
            onClick={() => void link()}
            disabled={picked === null || busy}
            data-testid="project-mcp-link"
            sx={[tapTargetSx, { mb: 1 }]}
          >
            Link
          </Button>
        </Box>
      )}
    </Box>
  )
}
