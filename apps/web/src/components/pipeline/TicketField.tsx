import { type JSX, useEffect, useState } from 'react'
import Box from '@mui/material/Box'
import TextField from '@mui/material/TextField'
import { useQuery } from '@tanstack/react-query'
import { apiDetail } from '../../api'
import { lookupTicket, type TicketSystem } from '../../lib/pipelineGates'
import StatusChip from '../../ui/StatusChip'
import { PHONE } from '../../ui/listStyles'
import { grid } from '../../theme'

/* THE RELEASE'S CHANGE TICKET (PRD D8 "ask when pressed"): the step into `stageId` has a ticket gate that asks
 * whoever presses it for a ticket. The key typed is looked up (`GET /pipeline/ticket`) and only a ticket the system
 * does not know is never handed back (`onTicket`), so Release stays off for it; the gate checks its state when the
 * release runs. */

const LOOKUP_DELAY_MS = 400

export default function TicketField({ projectId, stageId, system, jiraProject, disabled, onTicket }: {
  projectId: number
  stageId: string
  system: TicketSystem
  /** The gate's Jira project key, so the example reads like the tickets it takes (OAM-123). */
  jiraProject?: string | null
  disabled: boolean
  onTicket: (key: string | null) => void
}): JSX.Element {
  const [typed, setTyped] = useState('')
  const [asked, setAsked] = useState('')
  useEffect(() => {
    const key = typed.trim()
    const timer = window.setTimeout(() => setAsked(key), LOOKUP_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [typed])
  const lookup = useQuery({
    queryKey: ['pipeline-ticket', projectId, stageId, asked],
    queryFn: () => lookupTicket(projectId, stageId, asked),
    enabled: asked.length >= 2,
    staleTime: 30_000,
    retry: false,
  })
  const settled = asked === typed.trim() && asked.length >= 2 && !lookup.isFetching
  const found = settled && lookup.data?.found ? lookup.data : null
  /* A lookup that can't run (this person can't use the gate's login) does not hold Release: the gate checks the
     ticket when the release runs. Only a ticket the system says it doesn't know does. */
  const unchecked = settled && lookup.isError ? asked : null
  const handed = found ? found.key : unchecked
  useEffect(() => { onTicket(handed) }, [handed, onTicket])

  let mark: JSX.Element | null = null
  if (settled && lookup.isError) {
    mark = <StatusChip label="Not checked" tone="warning" title={apiDetail(lookup.error, 'Could not check the ticket')} testId="release-ticket-state" />
  } else if (found) {
    mark = <StatusChip label={found.status || 'Found'} tone="success" title={`${found.key}${found.status ? ` is ${found.status}` : ''}`} testId="release-ticket-state" />
  } else if (settled && lookup.data && !lookup.data.found) {
    mark = <StatusChip label={`No ticket ${asked}`} tone="error" wrap testId="release-ticket-state" />
  }
  const label = system === 'servicenow' ? 'Change ticket' : 'Ticket'
  return (
    <Box sx={{ display: 'inline-flex', alignItems: 'center', gap: 1, flexWrap: 'wrap', [PHONE]: { width: '100%' } }}>
      <TextField
        size="small"
        value={typed}
        onChange={e => setTyped(e.target.value)}
        disabled={disabled}
        placeholder={system === 'servicenow' ? 'CHG0031042' : `${jiraProject || 'OPS'}-123`}
        sx={{ width: grid(22), [PHONE]: { flex: 1, width: 'auto' } }}
        slotProps={{ htmlInput: { 'data-testid': 'release-ticket', 'aria-label': label, maxLength: 64, spellCheck: false } }}
      />
      {mark}
    </Box>
  )
}
