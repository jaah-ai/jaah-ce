import { type JSX } from 'react'
import { periodWords } from '../../lib/relativeTime'
import { KeyValue } from '../../ui/KeyValueList'
import OwnValueBlock from './OwnValueBlock'
import { getOrgCronMinPeriod, patchOrgCronMinPeriod } from './api'

/* AN ORG'S OWN CRON MINIMUM PERIOD, the org admin's raise-only `own` (Settings > Organizations; never under what
 * the operator set — `routes/orgs.py`, `app/cron_min_period.py`). The operator's per-org `override` is a row of
 * the customer's Overrides tab (`OverridesTab.tsx`), same read, same PATCH. The API is the boundary: a tenant sending
 * `override` is refused there. Input in seconds, shown in words; a value under the upstream is refused by the API
 * with its sentence. */

export default function CronMinPeriodBlock({ orgId, title = 'Cron minimum period' }: {
  orgId: string
  title?: string
}): JSX.Element {
  return (
    <OwnValueBlock
      queryKey={['org-cron-min-period', orgId]} testId="org-cron-min-period" titleId="org-cron-min-period-own-title"
      title={title} tier="own"
      help={{
        tip: 'Shortest gap allowed between two firings of one schedule.',
        more: 'It can only raise Jaah\'s minimum, never lower it. A schedule set faster is refused, '
          + 'and an older one that fires too soon is skipped.',
        docs: 'administration/security-and-audit/settings',
      }}
      noun="cron minimum" field="minimum" unit="seconds" format={periodWords}
      load={() => getOrgCronMinPeriod(orgId)}
      write={value => patchOrgCronMinPeriod(orgId, { own: value })}
      last={s => (
        <KeyValue label="Set by Jaah"
          valueTestId="org-cron-min-period-upstream">
          {periodWords(s.upstream)}
        </KeyValue>
      )}
    />
  )
}
