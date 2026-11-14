import { type JSX } from 'react'
import { KeyValue } from '../../ui/KeyValueList'
import OwnValueBlock from './OwnValueBlock'
import { getOrgOutcomeScanInterval, patchOrgOutcomeScanInterval } from './api'

/* AN ORG'S OWN OUTCOME SCAN INTERVAL (Settings > Tasks > Schedules): the org admin's `own` minutes, blank = the
 * operator's `global` default; a project's own interval (Project › Governance) beats it. The API bounds it
 * (`minimum`..`maximum`, 422 otherwise) and its sentence is the block's banner; the Default row shows the range. */

export const OUTCOME_SCAN_INTERVAL_LABEL = 'Outcome scan interval'

const minutes = (n: number) => `${n} min`

export default function OutcomeScanIntervalBlock({ orgId }: { orgId: string }): JSX.Element {
  return (
    <OwnValueBlock
      queryKey={['org-outcome-scan-interval', orgId]} testId="org-outcome-scan-interval" titleId="org-outcome-scan-interval-title"
      title={OUTCOME_SCAN_INTERVAL_LABEL}
      help={{
        tip: 'How often each project is scanned for reverts and fixes.',
        more: 'In minutes. Blank uses the Default below. A project\'s own interval, set on the project, overrides this one.',
        docs: 'administration/security-and-audit/settings',
      }}
      noun="outcome scan interval" field="interval" unit="minutes" format={minutes}
      load={() => getOrgOutcomeScanInterval(orgId)}
      write={value => patchOrgOutcomeScanInterval(orgId, { own: value })}
      last={s => (
        <KeyValue label="Default" valueTestId="org-outcome-scan-interval-global">
          {`${minutes(s.global)} · ${s.minimum}–${s.maximum}`}
        </KeyValue>
      )}
    />
  )
}
