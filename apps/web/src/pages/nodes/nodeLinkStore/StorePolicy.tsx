import type { JSX } from 'react'
import { type QueryKey, useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../../api'
import Banner from '../../../ui/Banner'
import { CodeBlock } from '../../../ui/CodeBlock'

/* A store's file-system policy, read-only, and what its last Repair said — spelled once for the Storage tab
 * (`NodeLinkStorageTab.tsx`) and Settings › NodeLink storage (`NodeLinkLocationsSection.tsx`). The panel reads
 * only while mounted; a caller re-reads it by invalidating `queryKey` after Refresh or Repair. */

/** AWS's policy as one indented document; verbatim when it is not JSON. */
function prettyPolicy(policy: string): string {
  try {
    return JSON.stringify(JSON.parse(policy), null, 2)
  } catch {
    return policy
  }
}

export function PolicyToggle({ open, onToggle, testId }: { open: boolean; onToggle: () => void; testId: string }): JSX.Element {
  return (
    <Button variant="text" size="small" onClick={onToggle} aria-expanded={open} data-testid={testId}>
      {open ? 'Hide policy' : 'Show policy'}
    </Button>
  )
}

/** "Policy rewritten · N access points": a Repair's answer. */
export function RepairedNote({ count, testId }: { count: number; testId: string }): JSX.Element {
  return (
    <Typography variant="body2" role="status" data-testid={testId} sx={{ color: 'success.main' }}>
      Policy rewritten · {count} access point{count === 1 ? '' : 's'}
    </Typography>
  )
}

export function PolicyPanel({ queryKey, read, testId }: {
  queryKey: QueryKey
  read: () => Promise<{ policy: string | null }>
  testId: string
}): JSX.Element {
  const query = useQuery({ queryKey, queryFn: read, retry: false })
  return (
    <Box data-testid={testId} sx={{ display: 'flex', flexDirection: 'column', gap: 0.75, minWidth: 0 }}>
      <Typography variant="subtitle2">File-system policy</Typography>
      {query.isPending && <Typography variant="body2" sx={{ color: 'text.secondary' }}>Reading from AWS…</Typography>}
      {query.isError && <Banner severity="error">{apiDetail(query.error, 'Could not read the policy.')}</Banner>}
      {query.data && (query.data.policy === null
        ? <Typography variant="body2" sx={{ color: 'text.secondary' }}>None: the file system has no policy.</Typography>
        : <CodeBlock bordered maxHeight="28rem" testId={`${testId}-json`}>{prettyPolicy(query.data.policy)}</CodeBlock>)}
    </Box>
  )
}
