import { useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Paper from '@mui/material/Paper'
import Typography from '@mui/material/Typography'
import { useAuth, useCan, useCanOnAnyProject } from '../../auth'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { cellOn } from '../../lib/providerGrid'
import { useOrgScope } from '../../lib/orgScope'
import { useConfirm } from '../../components/ConfirmDialog'
import { useApiRead } from '../../hooks/useApiRead'
import { useBusyIds } from '../../hooks/useBusyIds'
import { CompactVerbLabel, compactVerbProps } from '../../ui/CompactVerb'
import Banner from '../../ui/Banner'
import EmptyState from '../../ui/EmptyState'
import { tapTargetSx } from '../../ui/listStyles'
import LoadingState from '../../ui/LoadingState'
import { StatusChip } from '../../ui/StatusChip'
import { AgentsStrip, useAgentsVerbs } from './agentsTabs'
import {
  type Agent,
  agentTypeLabel,
  CONNECTORS_REPO_URL,
  languageLabel,
  listCatalog,
  publishConnector,
  refusalSentences,
  REMOVED,
  removeConnector,
  setConnectorEnabled,
  shortCommit,
} from './api'
import { type Notice, NoticeBanner, SnapshotDialog } from './SnapshotDialog'

/* ── DIRECTORY > AGENTS > CATALOG: APPROVED CONNECTORS (AC-13) ─────────────────────────────────────────────
   `GET /api/agent-catalog` — every active connector with this org's `enabled` flag, plus a withdrawn one only
   where this org enabled it: that card says "Removed from catalog" (tasks naming it fail to start, saying so),
   never vanishes. Enable/Disable is per org and takes `agents:edit` org-wide (`_require_org_editor`); an enabled
   connector is in the task picker (`components/agents/AgentPicker.tsx`). Publish (a repo, sub-path and exact
   commit) and Remove are the operator's (`is_operator`); the server is the boundary. Cards wrap their names
   (AC-9's readability rule), one column on a phone.
   An operator's Orgs filter picks whose `enabled` flags show (`routes/agents.py::_enabled_ids`; All is their own
   org's). A toggle writes the operator's own org, so it is withheld while the filter names another. */

export default function CatalogPage() {
  const { admin } = useAuth()
  const can = useCan()
  const canOnAny = useCanOnAnyProject()
  const scope = useOrgScope()
  const isOperator = admin?.is_operator === true
  const { canPublish } = useAgentsVerbs()
  // The flags shown are another org's: the operator's filter names any org but their own.
  const otherOrgs = isOperator && scope !== null && !(scope.length === 1 && scope[0].id === admin?.org?.id)
    ? scope
    : null
  /* On or off for the WHOLE org takes `agents:edit` org-wide (`_require_org_editor`); `useCan` also answers for a
   * project-scoped grant, so the org admin seat is the presentation gate. The server stays the boundary. */
  const canToggle = isOrgAdminSeat(admin) && otherOrgs === null
  /* Enabling one needs the SDK agent row on the org's grid (the API refuses otherwise); disabling never does. */
  const canEnable = cellOn(admin, 'sdk', 'own')
  // An agent editor on some project, but not an org admin: say why there is no toggle.
  const editsElsewhere = !isOrgAdminSeat(admin) && (can('agents:edit') || canOnAny('agents:edit'))
  const { data, loading, error, refresh } = useApiRead(['agent-catalog'], () => listCatalog(), 'Could not load the catalog')
  const [busy, withBusy] = useBusyIds()
  const [notice, setNotice] = useState<Notice | null>(null)
  const [publishing, setPublishing] = useState(false)
  const [confirm, confirmDialog] = useConfirm()

  const run = (connector: Agent, done: string, call: () => Promise<unknown>) => withBusy(connector.id, async () => {
    setNotice(null)
    try {
      await call()
      setNotice({ severity: 'success', lines: [done] })
    } catch (err) {
      setNotice({ severity: 'error', lines: refusalSentences(err, `Could not change ${connector.display_name}`) })
    }
  }).finally(refresh)

  async function withdraw(connector: Agent) {
    const ok = await confirm({
      title: `Remove ${connector.display_name}?`,
      message: 'It leaves the catalog. Orgs that enabled it see it marked removed, and their tasks using it fail to start.',
      confirmLabel: 'Remove',
      destructive: true,
    })
    if (ok) await run(connector, `Removed ${connector.display_name} from the catalog.`, () => removeConnector(connector.id))
  }

  const connectors = data ?? []
  return (
    <Box>
      <AgentsStrip
        tab="catalog"
        actions={canPublish && (
          <Button variant="contained" color="primary" {...compactVerbProps('Publish', tapTargetSx)} onClick={() => setPublishing(true)}>
            <CompactVerbLabel label="Publish" />
          </Button>
        )}
      />
      {error && <Banner severity="error">{error}</Banner>}
      {/* Nothing listed, nothing to enable: the admin-only note waits for a first connector. */}
      {(otherOrgs || (editsElsewhere && connectors.length > 0)) && (
        <Typography variant="body2" sx={{ color: 'text.secondary', pt: 1 }} data-testid="catalog-toggle-note">
          {otherOrgs
            ? `Showing what ${otherOrgs.map(org => org.name).join(', ')} enabled. Clear the Orgs filter to change your org's.`
            : 'Only admins can enable catalog agents.'}
        </Typography>
      )}
      <NoticeBanner notice={notice} onDismiss={() => setNotice(null)} testId="catalog-notice" />
      {loading && data === null ? (
        <LoadingState rows={3} />
      ) : connectors.length === 0 ? (
        /* One link: the how-to rides the action's tooltip, not a second paragraph (AC-13). */
        <EmptyState
          title="No connectors yet"
          action={(
            <Button component="a" href={CONNECTORS_REPO_URL} target="_blank" rel="noopener noreferrer"
              data-testid="catalog-contribute"
              title="Build one with the Jaah agent SDK; the repo's CONTRIBUTING guide has the steps.">
              Contribute one
            </Button>
          )}
        />
      ) : (
        <Box
          data-testid="catalog-cards"
          sx={{ display: 'grid', gap: 1.5, pt: 1.5, gridTemplateColumns: 'repeat(auto-fill, minmax(min(100%, 18rem), 1fr))' }}
        >
          {connectors.map(connector => (
            <ConnectorCard
              key={connector.id}
              connector={connector}
              busy={busy.has(connector.id)}
              canToggle={canToggle}
              canEnable={canEnable}
              isOperator={isOperator}
              onToggle={enabled => void run(
                connector,
                `${enabled ? 'Enabled' : 'Disabled'} ${connector.display_name}.`,
                () => setConnectorEnabled(connector.id, enabled),
              )}
              onRemove={() => void withdraw(connector)}
            />
          ))}
        </Box>
      )}
      {publishing && (
        <SnapshotDialog
          connector
          title="Publish connector"
          submitLabel="Publish"
          initial={{ repo_url: CONNECTORS_REPO_URL, subpath: '', ref: '' }}
          onClose={() => setPublishing(false)}
          onSave={async body => {
            const connector = await publishConnector(body)
            setNotice({ severity: 'success', lines: [`Published ${connector.display_name}.`] })
            await refresh()
          }}
        />
      )}
      {confirmDialog}
    </Box>
  )
}

function ConnectorCard({ connector, busy, canToggle, canEnable, isOperator, onToggle, onRemove }: {
  connector: Agent
  busy: boolean
  canToggle: boolean
  canEnable: boolean
  isOperator: boolean
  onToggle: (enabled: boolean) => void
  onRemove: () => void
}) {
  const removed = connector.status === REMOVED
  const enabled = connector.enabled === true
  const facts = [
    agentTypeLabel(connector.agent_type),
    languageLabel(connector.language),
    connector.version ? `v${connector.version.replace(/^v/, '')}` : null,
    shortCommit(connector.resolved_commit),
  ].filter(Boolean).join(' · ')
  return (
    <Paper
      variant="outlined"
      data-testid="catalog-card"
      data-connector={connector.slug}
      sx={{ p: 1.5, display: 'flex', flexDirection: 'column', gap: 0.75, minWidth: 0, opacity: removed ? 0.85 : 1 }}
    >
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1 }}>
        {/* The whole name, wrapped: never clipped to a few characters. */}
        <Typography
          variant="subtitle2"
          component="h3"
          title={connector.display_name}
          sx={{ flex: 1, minWidth: 0, overflowWrap: 'anywhere' }}
        >
          {connector.display_name}
        </Typography>
        {removed
          ? <StatusChip tone="error" label="Removed from catalog" glyph="✕" labelTestId="catalog-removed" />
          : enabled && <StatusChip tone="success" label="Enabled" glyph="✓" labelTestId="catalog-enabled" />}
      </Box>
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>
        {facts}{connector.maintainer ? ` · by ${connector.maintainer}` : ''}
      </Typography>
      {connector.description && <Typography variant="body2">{connector.description}</Typography>}
      {removed && (
        <Typography variant="caption" sx={{ color: 'error.main' }}>
          Tasks using it fail to start. Pick another agent for them.
        </Typography>
      )}
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mt: 'auto', pt: 0.5 }}>
        {canToggle && (enabled || (!removed && canEnable)) && (
          <Button
            size="small"
            variant={enabled ? 'outlined' : 'contained'}
            disabled={busy}
            onClick={() => onToggle(!enabled)}
            aria-label={`${enabled ? 'Disable' : 'Enable'} ${connector.display_name}`}
          >
            {enabled ? 'Disable' : 'Enable'}
          </Button>
        )}
        {isOperator && !removed && (
          <Button size="small" color="error" disabled={busy} onClick={onRemove} aria-label={`Remove ${connector.display_name}`}>
            Remove
          </Button>
        )}
      </Box>
    </Paper>
  )
}
