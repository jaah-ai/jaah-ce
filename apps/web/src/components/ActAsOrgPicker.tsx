import { useState, type ReactNode } from 'react'
import Button from '@mui/material/Button'
import TextField from '@mui/material/TextField'
import { apiDetail } from '../api'
import { enterActAs } from '../lib/actAs'
import { ORG_IDS_MAX, useOrgSearch } from '../lib/orgs'
import { useOrgScope } from '../lib/orgScope'
import type { Org } from '../types'
import Banner from '../ui/Banner'
import InterstitialPanel from '../ui/InterstitialPanel'
import LoadingState from '../ui/LoadingState'
import { tapTargetSx } from '../ui/listStyles'

/** The operator's way into a feature that runs inside one org (Compliance, a plan-gated page): the operator plane
 *  has none, and the org filter only narrows reads, so the way in is acting as an org (`lib/actAs.ts`). Lists the
 *  live tenant orgs `eligible` admits, picking the filter's own org when it is one of them, else a live (approved)
 *  one before a Pending one, and Act as. One roster page (`ORG_IDS_MAX`) per search: the box asks the server
 *  (`q`), so an org past the first page is one search away. The box shows only while there is such an org (the
 *  page is partial) or a search is under way; a roster that fits one page is the whole list. */
export default function ActAsOrgPicker({ testId, actAsTestId, title, body, eligible = () => true, none, noneHint }: {
  testId: string
  actAsTestId: string
  title: string
  /** The sentence under the title, given what the operator is looking at: “All orgs” or the org filter. */
  body: (view: string) => string
  /** Narrows the live tenant orgs (enabled, not the operator plane) to those that can open the feature. */
  eligible?: (org: Org) => boolean
  /** What every listed org is, for the empty answer: "No org has Compliance on." */
  none: string
  noneHint?: ReactNode
}) {
  const scope = useOrgScope()
  const search = useOrgSearch({ per: ORG_IDS_MAX })
  const roster = search.query
  // Live orgs before Pending ones (stable within each): the list's order is the default pick's.
  const orgs = (roster.data?.items ?? [])
    .filter(org => !org.is_operator_plane && org.enabled && eligible(org))
    .sort((a, b) => Number(a.lifecycle === 'pending') - Number(b.lifecycle === 'pending'))
  const [picked, setPicked] = useState('')
  const chosen = orgs.find(org => org.id === picked)
    ?? orgs.find(org => scope?.some(s => s.id === org.id))
    ?? orgs[0]
  const view = scope ? 'The org filter' : '“All orgs”'
  // Past one page an eligible org may sit unread: say "none here", never "none at all".
  const partial = (roster.data?.total ?? 0) > (roster.data?.items.length ?? 0)
  const searchable = partial || search.typed !== '' || search.searched !== ''
  const matching = search.searched ? ` matching “${search.searched}”` : ''
  const noneText = partial ? `None of the first ${ORG_IDS_MAX} orgs${matching} ${none}.` : `No org${matching} ${none}.`
  return (
    <InterstitialPanel testId={testId} title={title} body={body(view)}>
      {searchable && (
        <TextField
          type="search"
          label="Search orgs"
          value={search.typed}
          onChange={e => search.type(e.target.value)}
          slotProps={{ inputLabel: { shrink: true }, htmlInput: { 'aria-label': 'Search orgs' } }}
          sx={theme => ({ m: 0, mt: 1, '& .MuiInputBase-root': tapTargetSx(theme) })}
        />
      )}
      {roster.isPending ? (
        <LoadingState rows={1} />
      ) : roster.error ? (
        <Banner severity="error">{apiDetail(roster.error, 'The orgs could not be read.')}</Banner>
      ) : !chosen ? (
        <Banner severity="info">{noneText}{noneHint && <> {noneHint}</>}</Banner>
      ) : (
        <>
          <TextField
            select
            label="Org"
            value={chosen.id}
            onChange={e => setPicked(e.target.value)}
            slotProps={{ select: { native: true }, inputLabel: { shrink: true } }}
            // The outlined label rides ~9px above the field: clear of the sentence above.
            sx={theme => ({ m: 0, mt: 1, '& .MuiInputBase-root': tapTargetSx(theme) })}
          >
            {/* The picked org's state rides its own option, once: no second chip beside the field. */}
            {orgs.map(org => (
              <option key={org.id} value={org.id}>{org.lifecycle === 'pending' ? `${org.name} · Pending` : org.name}</option>
            ))}
          </TextField>
          <Button
            variant="contained"
            color="warning"
            data-testid={actAsTestId}
            title={`Acts as ${chosen.name}'s Owner in this tab`}
            onClick={() => enterActAs({ id: chosen.id, slug: chosen.slug, name: chosen.name })}
            sx={[tapTargetSx, { alignSelf: 'flex-start', textTransform: 'none' }]}
          >
            Act as {chosen.name}
          </Button>
        </>
      )}
    </InterstitialPanel>
  )
}
