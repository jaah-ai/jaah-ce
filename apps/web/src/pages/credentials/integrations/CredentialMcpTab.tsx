import { type JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { useApiRead } from '../../../hooks/useApiRead'
import EmptyState from '../../../ui/EmptyState'
import LoadFailedState from '../../../ui/LoadFailedState'
import LoadingState from '../../../ui/LoadingState'
import RelativeTime from '../../../ui/RelativeTime'
import { listCredentialMcpLinks } from './api'

/* AN MCP CONNECTION'S OWN TAB (`/credentials/:id?tab=mcp`): the projects that link it, so their sessions reach the
 * server (`GET /api/credentials/{id}/mcp-links`; a project the viewer cannot see is not named). A link is made and
 * removed on the project's own MCP tab, where its editors are. */
export default function CredentialMcpTab({ credentialId }: { credentialId: string }): JSX.Element {
  const { data, loading, error, reload } = useApiRead(
    ['credential-mcp-links', credentialId], () => listCredentialMcpLinks(credentialId), 'Failed to load linked projects',
  )
  if (loading) return <LoadingState label="Loading linked projects…" />
  if (error) return <LoadFailedState title="Linked projects unavailable" error={error} onRetry={() => void reload()} />
  if (!data || data.length === 0) {
    return <EmptyState title="No project links it yet" />
  }
  return (
    <Box component="ul" data-testid="credential-mcp-links" sx={{ listStyle: 'none', m: 0, p: 0, display: 'flex', flexDirection: 'column', gap: 1 }}>
      {data.map(link => (
        <Box component="li" key={link.project_id} sx={{ display: 'flex', alignItems: 'baseline', gap: 1, minWidth: 0 }}>
          <Typography
            component={RouterLink}
            to={`/projects/${link.project_id}?tab=customize&section=mcp`}
            variant="body2"
            sx={{ minWidth: 0, overflowWrap: 'anywhere' }}
          >
            {link.project_name}
          </Typography>
          <Typography variant="caption" sx={{ color: 'text.secondary', flexShrink: 0 }}>
            Linked <RelativeTime iso={link.created_at} />
          </Typography>
        </Box>
      ))}
    </Box>
  )
}
