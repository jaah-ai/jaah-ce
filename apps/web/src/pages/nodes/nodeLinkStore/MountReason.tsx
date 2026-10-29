import type { JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { nodeLinkUrl } from './api'

/* Why a NodeLink is not mounted on a node, IN FULL — the helper's text is the diagnosis, never cut — spelled once
 * for the NodeLink's Nodes tab (`NodeLinkNodesTab.tsx`) and a node's NodeLinks tab (`NodeNodeLinksPanel.tsx`). A
 * DNS failure on EFS storage with no mount target IP adds where the fix is: Storage › Mount target IP › Change, for a
 * reader who can change it; anyone else is told to ask an admin. */

/** The node's words when the file system's DNS name does not resolve: `mount.efs`'s ("Failed to resolve …
 *  ensure that the VPC has an EFS mount target"), the resolver's, and the server's rewrite of them on storage
 *  with no mount target IP (`nodelinks.OUT_OF_VPC`). */
const DNS_FAILURES = [
  'failed to resolve',
  'ensure that the vpc has an efs mount target',
  'getaddrinfo',
  'name or service not known',
  'temporary failure in name resolution',
]
/** `nodelinks.OUT_OF_VPC`: the server says it only of storage with no mount target IP. */
const OUT_OF_VPC = 'outside the file system\'s vpc'

/** The reason is a DNS failure on storage with no mount target IP. `mountTargetIp`: the storage's; `undefined`
 *  when this reader cannot tell — then only the server's own out-of-VPC verdict counts. */
export function needsMountTargetIp(reason: string | null | undefined, mountTargetIp: string | null | undefined): boolean {
  const text = (reason ?? '').toLowerCase()
  if (text.includes(OUT_OF_VPC)) return !mountTargetIp
  if (mountTargetIp !== null) return false
  return DNS_FAILURES.some(marker => text.includes(marker))
}

export default function MountReason({ reason, nodelinkId, mountTargetIp, canChange, efs = true, testId }: {
  reason: string
  nodelinkId: string
  /** See `needsMountTargetIp`. */
  mountTargetIp: string | null | undefined
  /** The storage is EFS, the only one with a mount target IP; false says no fix. Default: the server's verdict
   *  (`nodelinks.OUT_OF_VPC`, said of EFS only) decides. */
  efs?: boolean
  /** The Storage tab's Change is this reader's (`NodeLinkStorageTab.tsx`): without it, no link to a dead end. */
  canChange: boolean
  testId?: string
}): JSX.Element {
  return (
    // A row of its own: a flex row beside the name and chip would squeeze a long reason to a sliver.
    <Box data-testid={testId} sx={{ flexBasis: '100%', minWidth: 0 }}>
      <Typography variant="body2" sx={{ color: 'text.secondary', overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>
        {reason}
      </Typography>
      {efs && needsMountTargetIp(reason, mountTargetIp) && (
        <Typography variant="body2" data-testid={testId && `${testId}-hint`}>
          {canChange ? (
            <>
              Set{' '}
              <Link component={RouterLink} to={nodeLinkUrl(nodelinkId, 'storage')} underline="hover">
                Storage › Mount target IP › Change
              </Link>
            </>
          ) : 'Ask an admin to set its mount target IP.'}
        </Typography>
      )}
    </Box>
  )
}
