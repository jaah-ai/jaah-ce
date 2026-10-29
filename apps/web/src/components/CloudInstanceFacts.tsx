import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import { useAuth } from '../auth'
import type { CloudProvider } from '../types'
import { CopyButton } from '../ui/CopyButton'
import { DetailField } from '../ui/detail/DetailPage'
import { Mono } from '../ui/DetailFields'
import { useRegionLabel } from '../lib/dataRegions'
import { tapTargetSx } from '../ui/listStyles'

/* THE CLOUD INSTANCE BEHIND A NODE OR HOST — one home for the node page's Machine section and the
 * host page's Status: its instance id (copyable), region, account and the image it was launched from (so a
 * stale host shows here, not only in the cloud's console), with the provider's console doors an operator reaches
 * for (`cloudConsoleLinks`). OPERATOR ONLY: a tenant reads no cloud internals beyond the instance type. Nothing when
 * the instance is unknown. */

type ConsoleLink = { key: string; label: string; href: string }

/** The provider's console pages for an instance: EC2 and Session Manager on AWS, the Compute instance on
 *  GCP (`scope` = project, `zone` needed), the VM on Azure (`scope` = `subscription/resource-group`). None when a
 *  fact the URL needs is missing: never another cloud's console. An absent provider is AWS (`clouds.provider_key_of`);
 *  null is a cloud the server could not name, which gets none. */
export function cloudConsoleLinks({ provider, instanceId, region, zone, scope }: {
  provider?: CloudProvider | null
  instanceId: string
  region?: string | null
  zone?: string | null
  scope?: string | null
}): ConsoleLink[] {
  const id = encodeURIComponent(instanceId)
  const enc = (v: string) => encodeURIComponent(v)
  switch (provider === undefined ? 'aws' : provider) {
    case 'aws': {
      if (!region) return []
      const at = enc(region)
      return [
        { key: 'ec2', label: 'EC2', href: `https://${at}.console.aws.amazon.com/ec2/home?region=${at}#InstanceDetails:instanceId=${id}` },
        { key: 'ssm', label: 'Connect (SSM)', href: `https://${at}.console.aws.amazon.com/systems-manager/session-manager/${id}?region=${at}` },
      ]
    }
    case 'gcp':
      return zone && scope
        ? [{ key: 'gcp', label: 'Compute Engine', href: `https://console.cloud.google.com/compute/instancesDetail/zones/${enc(zone)}/instances/${id}?project=${enc(scope)}` }]
        : []
    case 'azure': {
      const [subscription, group] = (scope ?? '').split('/')
      return subscription && group
        ? [{ key: 'azure', label: 'Azure portal', href: `https://portal.azure.com/#resource/subscriptions/${enc(subscription)}/resourceGroups/${enc(group)}/providers/Microsoft.Compute/virtualMachines/${id}/overview` }]
        : []
    }
    default:
      return []
  }
}

export default function CloudInstanceFacts({ provider, instanceId, region, zone, scope, accountId, imageId, showRegion = true }: {
  /** The instance's cloud (`nodes.infra.cloud_provider`); absent is AWS, null unknown. Picks the console links. */
  provider?: CloudProvider | null
  instanceId: string | null | undefined
  /** Also the console links' region. */
  region: string | null | undefined
  /** Where the instance runs beyond its region (`clouds.InstanceRef`): the GCP and Azure links need them. */
  zone?: string | null
  scope?: string | null
  /** False where the page already names the region (the node page's Status, every viewer's). */
  showRegion?: boolean
  accountId?: string | null
  /** The launch's image; absent for a machine launched before it was recorded. */
  imageId?: string | null
}): JSX.Element | null {
  const operator = useAuth().admin?.is_operator === true
  const regionLabel = useRegionLabel(operator && instanceId && showRegion ? region : null)
  if (!operator || !instanceId) return null
  const links = cloudConsoleLinks({ provider, instanceId, region, zone, scope })
  // Each cloud's own word for the machine: Azure calls it a VM.
  const noun = provider === 'azure' ? 'VM' : 'Instance'
  return (
    <Box data-testid="cloud-instance" sx={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0 }}>
      {/* A wrapping row, not a grid: the id is the widest fact, and on a phone each fact takes its own line. */}
      <Box component="dl" sx={{ m: 0, display: 'flex', flexWrap: 'wrap', columnGap: 3, rowGap: 1, minWidth: 0 }}>
        <DetailField label={noun}>
          <Box component="span" sx={{ display: 'inline-flex', flexWrap: 'wrap', alignItems: 'center', gap: 0.5, minWidth: 0 }}>
            <Mono testId="cloud-instance-id">{instanceId}</Mono>
            <CopyButton variant="icon" label={`Copy ${noun === 'VM' ? 'VM' : 'instance'} id`} text={() => instanceId} testId="cloud-instance-copy" />
          </Box>
        </DetailField>
        {/* The readable name, then the code a cloud console search wants. */}
        {showRegion && region && (
          <DetailField label="Region">
            <Box component="span" sx={{ display: 'inline-flex', flexWrap: 'wrap', alignItems: 'baseline', columnGap: 0.75, minWidth: 0 }}>
              {regionLabel !== region && <span data-testid="cloud-instance-region-label">{regionLabel}</span>}
              <Mono testId="cloud-instance-region">{region}</Mono>
            </Box>
          </DetailField>
        )}
        {accountId && <DetailField label="Account"><Mono testId="cloud-instance-account">{accountId}</Mono></DetailField>}
        <DetailField label="Image">
          {imageId
            ? <Mono testId="cloud-instance-image">{imageId}</Mono>
            : <Box component="span" data-testid="cloud-instance-image" sx={{ color: 'text.secondary' }}>Not recorded</Box>}
        </DetailField>
      </Box>
      {links.length > 0 && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 2, fontSize: '0.875rem' }}>
          {links.map(l => (
            <Link key={l.key} href={l.href} target="_blank" rel="noopener noreferrer" underline="hover" data-testid={`cloud-instance-${l.key}`}
              sx={[tapTargetSx, { display: 'inline-flex', alignItems: 'center' }]}
            >
              {l.label} ↗
            </Link>
          ))}
        </Box>
      )}
    </Box>
  )
}
