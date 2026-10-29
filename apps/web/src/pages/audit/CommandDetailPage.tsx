import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { useQuery } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import { apiDetail, get } from '../../api'
import Breadcrumb from '../../components/Breadcrumb'
import { absoluteTime, NO_VALUE } from '../../lib/relativeTime'
import { AUDIT_NODE_TAB_PATHS } from '../../routeGroups'
import type { Command } from '../../types'
import Banner from '../../ui/Banner'
import { CodeBlock } from '../../ui/CodeBlock'
import { Mono } from '../../ui/DetailFields'
import DetailPage, { DETAIL_SECTIONS_SX, DetailField, DetailSection } from '../../ui/detail/DetailPage'
import LoadingState from '../../ui/LoadingState'
import StatusChip from '../../ui/StatusChip'
import { toneFor } from '../../ui/statusTone'

/* ONE NODE COMMAND, IN FULL (`/audit/nodes/commands/:commandId`) — the editor beside Audit > Nodes' command log
 * (`lib/listDetail.ts`), a full page below the dock width. The row keeps what is scanned down the log; this keeps
 * the args and result JSON the row only summarises. Read by id (`GET /api/command-log/{id}`), under the log's own
 * scoping: a command the reader could not page to is a 404. */

/** The command's address, as the log opens it. */
export function commandPath(commandId: number): string {
  return `/audit/nodes/commands/${commandId}`
}

/** What a command bound to no node is called: the log's Node filter option, its rows, and this page. */
export const UNBOUND_NODE_LABEL = 'No node'

/** `GET /api/command-log/{id}` (`routes/portfolio.py::CommandDetailOut`): the log's row plus who dispatched it. */
type CommandDetail = Command & { actor: string | null }

function Json({ value, testId }: { value: unknown; testId: string }): JSX.Element {
  return <CodeBlock testId={testId} maxHeight={480}>{JSON.stringify(value, null, 2)}</CodeBlock>
}

export default function CommandDetailPage(): JSX.Element {
  const params = useParams()
  const commandId = Number(params.commandId)
  const validId = Number.isInteger(commandId) && commandId > 0
  const query = useQuery({
    queryKey: ['command-log', commandId],
    queryFn: () => get<CommandDetail>(`/api/command-log/${commandId}`),
    enabled: validId,
    retry: false,
  })
  const cmd = query.data

  const breadcrumb = (
    <Breadcrumb
      items={[
        { label: 'Audit', to: '/audit' },
        { label: 'Nodes', to: AUDIT_NODE_TAB_PATHS.commands },
        { label: `Command ${params.commandId ?? ''}` },
      ]}
    />
  )
  return (
    <DetailPage
      testId="command-detail"
      sx={{ minWidth: 0 }}
      breadcrumb={breadcrumb}
      title={cmd && <Box component="span" sx={{ overflowWrap: 'anywhere' }}>{cmd.command_type}</Box>}
    >
      {!validId && <Banner severity="error">This command link is not valid.</Banner>}
      {validId && query.isPending && <LoadingState rows={4} label="Loading command" />}
      {query.isError && <Banner severity="error">{apiDetail(query.error, 'Could not load this command')}</Banner>}
      {cmd && (
        <Box sx={DETAIL_SECTIONS_SX}>
          <DetailSection fields testId="command-detail-fields">
            <DetailField label="Status">
              <Box component="span" data-testid="command-detail-status">
                <StatusChip label={cmd.status} tone={toneFor(`badge-${cmd.status}`)} />
              </Box>
            </DetailField>
            <DetailField label="Node">
              {cmd.node_id ? <Mono>{cmd.node_id}</Mono> : <Typography variant="body2">{UNBOUND_NODE_LABEL}</Typography>}
            </DetailField>
            <DetailField label="By">
              <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>{cmd.actor ?? NO_VALUE}</Typography>
            </DetailField>
            <DetailField label="Submitted">
              <Typography variant="body2">{absoluteTime(cmd.created_at)}</Typography>
            </DetailField>
            <DetailField label="Completed">
              <Typography variant="body2">{absoluteTime(cmd.completed_at)}</Typography>
            </DetailField>
          </DetailSection>
          <DetailSection title="Args" divided>
            <Json value={cmd.args} testId="command-detail-args" />
          </DetailSection>
          <DetailSection title="Result" divided>
            <Json value={cmd.result} testId="command-detail-result" />
          </DetailSection>
        </Box>
      )}
    </DetailPage>
  )
}
