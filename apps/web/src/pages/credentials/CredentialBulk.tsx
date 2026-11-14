import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { operatorPlaneRefusal } from '../../components/ChangeOrgDialog'
import { type ConfirmOptions, danger } from '../../components/ConfirmDialog'
import type { CredentialView } from '../../lib/credentialFilterStorage'
import { groupedCount, groupedCountOf } from '../../lib/humanize'
import { currentOrgScope } from '../../lib/orgScope'
import { useRowSelection } from '../../hooks/useRowSelection'
import type { Org } from '../../types'
import BulkActionBar, { type BulkAction } from '../../ui/BulkActionBar'
import FormTextField from '../../ui/FormTextField'
import FormField from '../../ui/FormField'
import { OrgSelect } from '../../ui/OrgSelect'
import type { FeedColumn, FeedSlot } from '../../ui/feed/types'
import { SelectAllBox, SelectRowBox, selectSlot } from '../../ui/RowSelect'
import Toast from '../../ui/Toast'
import {
  BULK_CAP,
  bulkChangeCredentialOrg,
  bulkDeleteCredentials,
  bulkMoveCredentials,
  type Credential,
  type CredentialBulkSet,
  type CredentialMatch,
} from './api'
import { bulkDeletesLastGithub, withLastGithubWarning } from './lastGithubWarning'
import { FONT_SIZE } from '../../theme'

// BULK ACTIONS ON THE CREDENTIALS ROSTER: Flat mode's per-row tick, the page tick, "Select all N
// matching", the action bar and its confirms. Each action is ONE server call over the whole set
// (`routes/credentials/bulk.py`), all or nothing — never one call per row (`useBulkRuns` is deliberately not used).
// "All matching" sends the roster's filters (`match`) with the count the confirm named (`expect_count`), never ids,
// so a set that changed meanwhile is refused, not acted on. The server is the gate: a tick offers, it grants nothing.

const CAP_REASON = `At most ${groupedCount(BULK_CAP)} secrets per action. Narrow the filter.`

/** A count as the reader sees it: `10,000+` past the server's count cap. */
function countText(count: number, capped: boolean): string {
  return `${groupedCount(count)}${capped ? '+' : ''}`
}

function plural(count: string, one: boolean): string {
  return `${count} secret${one ? '' : 's'}`
}

/** The applied filters as the bulk routes' `match` (`roster.RosterFilterSet`), or null when the roster's narrowing
 *  cannot be said in it: an operator's global org filter naming several orgs rides a header the write ignores. */
function matchOf(view: CredentialView): CredentialMatch | null {
  const match: CredentialMatch = {}
  if (view.type) match.type = view.type
  if (view.q.trim()) match.q = view.q.trim()
  if (view.ownerIds.length > 0) match.owner_id = [...view.ownerIds]
  if (view.secrets) match.secrets = view.secrets
  if (view.health) match.health = view.health
  if (view.mine) match.mine = true
  const scope = currentOrgScope()
  if (scope !== null && scope.length > 1) return null
  if (scope !== null && scope.length === 1) match.org_id = scope[0].id
  return match
}

/** The folder field inside the Move confirm: its own state, read by the confirm's action through `onChange`. */
function FolderField({ onChange }: { onChange: (value: string) => void }) {
  const [value, setValue] = useState('')
  return (
    <Box sx={{ mt: 2 }}>
      <FormTextField
        autoFocus
        label="Folder"
        id="bulk-move-folder"
        help="Keeps each last name part: a/b/jira to team becomes team/jira."
        placeholder="team/service — empty for the top level"
        value={value}
        onChange={next => { setValue(next); onChange(next) }}
        testId="bulk-move-folder"
      />
    </Box>
  )
}

/** The org picker inside the Change org confirm, likewise: the console's one org control, the operator plane refused
 *  as `ChangeOrgDialog` refuses it for a credential. */
function OrgField({ onChange }: { onChange: (org: Org) => void }) {
  const [value, setValue] = useState<Org | null>(null)
  return (
    <Box sx={{ mt: 2 }}>
      <FormField label="Move to" help="The selected credentials move into this org.">
        <OrgSelect
          id="bulk-change-org-target"
          value={value?.id ?? null}
          onChange={(_id, org) => { setValue(org); onChange(org) }}
          optionDisabled={operatorPlaneRefusal('credential')}
          testId="bulk-change-org-target"
        />
      </FormField>
    </Box>
  )
}

export type CredentialBulk = {
  /** The tick for one flat row, as a trailing slot. */
  rowSlot: (row: Credential) => FeedSlot
  /** The select column's header: the page tick (`FeedList`'s `columns`). */
  selectColumn: FeedColumn
  /** The select-all-matching banner, above the rows, once every shown row is ticked. */
  header: ReactNode
  /** The action bar, the confirms' toast. */
  bar: ReactNode
}

export function useCredentialBulk({
  items,
  total,
  totalCapped,
  applied,
  isOperator,
  confirm,
  onDone,
}: {
  /** The rows on screen. */
  items: readonly Credential[]
  /** The roster's count under `applied`, and whether it stopped at the server's cap. */
  total: number
  totalCapped: boolean
  /** The filters the rows were read with: "all matching" sends them, and any change drops the selection. */
  applied: CredentialView
  isOperator: boolean
  confirm: (options: ConfirmOptions) => Promise<boolean>
  /** Re-read the roster after an action landed. */
  onDone: () => void
}): CredentialBulk {
  const onPage = useMemo(() => new Set(items.map(row => row.id)), [items])
  const {
    selected: picked, set: setPicked, toggle, clear: clearPicked, toggleAll, allShownSelected, someShownSelected,
  } = useRowSelection(onPage)
  const [allMatching, setAllMatching] = useState(false)
  const [toast, setToast] = useState<{ text: string; seq: number } | null>(null)

  const clear = () => {
    clearPicked()
    setAllMatching(false)
  }

  /* ANY FILTER OR ORDER CHANGE DROPS THE SELECTION: what was ticked was read under the old filters. */
  const match = matchOf(applied)
  const filterKey = JSON.stringify([match, applied.sort, currentOrgScope()?.map(org => org.id) ?? null])
  const lastKey = useRef(filterKey)
  useEffect(() => {
    if (lastKey.current === filterKey) return
    lastKey.current = filterKey
    clearPicked()
    setAllMatching(false)
  }, [filterKey, clearPicked])

  const pageAll = onPage.size > 0 && (allMatching || allShownSelected)
  const pageSome = allMatching ? onPage.size > 0 : someShownSelected

  /* The header tick is the rows on screen; off "all matching", it drops the lot. */
  function togglePage() {
    if (allMatching) clear()
    else toggleAll()
  }

  function toggleRow(id: string) {
    if (allMatching) {
      // Unticking one row of "all matching" falls back to the shown rows minus that row: the rest are off screen.
      setAllMatching(false)
      setPicked(new Set([...onPage].filter(other => other !== id)))
      return
    }
    toggle(id)
  }

  const count = allMatching ? total : picked.size
  const capped = allMatching && totalCapped
  const shown = countText(count, capped)
  const overCap = capped || count > BULK_CAP
  const set = (): CredentialBulkSet => (allMatching && match !== null ? { match } : { ids: [...picked] })

  function landed(verb: string, done: number, where = '') {
    clear()
    setToast(prev => ({ text: `${verb} ${groupedCountOf(done, 'secret')}${where}`, seq: (prev?.seq ?? 0) + 1 }))
    onDone()
  }

  async function runDelete() {
    let done = 0
    const lastGithub = await bulkDeletesLastGithub(allMatching && match !== null ? { applied } : { ids: [...picked] }, items)
    const ok = await confirm(danger(`Delete ${shown}`, {
      title: `Delete ${plural(shown, count === 1)}`,
      message: withLastGithubWarning(
        `This removes ${count === 1 ? 'it' : `all ${shown}`} and the secrets stored with ${count === 1 ? 'it' : 'them'}. This cannot be undone.`,
        lastGithub,
      ),
      typed: count > 1 ? { name: String(count), action: 'the delete' } : undefined,
      run: {
        action: async () => { done = (await bulkDeleteCredentials(set(), count)).count },
        failed: 'Failed to delete secrets',
        busyLabel: 'Deleting…',
      },
    }))
    if (ok) landed('Deleted', done)
  }

  async function runMove() {
    let to = ''
    let done = 0
    const ok = await confirm({
      title: `Move ${plural(shown, count === 1)}`,
      message: (
        <>
          Each keeps its own name under the folder.
          <FolderField onChange={value => { to = value }} />
        </>
      ),
      confirmLabel: `Move ${shown}`,
      run: {
        action: async () => { done = (await bulkMoveCredentials(set(), count, to.trim())).count },
        failed: 'Failed to move secrets',
        busyLabel: 'Moving…',
      },
    })
    if (ok) landed('Moved', done)
  }

  async function runChangeOrg() {
    let target: Org | null = null
    let done = 0
    const ok = await confirm({
      title: `Change org of ${plural(shown, count === 1)}`,
      message: (
        <>
          Refused for system, NodeVault, project and in-use credentials.
          <OrgField onChange={org => { target = org }} />
        </>
      ),
      confirmLabel: 'Change org',
      run: {
        action: async () => {
          const org = target as Org | null
          if (org === null) throw new Error('Pick an org')
          done = (await bulkChangeCredentialOrg(set(), count, org.id)).count
        },
        failed: 'Failed to change org',
        busyLabel: 'Moving…',
      },
    })
    if (ok) landed('Moved', done, ` to ${(target as Org | null)?.name ?? 'the org'}`)
  }

  const refusal = overCap ? { disabled: true, disabledReason: CAP_REASON } : {}
  const actions: BulkAction[] = [
    { id: 'delete', label: 'Delete', icon: '✕', destructive: true, onRun: () => void runDelete(), ...refusal },
    { id: 'move', label: 'Move to folder', icon: '→', onRun: () => void runMove(), ...refusal },
    ...(isOperator
      ? [{ id: 'change-org', label: 'Change org', icon: '⇄', onRun: () => void runChangeOrg(), ...refusal }]
      : []),
  ]

  const rowSlot = (row: Credential): FeedSlot => selectSlot('col-select',
    <SelectRowBox testId="credential-select" checked={allMatching || picked.has(row.id)} onChange={() => toggleRow(row.id)} name={row.name} />,
  )

  /* Once every shown row is ticked, offer the rest: a link to every row the filters match (the "N" capped as the
     count is). Not when the narrowing cannot be sent (`matchOf`), nor when the shown rows are everything. */
  const offerAll = pageAll && !allMatching && match !== null && (total > onPage.size || totalCapped)
  /* THE SELECT COLUMN'S HEADER TICKS EVERY ROW SHOWN (as `/users` and `/nodes`), over the rows' ticks. */
  const selectColumn: FeedColumn = {
    key: 'select',
    ariaLabel: 'Select shown',
    label: (
      <SelectAllBox
        testId="credential-select-page"
        allSelected={pageAll}
        someSelected={pageSome}
        onChange={togglePage}
        shown={`${onPage.size} secrets shown`}
      />
    ),
  }
  const header = onPage.size === 0 ? null : (
    <>
      {(offerAll || allMatching) && (
        <Box
          data-testid="credential-select-banner"
          sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 1, fontSize: FONT_SIZE.base, py: 0.5 }}
        >
          {allMatching ? (
            <>
              <span>All {shown} matching are selected.</span>
              <Button size="small" onClick={clear} data-testid="credential-select-clear">Clear selection</Button>
            </>
          ) : (
            <>
              <span>All {onPage.size} shown are selected.</span>
              <Button size="small" onClick={() => setAllMatching(true)} data-testid="credential-select-all">
                Select all {countText(total, totalCapped)} matching
              </Button>
            </>
          )}
        </Box>
      )}
    </>
  )

  const bar = (
    <>
      <BulkActionBar count={count} onClear={clear} actions={actions} label="Bulk secret actions" />
      <Toast toast={toast} onClose={() => setToast(null)} testId="credentials-bulk-toast" />
    </>
  )

  return { rowSlot, selectColumn, header, bar }
}
