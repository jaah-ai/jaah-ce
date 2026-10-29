import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link as RouterLink, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import useMediaQuery from '@mui/material/useMediaQuery'
import { ApiError, apiDetail } from '../../api'
import { useAuth, useCan } from '../../auth'
import { isEnabled } from '../../lib/editionFeatures'
import { useChangeOrgAction } from '../../components/ChangeOrgDialog'
import { useConfirm } from '../../components/ConfirmDialog'
import { useOpenDetailPath } from '../../components/ListDetail'
import { useDebouncedCallback } from '../../hooks/useDebouncedCallback'
import { credentialFeedRow, credentialTypeSlotWidth, DESCRIPTION_QUERY } from '../../lib/credentialRow'
import { CompactVerbLabel, compactVerbProps } from '../../ui/CompactVerb'
import { useOrgVisible, useOtherOrgOf } from '../../ui/OrgIndicator'
import { actAsOnce } from '../../lib/actAs'
import CredentialTreeView from './CredentialTreeView'
import {
  clearCredentialFilters,
  CREDENTIAL_VIEW_ONLY_FIELDS,
  hasActiveCredentialFilters,
  loadCredentialView,
  saveCredentialView,
  type CredentialSort,
  type CredentialView,
} from '../../lib/credentialFilterStorage'
import { usePersistedListView } from '../../lib/listViewStorage'
import Banner from '../../ui/Banner'
import EmptyState from '../../ui/EmptyState'
import NoMatchesState from '../../ui/NoMatchesState'
import ListStates from '../../ui/feed/ListStates'
import type { FeedRowSpec } from '../../ui/feed/types'
import { CREDENTIALS_LIST_CONTAINER_SX, CREDENTIALS_TYPE_UNDER, tapTargetSx } from '../../ui/listStyles'
import { Pager } from '../../ui/Pager'
import PageHeader from '../../ui/PageHeader'
import SortMenu, { type SortMenuColumn } from '../../ui/SortMenu'
import { useCredentialBulk } from './CredentialBulk'
import CredentialFilterStrip, {
  useCredentialFilterRosters,
} from './CredentialFilterStrip'
import { useCredentialTypes } from './useCredentialTypes'
import {
  deleteCredential,
  listCredentials,
  PER_PAGE,
  renameCredential,
  type Credential,
} from './api'

// Directory > Secrets — the roster of third-party connections. Open to every signed-in user
// (App.tsx). The server is the authoritative gate (every /api/credentials endpoint declares a
// `credentials:*` key, admits a keyless session only to the rows it created, and scopes rows by grant);
// `useCan` and the row's `editable` here only avoid OFFERING refused writes.
//
// A Directory tab's page at `/credentials` (App.tsx owns its address and gate); editor routes are separate pages.
// Which types exist is server-owned (`GET /api/credentials/types`); nothing here, copy included,
// enumerates them.
//
// The row is `lib/credentialRow.tsx`'s, the single home for every column decision. This page
// owns the fetch, pager, delete confirmation, permission reads and the one viewport decision the row
// builder cannot make (`DESCRIPTION_QUERY`, a hook).
//
// Tree mode (lazy-loaded) is `./CredentialTreeView.tsx`: the server lists one level per
// request (tenants, homes, folders), so the tree is never a slice of the roster. This page owns the flat fetch and
// whether the pager is drawn, and lends the tree its row deps, its writes and their re-read. Naming is untouched:
// the tree is a VIEW over names; dragging RENAMES, one `PATCH /api/credentials/{id}`.

/** Skeleton rows while loading: a handful, not `PER_PAGE`, so the page does not SHRINK on settle. */
const PAGE_SKELETON_ROWS = 5

/** A cut name's floor beside an open credential: ~16 characters, enough to tell two rows apart
 *  (`e2e/directory-split-width.spec.ts`'s `TITLE_FLOOR_PX`). */
const TITLE_FLOOR_PX = 120
/** The hover copy control `lib/credentialRow.tsx` budgets beside the name: 24px plus its 4px margin. */
const COPY_NAME_PX = 28

/* THE NAME KEEPS ITS FLOOR BESIDE AN OPEN CREDENTIAL. The ~320px list left a long name less than the
   floor once a NodeVault mark, the hover copy control and a scrollbar took their share. The floor is on the
   naming CELL, so a cut name is held at 120px (the cell keeps that width even for a short one); under ~400px (where the
   type already hangs) the copy control leaves the cell (⋯ "Copy name" is its other home), so all of it is the
   name's. CSS, not a row field: `ui/feed` is shared and the band is a container query. */
const NAME_FLOOR_SX = {
  '& [data-testid="feed-row"] > [role="cell"]:has(.feed-title)': {
    minWidth: TITLE_FLOOR_PX + COPY_NAME_PX,
    [CREDENTIALS_TYPE_UNDER]: { minWidth: TITLE_FLOOR_PX },
  },
  [CREDENTIALS_TYPE_UNDER]: { '& .feed-title-action': { display: 'none' } },
} as const

/** The order menu's keys: `created` stands for `newest`/`oldest` (its two directions); the rest read A–Z. */
type SortKey = 'created' | 'name' | 'type' | 'org'
const SORT_COLUMNS: readonly SortMenuColumn<SortKey>[] = [
  { key: 'created', label: 'Created' },
  { key: 'name', label: 'Name' },
  { key: 'type', label: 'Type' },
  { key: 'org', label: 'Org' },
]

/** `sort` as the menu's `{ key, dir }`. */
function sortMenuState(sort: CredentialSort): { key: SortKey; dir: 'asc' | 'desc' } {
  if (sort === 'newest') return { key: 'created', dir: 'desc' }
  if (sort === 'oldest') return { key: 'created', dir: 'asc' }
  return { key: sort, dir: 'asc' }
}

/** A menu pick as the wire `sort`: picking Created again flips its direction. */
function nextSort(current: CredentialSort, key: SortKey): CredentialSort {
  if (key !== 'created') return key
  return current === 'newest' ? 'oldest' : 'newest'
}

/** `1–25 of 1,234`, or `26–50 of 10,000+` where keyset paging capped the count (`total_capped`); a whole
 *  uncapped roster is its bare count. */
function rosterRange(shown: { first: number; last: number }, total: number, capped: boolean): string {
  if (shown.first === 1 && shown.last === total && !capped) return total.toLocaleString()
  return `${shown.first.toLocaleString()}–${shown.last.toLocaleString()} of ${total.toLocaleString()}${capped ? '+' : ''}`
}

/** `1–25 of 1,234` and the order menu, on one line above the rows. The Org order only where the viewer sees
 *  tenants (`useOrgVisible`): a tenant's roster is one org. */
function RosterSummary({ range, sort, onSort, showOrg }: {
  range: string
  sort: CredentialSort
  onSort: (sort: CredentialSort) => void
  showOrg: boolean
}) {
  return (
    <Box data-testid="credentials-summary" sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1, mt: 0.5 }}>
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>{range}</Typography>
      <SortMenu
        noun="secrets"
        columns={showOrg ? SORT_COLUMNS : SORT_COLUMNS.filter(column => column.key !== 'org')}
        // A stored `org` order with no Org item (no longer an operator) reads as Name: one org orders the same.
        sort={sortMenuState(!showOrg && sort === 'org' ? 'name' : sort)}
        onSort={key => onSort(nextSort(sort, key))}
      />
    </Box>
  )
}

/** Tree mode's stand-in for the flat rows: a module constant (stable identity, no allocation). */
const NO_ROWS: FeedRowSpec[] = []

/** Does this patch touch ONLY how the roster is drawn (no pager reset, request or debounce)? Which fields
 *  those are is `CREDENTIAL_VIEW_ONLY_FIELDS`'s (shared with `hasActiveCredentialFilters` and
 *  `clearCredentialFilters`). A mixed patch counts as a filter change (re-fetch is the safe answer); an
 *  empty patch answers `true` and does nothing. */
function isViewOnlyPatch(patch: Partial<CredentialView>): boolean {
  const keys = Object.keys(patch) as (keyof CredentialView)[]
  return keys.every(key => CREDENTIAL_VIEW_ONLY_FIELDS.includes(key))
}

export default function Credentials() {
  const [items, setItems] = useState<Credential[]>([])
  const [total, setTotal] = useState(0)
  /* KEYSET PAGING: the API serves `next_cursor` and no previous one, so the page holds the stack of
     cursors it walked — `[null]` is page 1, Previous pops, Next pushes. A reload lands on page 1 (there is no
     "jump to page N"). Tree mode reads its own levels and ignores the stack, so switching modes keeps Flat's place. */
  const [cursors, setCursors] = useState<(string | null)[]>([null])
  const page = cursors.length
  const cursor = cursors[cursors.length - 1]
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [totalCapped, setTotalCapped] = useState(false)
  /* The ordinal of the first row held — set with the rows, so the range never pairs one page's rows with
     another's number (a mode switch or a stale-cursor restart re-renders before the fetch lands). */
  const [firstShown, setFirstShown] = useState(1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busyId, setBusyId] = useState<string | null>(null)
  const can = useCan()
  const { admin } = useAuth()
  const navigate = useNavigate()
  const [confirm, confirmDialog] = useConfirm()
  const changeOrg = useChangeOrgAction('credential')

  /* May this viewer see which tenant owns a credential (`ui/OrgIndicator.tsx::useOrgVisible`)?
     It drives each row's org slot and ⋯ item (`CredentialRowDeps.showOrg`). There is no page-level Org
     filter: an operator's org narrowing comes from the global filter (`lib/orgScope.ts`, R5.6). */
  const showOrg = useOrgVisible()
  /* An operator's view of another org's row: Edit becomes Act as; Delete goes as that org, once. */
  const otherOrgOf = useOtherOrgOf()

  /* The option rosters, read ONCE and shared by the strip and the empty state, which must NAME picked
     filters (R2.6). `useCredentialTypes` is the editor pages' hook. */
  const credentialTypes = useCredentialTypes()
  const rosters = useCredentialFilterRosters()

  /* ── The filter strip's state ────────────────────────────────────────────────
     Two values: `view` is what the controls show and persist (moves per keystroke); `appliedView` builds
     the request, Search reaching it after 150ms quiet (600ms max during sustained typing); discrete picks
     apply at once.
     Restored at mount in a lazy initialiser (R2.8) from `lib/credentialFilterStorage.ts`; both seeded from
     the SAME read, so a restored visit does not fetch unfiltered first. `loadCredentialView` keys on the
     same `admin?.id`. */
  const listView = usePersistedListView<CredentialView>(
    () => loadCredentialView(admin?.id ?? null),
    next => saveCredentialView(admin?.id ?? null, next),
  )
  const { view, toggleGroup } = listView
  /* The tree's own writes to the open set (a reveal, the silent drop): the list view's patch (view-only), not the
     page's `patchView`, which also moves the pager. Stable, so the tree's effect does not re-run per render. */
  const patchListView = listView.patchView
  const setExpanded = useCallback((expanded: string[]) => { patchListView({ expanded }) }, [patchListView])
  const [appliedView, setAppliedView] = useState<CredentialView>(view)

  /* The hook reads its callback from a ref at fire time, so it sees the LATEST `view`. */
  const { trigger: scheduleSearch, cancel: cancelSearch } = useDebouncedCallback(
    () => setAppliedView(view),
    150,
    600,
  )

  /* ── Tree mode: `treeMode` is read off `view`, never `appliedView` (debouncing the toggle would lag
     it); filters still come from `appliedView`. The tree fetches its own levels: the flat page is not read. */
  const treeMode = view.mode === 'tree'
  const per = PER_PAGE

  /* One writer for every control: the view moves, the pager resets (R2.7 — page 4 of a two-page result is
     blank), and the change persists. Only Search is debounced.
     The five FILTERS reset the pager and re-fetch; `mode`/`expanded` (view-only, per
     `hasActiveCredentialFilters`) must not reset the pager, apply a pending search, or re-fetch. */
  function patchView(patch: Partial<CredentialView>) {
    const next = listView.patchView(patch)

    if (isViewOnlyPatch(patch)) {
      /* Tree ignores the cursor stack, so Flat's page waits there untouched. `expanded` alone touches nothing. */
      if (patch.mode !== undefined && patch.mode !== view.mode) {
        /* Mark loading in the same commit as the flip: back in Flat, the page fetched afresh paints, never the
           page Flat left behind. */
        setLoading(true)
      }
      /* `appliedView` is deliberately NOT touched: `next` carries the unapplied `q`, and nothing reads view
         fields off `appliedView`. `view` is their one home. */
      return
    }

    /* Back to page 1, in Tree mode too: a cursor belongs to the filters it was minted under. */
    setCursors([null])
    if ('q' in patch && Object.keys(patch).length === 1) {
      scheduleSearch()
      return
    }
    // A discrete pick cancels a pending keystroke: `next` already carries `q`, so one request covers both.
    cancelSearch()
    setAppliedView(next)
  }

  /* Back to the whole roster in one gesture, via the same writer (pager resets, cleared view persists). */
  function clearFilters() {
    /* The narrowings only (`lib/credentialFilterStorage.ts`'s `clearCredentialFilters`): the default view
       would also drop Tree mode and collapse folders. */
    patchView(clearCredentialFilters(view))
  }

  /* The roster request with its signal: the effect aborts the previous controller, and both tails check
     `aborted` FIRST so a late superseded answer cannot write state (`ui/AsyncLookup`'s discipline, R2.9). */
  const load = useCallback(async (signal?: AbortSignal) => {
    // The tree reads its own levels (`CredentialTreeView`); Flat's page is fetched when Flat is back.
    if (treeMode) return
    try {
      const data = await listCredentials(1, per, {
        type: appliedView.type,
        q: appliedView.q,
        mine: appliedView.mine,
        ownerIds: appliedView.ownerIds,
        secrets: appliedView.secrets,
        health: appliedView.health,
        sort: appliedView.sort,
        keyset: true,
        cursor,
        signal,
      })
      if (signal?.aborted) return
      /* A later page gone empty (its rows deleted from the pane beside the list, or by someone else) steps back
         rather than drawing "No secrets yet" with no pager to leave by. */
      if (cursor !== null && data.items.length === 0) {
        setCursors(current => current.slice(0, -1))
        return
      }
      setItems(data.items)
      setFirstShown((page - 1) * per + 1)
      setTotal(data.total)
      setTotalCapped(data.total_capped === true)
      setNextCursor(data.next_cursor ?? null)
      setError('')
      setLoading(false)
    } catch (err) {
      if (signal?.aborted) return
      /* A cursor the server no longer honours (422, "This page link is out of date") restarts at page 1, quietly:
         the operator asked for rows, not for the reason a link went stale. Loading stays on through the restart. */
      if (cursor !== null && err instanceof ApiError && err.status === 422) {
        setCursors([null])
        return
      }
      setError(apiDetail(err, 'Failed to load secrets'))
      setLoading(false)
    }
    /* The six filter fields and the order one by one, NOT `appliedView` whole: its identity also changes on
       `expanded`, which would re-fetch per group toggle. `treeMode` is listed: Flat's page is read on the way back. */
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    cursor,
    page,
    treeMode,
    per,
    appliedView.type,
    appliedView.q,
    appliedView.mine,
    /* Joined: a new array of the same ids must not re-fetch. */
    appliedView.ownerIds.join(','),
    appliedView.secrets,
    appliedView.health,
    appliedView.sort,
  ])

  /* THE EDITOR BESIDE THE LIST CLOSING IS A RE-READ: this page stays mounted behind an open
     editor (components/ListDetail.tsx), so a credential created, edited or deleted there — Save and Delete
     both navigate back here, closing the pane — would otherwise stay invisible until a reload. Keyed on the
     close, not on every open or editor-to-editor move. Full-page editors (narrow viewports) remount this
     page instead, so `openPath` stays null there and nothing fires twice. A bump of `reloads`, so the re-read
     rides the abortable effect below and a filter change made meanwhile still wins. */
  const [reloads, setReloads] = useState(0)
  const openPath = useOpenDetailPath()
  const lastOpenPath = useRef(openPath)
  useEffect(() => {
    const wasOpen = lastOpenPath.current !== null
    lastOpenPath.current = openPath
    if (wasOpen && openPath === null) setReloads(n => n + 1)
  }, [openPath])

  const lastReloads = useRef(reloads)
  useEffect(() => {
    const controller = new AbortController()
    // A re-read keeps the rows on screen; a new filter or page shows the skeleton.
    if (lastReloads.current === reloads) setLoading(true)
    lastReloads.current = reloads
    void load(controller.signal)
    return () => controller.abort()
  }, [load, reloads])

  async function handleDelete(row: Credential) {
    /* One write at a time: `busyId` is a single slot, so a Delete's `finally` would clear an in-flight
       move's busy state (and a folder move marks no row busy to disable Delete on). */
    if (busyId !== null) return
    /* The DELETE runs inside the confirm: a refusal stays in the dialog with its reason. Another org's
       row goes as that org for this one request (`actAsOnce`), as a task's Delete does. */
    const ok = await confirm({
      title: 'Delete secret',
      identity: `${row.type_label} · ${row.name}`,
      message:
        'This removes the secret and the values stored with it. This cannot be undone.',
      confirmLabel: 'Delete',
      destructive: true,
      run: {
        action: async () => {
          setBusyId(row.id)
          try {
            await deleteCredential(row.id, actAsOnce(otherOrgOf(row.org)))
          } catch (err) {
            setBusyId(null)
            throw err
          }
        },
        failed: 'Failed to delete secret',
        busyLabel: 'Deleting…',
      },
    })
    if (!ok) return
    if (treeMode) {
      // The tree re-reads its open levels in place.
      setReloads(n => n + 1)
      setBusyId(null)
      return
    }
    try {
      // Deleting a page's LAST row past page 1: step back BEFORE re-reading, avoiding an empty page N
      // ("No secrets yet") and a second round trip. Popping the cursor re-runs `load` once.
      if (items.length === 1 && page > 1) {
        setCursors(current => current.slice(0, -1))
        return
      }
      /* Otherwise re-read (rows shift up from the next page; `total` drives Next) through the abortable effect, so a
         Next pressed meanwhile wins rather than being overwritten by this answer. */
      setReloads(n => n + 1)
    } finally {
      setBusyId(null)
    }
  }

  /* ── Re-filing a credential by dragging it ───────────────────────────────────
     The write is a RENAME: a folder is a `name` prefix, not an entity. The tree owns the gesture and which drops
     it offers (`./CredentialTreeView.tsx`); the page owns the request, the in-flight row and the re-read. A refusal
     rejects back to the tree, which says so. */
  async function refileCredential(row: Credential, name: string) {
    setBusyId(row.id)
    try {
      await renameCredential(row.id, name, actAsOnce(otherOrgOf(row.org)))
      // Re-read: a renamed credential can fall out of the current filter.
      setReloads(n => n + 1)
    } finally {
      setBusyId(null)
    }
  }

  /* The roster as `ui/feed/FeedList` rows, composed via `lib/credentialRow.tsx`; the page supplies
     permissions, in-flight row, handlers and the viewport question.
     The description's 1280px decision is a JS read (a hook, so not in `lib/`), so "no description" means
     the slot is ABSENT, reserving none of its 220px (`lib/credentialRow.tsx`'s `DESCRIPTION_SLOT`). */
  /* Nor beside an open editor: the pane takes the width the 1280px decision priced in, and the fixed slot then
     squeezed the NAME out of the row, leaving only the description column to tell rows apart. */
  const showDescription = useMediaQuery(DESCRIPTION_QUERY) && openPath === null
  /* Off the loaded page, not per mode: switching Flat/Tree never moves the column. Memoised: it measures text,
     and the page re-renders per keystroke. */
  const besideDetail = openPath !== null
  const typeWidth = useMemo(() => credentialTypeSlotWidth(items, besideDetail), [items, besideDetail])
  /* Row deps not on the credential, shared by both modes. `busy` is per-row and computed per
     mode (`./credentialTree.ts`'s `busyId`). */
  const rowDeps = {
    showDescription,
    typeWidth,
    showOrg,
    besideDetail,
    canEdit: can('credentials:edit'),
    canDelete: can('credentials:delete'),
    onEdit: (target: Credential) => navigate(`/credentials/${target.id}`),
    onDelete: (target: Credential) => { void handleDelete(target) },
    viewerEmail: admin?.email ?? null,
    changeOrg: (target: Credential) => changeOrg.menuItem({
      id: target.id, name: target.name, org: target.org, href: `/credentials/${target.id}`,
    }),
  }

  /* BULK ACTIONS (`./CredentialBulk.tsx`): Flat mode's ticks, select-all-matching and the action bar.
     Re-reads through `reloads`, as a closing editor does. */
  const bulk = useCredentialBulk({
    items,
    total,
    totalCapped,
    applied: appliedView,
    isOperator: admin?.is_operator === true,
    confirm,
    onDone: () => setReloads(n => n + 1),
  })

  /* Each mode builds only its own rows: nothing is memoized and this re-renders per keystroke, so building
     both would double `credentialFeedRow` calls (400 at 200 rows). */
  const flatRows: FeedRowSpec[] = treeMode
    ? NO_ROWS
    : items.map((row): FeedRowSpec => {
        const spec = credentialFeedRow(row, { ...rowDeps, busy: busyId === row.id })
        return { ...spec, slots: [...(spec.slots ?? []), bulk.rowSlot(row)] }
      })

  const shown = { first: firstShown, last: firstShown + items.length - 1 }
  // Rows deleted since an earlier page can leave a later one past the re-counted total: never "26–50 of 45".
  const range = rosterRange(shown, Math.max(total, shown.last), totalCapped)

  return (
    /* The query container `credentialTypeSlotWidth`'s `cqw` cap and the Type slot's gate read: the list's own
       width, not the window's. */
    <Box sx={besideDetail ? [CREDENTIALS_LIST_CONTAINER_SX, NAME_FLOOR_SX] : CREDENTIALS_LIST_CONTAINER_SX}>
      {/* ADD CREDENTIAL, on the Directory strip via a titleless header (`components/TabActionsSlot`).
          Ungated: `POST /api/credentials` admits every session to create its OWN row (`own_credential`). */}
      <PageHeader
        actions={
          <Button component={RouterLink} to="/credentials/new" variant="contained" color="primary" {...compactVerbProps('Add secret', tapTargetSx)}>
            <CompactVerbLabel label="Add secret" />
          </Button>
        }
      />

      {/* The filter strip (R2.1), above the data it narrows; it holds no state of its own. */}
      <CredentialFilterStrip
        view={view}
        onChange={patchView}
        types={credentialTypes.types}
        typesError={credentialTypes.error}
        typesLoading={credentialTypes.loading}
        rosters={rosters}
      />

      {error && <Banner severity="error">{error}</Banner>}

      {/* HOW MANY, AND IN WHAT ORDER: the roster's size was nowhere on the page, and its order fixed.
          Outside the filter strip, whose chrome budget is pinned (`e2e/credentials-filters.spec.ts`). Kept mounted
          through a re-fetch (the last page's numbers): unmounting it jumped the rows a line and dropped the ⇅
          trigger's focus to the body after a pick. */}
      {/* Flat mode's: the tree counts beside every node and is always in name order. */}
      {!treeMode && items.length > 0 && (
        <RosterSummary
          range={range}
          sort={view.sort}
          onSort={sort => patchView({ sort })}
          showOrg={showOrg}
        />
      )}

      {!treeMode && !loading && bulk.header}
      {!treeMode && bulk.bar}

      {treeMode ? (
        <CredentialTreeView
          filters={{
            type: appliedView.type, q: appliedView.q, mine: appliedView.mine, ownerIds: appliedView.ownerIds,
            secrets: appliedView.secrets, health: appliedView.health,
          }}
          filtered={hasActiveCredentialFilters(appliedView)}
          operator={showOrg}
          rootOrgId={!isEnabled('multi_org_tenancy') ? admin?.org?.id : undefined}
          expanded={view.expanded}
          onToggle={toggleGroup}
          onExpanded={setExpanded}
          rowDeps={rowDeps}
          busyId={busyId}
          canDrag={rowDeps.canEdit}
          onRefile={refileCredential}
          reloadToken={reloads}
          empty={hasActiveCredentialFilters(appliedView)
            ? <NoMatchesState onClear={clearFilters} />
            : <EmptyState title="No secrets yet" scoped />}
        />
      ) : (
        /* Skeleton rows, not a spinner (avoids the layout jump the space criterion measures). Two empty states,
           forked on the FILTERS (R2.6), not `items.length`: "No secrets yet" over a filtered-out full roster
           invited duplicates. The filtered arm offers the undo. */
        <ListStates
          loading={loading}
          skeleton={{ rows: PAGE_SKELETON_ROWS }}
          empty={items.length === 0}
          noMatch={hasActiveCredentialFilters(appliedView) && <NoMatchesState onClear={clearFilters} />}
          emptyTitle="No secrets yet"
          emptyScoped
          feed={{ ariaLabel: 'Secrets', testId: 'credentials-list', sections: [{ key: 'credentials', rows: flatRows }] }}
        />
      )}

      {/* The pager is Flat mode's alone (R3.4); HIDDEN, not disabled, in Tree mode (no page to move to). Its readout is
          the summary's range: keyset paging knows no page count. Next is live while the server hands a cursor. */}
      {!treeMode && items.length > 0 && (page > 1 || nextCursor !== null) && (
        <Pager
          testId="pagination"
          page={page}
          totalPages={page + (nextCursor === null ? 0 : 1)}
          label={range}
          noun="secrets"
          previousDisabled={loading || page <= 1}
          nextDisabled={loading || nextCursor === null}
          onPrevious={() => setCursors(current => (current.length > 1 ? current.slice(0, -1) : current))}
          onNext={() => { if (nextCursor !== null) setCursors(current => [...current, nextCursor]) }}
        />
      )}

      {confirmDialog}
      {changeOrg.dialog}
    </Box>
  )
}
