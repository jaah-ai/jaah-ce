import type { JSX } from 'react'
import type { QueryKey } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import { useApiRead } from '../../hooks/useApiRead'
import { tapTargetSx } from '../listStyles'
import LoadFailedState from '../LoadFailedState'
import NewButton from '../NewButton'
import PageHeader from '../PageHeader'
import ListStates from './ListStates'
import type { FeedColumns, FeedRowSpec } from './types'

/* A whole, unfiltered list of records with its New button: above the rows (＋ on a phone), or the empty state's one
   action (its words at every width: alone under "No … yet", a ＋ says nothing). The body by read state: loading,
   failed, none yet, or the rows. `page` puts the page's header over it (the same feed is a Settings tab's body
   without one). `id` heads every test id and keys the section; `label` names the list. */
export default function RecordFeed<R>({ id, label, queryKey, list, rowOf, columns, emptyTitle, newLabel, newTo, page }: {
  id: string; label: string; queryKey: QueryKey; list: () => Promise<R[]>; rowOf: (row: R) => FeedRowSpec
  columns: FeedColumns; emptyTitle: string; newLabel: string; newTo: string; page?: boolean
}): JSX.Element {
  const { data, loading, error, reload } = useApiRead(queryKey, list, `Could not load ${label.toLowerCase()}`)
  const rows = (data ?? []).map(rowOf)
  const newButton = (worded: boolean): JSX.Element =>
    <NewButton label={newLabel} to={newTo} testId={`${id}-new`} worded={worded} sx={tapTargetSx} />
  const feed = (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      {rows.length > 0 && <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>{newButton(false)}</Box>}
      <ListStates
        loading={data === null ? !error : loading && rows.length === 0}
        skeleton={{ rows: 3 }}
        failed={data === null && <LoadFailedState title={`${label} unavailable`} error={error} onRetry={() => void reload()} />}
        empty={rows.length === 0} noMatch={false}
        emptyTitle={emptyTitle}
        emptyAction={newButton(true)}
        feed={{ ariaLabel: label, testId: `${id}-list`, sections: [{ key: id, rail: false, rows }], columns }}
      />
    </Box>
  )
  return page ? <Box data-testid={`${id}-page`} sx={{ minWidth: 0 }}><PageHeader title={label} />{feed}</Box> : feed
}
