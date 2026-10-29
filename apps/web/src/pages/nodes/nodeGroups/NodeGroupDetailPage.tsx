import { type JSX, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../../api'
import Breadcrumb from '../../../components/Breadcrumb'
import { useConfirm } from '../../../components/ConfirmDialog'
import { useChangeOrgAction } from '../../../components/ChangeOrgDialog'
import { TabPanel, Tabs } from '../../../components/Tabs'
import { useEventStream } from '../../../hooks/useEventStream'
import { useLiveRefetch } from '../../../hooks/useLiveRefetch'
import { useIsOrgAdmin } from '../../../hooks/useIsOrgAdmin'
import { useUrlTab } from '../../../hooks/useUrlTab'
import { absoluteTime, fmtTimestamp } from '../../../lib/relativeTime'
import Banner from '../../../ui/Banner'
import { DETAIL_ACTIONS_SX, DETAIL_SECTIONS_SX, DetailField, DetailPage, DetailSection } from '../../../ui/detail/DetailPage'
import { DisabledReason } from '../../../ui/DisabledReason'
import { monoSx, tapTargetSx } from '../../../ui/listStyles'
import LoadingState from '../../../ui/LoadingState'
import { MetaActor, MetaField, MetaRow } from '../../../ui/MetaRow'
import { OrgIndicator, useOrgVisible } from '../../../ui/OrgIndicator'
import StatusChip from '../../../ui/StatusChip'
import { StatGrid } from '../../../ui/StatGrid'
import { listCloudConnectionOptions } from '../../settings/cloudConnections/api'
import { getNodeGroup, listAssignableProjects } from './api'
import { deleteGroup } from './deleteGroup'
import {
  capacityLineLabel,
  convergence,
  groupAccount,
  groupPath,
  NO_EDIT_REASON,
} from './groupFacts'
import NodeGroupDialog from './NodeGroupDialog'

/* /nodes/groups/:groupId — ONE DYNAMIC NODE GROUP, the same page shape as `/nodes/:nodeId` (the shared
 * `ui/detail/DetailPage`, opened in the list/detail split beside /nodes — `lib/listDetail.ts`). Read-only
 * facts on the page; Edit opens the group's form (`NodeGroupDialog`, the same one New group uses), Delete
 * takes the list's guard (`groupFacts.ts::deleteBlockedReason`). Fetch posture is the list's: refetch on
 * SSE `entity: 'node_group'`. */

const GROUPS_PATH = '/nodes?tab=groups'

/* Status first (state, capacity, record meta), then the projects the group serves (`?tab=projects`). */
const TABS = [{ id: 'status', label: 'Status' }, { id: 'projects', label: 'Projects' }] as const

const LINE_SX = { display: 'flex', justifyContent: 'space-between', gap: 2, py: 0.5 } as const

export default function NodeGroupDetailPage(): JSX.Element {
  const { groupId = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const canEdit = useIsOrgAdmin()
  const showOrg = useOrgVisible()
  const stream = useEventStream()
  const [confirm, confirmDialog] = useConfirm()
  const changeOrg = useChangeOrgAction('node_group')
  const [editing, setEditing] = useState(false)
  const [actionError, setActionError] = useState('')
  const [deleting, setDeleting] = useState(false)
  const [tab, selectTab] = useUrlTab(TABS, 'status')

  const groupQuery = useQuery({
    queryKey: ['node-group', groupId],
    queryFn: () => getNodeGroup(groupId),
    enabled: groupId !== '',
  })

  useLiveRefetch(stream, () => void groupQuery.refetch(), null, { entity: 'node_group' })

  /* The form's reference lists, fetched only once Edit opens (the list's posture). */
  const optionsQuery = useQuery({ queryKey: ['node-group-options'], queryFn: listCloudConnectionOptions, enabled: editing })
  const projectsQuery = useQuery({
    queryKey: ['node-group-assignable-projects'],
    queryFn: listAssignableProjects,
    enabled: editing,
  })

  const row = groupQuery.data
  const name = row?.name ?? groupId
  const breadcrumb = (
    <Breadcrumb items={[{ label: 'Nodes', to: '/nodes' }, { label: 'Pools', to: GROUPS_PATH }, { label: name }]} />
  )

  if (groupQuery.isLoading) return <LoadingState rows={4} />
  if (groupQuery.isError || !row) {
    return (
      <Box>
        {breadcrumb}
        <Banner severity="error">{apiDetail(groupQuery.error, 'Failed to load the node group.')}</Banner>
      </Box>
    )
  }

  const state = convergence(row)
  const account = groupAccount(row)
  const optionsError = [
    optionsQuery.isError ? apiDetail(optionsQuery.error, 'Failed to load the AWS accounts and instance types.') : '',
    projectsQuery.isError ? apiDetail(projectsQuery.error, 'Failed to load the projects a group can serve.') : '',
  ].filter(Boolean).join(' ')

  const remove = () => deleteGroup(row, {
    confirm,
    setError: setActionError,
    busy: async (work) => {
      setDeleting(true)
      try { await work() } finally { setDeleting(false) }
    },
    onDeleted: async () => {
      await queryClient.invalidateQueries({ queryKey: ['node-groups'] })
      navigate(GROUPS_PATH)
    },
  })

  return (
    <DetailPage
      testId="node-group-detail"
      title={row.name}
      breadcrumb={breadcrumb}
      subtitle={row.description || undefined}
      actions={<StatusChip label={state.label} tone={state.tone} />}
      measure="read"
    >
      {actionError && <Banner severity="error">{actionError}</Banner>}
      <Box sx={DETAIL_ACTIONS_SX}>
        <DisabledReason reason={canEdit ? undefined : NO_EDIT_REASON}>
          <Button
            variant="outlined"
            data-testid="node-group-edit"
            disabled={!canEdit}
            title={canEdit ? undefined : NO_EDIT_REASON}
            onClick={() => { setActionError(''); setEditing(true) }}
            sx={tapTargetSx}
          >
            ✎ Edit
          </Button>
        </DisabledReason>
        {changeOrg.button({ id: row.id, name: row.name, org: row.org, href: groupPath(row.id) })}
        <Box sx={{ flex: '1 1 auto' }} />
        <DisabledReason reason={canEdit ? undefined : NO_EDIT_REASON}>
          <Button
            variant="outlined"
            color="error"
            data-testid="node-group-delete"
            disabled={!canEdit || deleting}
            title={canEdit ? undefined : NO_EDIT_REASON}
            onClick={() => { void remove() }}
            sx={tapTargetSx}
          >
            ✕ Delete
          </Button>
        </DisabledReason>
      </Box>
      <Tabs tabs={TABS} tab={tab} onSelect={selectTab} label="Node group sections" idPrefix="node-group" />

      <TabPanel id="status" current={tab} idPrefix="node-group">
        <Box sx={DETAIL_SECTIONS_SX}>
          <DetailSection title="Status & capacity">
            {/* THE REASON IN FULL: AWS puts the actionable part at the END. Muted on a switched-off group. */}
            {row.last_error !== '' && (
              <Banner severity={row.enabled ? 'error' : 'info'} testId="node-group-detail-error">
                {row.enabled ? row.last_error : `While it was running: ${row.last_error}`}
              </Banner>
            )}
            <StatGrid minTile={140} columns={3} testId="node-group-stats-grid">
              <DetailField label="Status"><StatusChip label={state.label} tone={state.tone} /></DetailField>
              <DetailField label="Instances">
                <span data-testid="node-group-detail-count">{row.node_count} / {row.desired_total}</span>
              </DetailField>
              <DetailField label="Last reconciled">
                <span title={row.last_reconciled_at ? absoluteTime(row.last_reconciled_at) : undefined}>
                  {row.last_reconciled_at ? fmtTimestamp(row.last_reconciled_at) : 'Never'}
                </span>
              </DetailField>
              <DetailField label="AWS account">{account}</DetailField>
              <DetailField label="Launching">{row.enabled ? 'On' : 'Off'}</DetailField>
              {showOrg && (
                <DetailField label="Org">
                  <OrgIndicator org={row.org} unresolvedId={row.org ? undefined : row.org_id} />
                </DetailField>
              )}
            </StatGrid>
          </DetailSection>

          <DetailSection title="Capacity">
            {row.capacity.length === 0 ? (
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>No capacity lines.</Typography>
            ) : (
              <Box data-testid="node-group-capacity">
                {row.capacity.map(line => (
                  <Box key={line.id} sx={LINE_SX}>
                    <Typography variant="body2" sx={monoSx}>{capacityLineLabel(line)}</Typography>
                    <Typography variant="body2" sx={[monoSx, { color: 'text.secondary' }]}>
                      {line.node_count} / {line.desired}
                    </Typography>
                  </Box>
                ))}
              </Box>
            )}
          </DetailSection>

          <MetaRow>
            <MetaField label="Created" iso={row.created_at} />
            <MetaActor label="Created by" actor={row.created_by} />
            <MetaField label="Updated" iso={row.updated_at} />
            <MetaActor label="Updated by" actor={row.updated_by} />
          </MetaRow>
        </Box>
      </TabPanel>

      <TabPanel id="projects" current={tab} idPrefix="node-group">
        <Box sx={DETAIL_SECTIONS_SX}>
          <DetailSection title="Projects">
            {row.projects.length === 0 ? (
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>No projects assigned.</Typography>
            ) : (
              <Box data-testid="node-group-projects">
                {row.projects.map(p => (
                  <Box key={p.id} sx={LINE_SX}>
                    <Typography variant="body2">{p.project_name}</Typography>
                  </Box>
                ))}
              </Box>
            )}
          </DetailSection>
        </Box>
      </TabPanel>

      {editing && (
        <NodeGroupDialog
          row={row}
          connections={optionsQuery.data?.connections ?? []}
          instanceTypes={optionsQuery.data?.instance_types ?? []}
          projects={Array.isArray(projectsQuery.data) ? projectsQuery.data : []}
          optionsError={optionsError}
          onClose={() => setEditing(false)}
          onSaved={async () => {
            await Promise.all([
              groupQuery.refetch(),
              queryClient.invalidateQueries({ queryKey: ['node-groups'] }),
            ])
          }}
        />
      )}
      {confirmDialog}
      {changeOrg.dialog}
    </DetailPage>
  )
}
