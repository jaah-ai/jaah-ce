import { type JSX, type ReactNode, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../api'
import { SectionHeader } from '../../ui/SectionHeader'
import Banner from '../../ui/Banner'
import EditableRow from '../../ui/EditableRow'
import type { HelpProp } from '../../ui/explain'
import { ExplainMoreButton, HelpLabel } from '../../ui/HelpLabel'
import { wholeNumber } from '../../lib/wholeNumber'
import { KeyValue, KeyValueList } from '../../ui/KeyValueList'
import LoadingState from '../../ui/LoadingState'

/* AN ORG'S OWN NUMERIC SETTING: Effective, Your <field> edited in place (`ui/EditableRow`; a blank commit clears
 * it), then an upstream row. Shared by `CronMinPeriodBlock` and `OutcomeScanIntervalBlock`; each passes its copy
 * and API. The server bounds the value (`routes/orgs.py`, 422) and its sentence is the block's banner; only a
 * non-number is stopped here, since it cannot be sent as one. */

export default function OwnValueBlock<S extends { own: number | null; effective: number }>(p: {
  /** The read's cache key; `OverridesTab` and `SubscriptionPanel` write the cron one, so it is passed, not derived. */
  queryKey: readonly unknown[]
  testId: string
  titleId: string
  title: string
  /** The own value's explanation, a hover on its "Your <field>" label (`ui/HelpLabel`). */
  help: HelpProp
  tier?: string
  noun: string
  load: () => Promise<S>
  write: (value: number | null) => Promise<S>
  format: (n: number) => string
  field: string
  unit: string
  last: (s: S) => ReactNode
}): JSX.Element {
  const client = useQueryClient()
  const query = useQuery({ queryKey: p.queryKey, queryFn: p.load })
  const [refused, setRefused] = useState('')
  const mutation = useMutation({
    mutationFn: p.write,
    onSuccess: next => client.setQueryData(p.queryKey, next),
    onError: err => setRefused(apiDetail(err, `Failed to save the ${p.noun}.`)),
  })
  const save = (next: string) => {
    const value = next === '' ? null : wholeNumber(next)
    if (next !== '' && value === null) {
      setRefused(`Whole ${p.unit}.`)
      return
    }
    setRefused('')
    mutation.mutate(value)
  }
  const error = refused || (query.isError ? apiDetail(query.error, `Failed to load the ${p.noun}.`) : '')
  const state = query.data ?? null
  const current = state === null ? null : state.own
  const label = `Your ${p.field}`

  return (
    <Stack component="section" spacing={1} data-testid={p.testId} data-tier={p.tier} aria-labelledby={p.titleId}>
      <SectionHeader title={p.title} titleId={p.titleId} />
      {error && <Banner severity="error" testId={`${p.testId}-error`}>{error}</Banner>}
      {state === null ? (
        !error && <LoadingState rows={2} />
      ) : (
        <KeyValueList align="center">
          <KeyValue label="Effective" strong valueTestId={`${p.testId}-effective`}>{p.format(state.effective)}</KeyValue>
          <EditableRow
            label={(
              <Box component="span" sx={{ position: 'relative' }}>
                <HelpLabel label={label} help={p.help} />
                <ExplainMoreButton label={label} help={p.help} />
              </Box>
            )}
            value={(
              <Typography component="span" variant="body2" data-testid={`${p.testId}-own`}>
                {current === null ? 'None' : p.format(current)}
              </Typography>
            )}
            seed={current === null ? '' : String(current)}
            editLabel={`Edit your ${p.field}`}
            canEdit={!mutation.isPending}
            onSave={save}
            testId={`${p.testId}-own`}
            rowTestId={`${p.testId}-own-row`}
          />
          {p.last(state)}
        </KeyValueList>
      )}
    </Stack>
  )
}
