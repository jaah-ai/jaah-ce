import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import useMediaQuery from '@mui/material/useMediaQuery'
import { ApiError } from '../../api'
import { useAuth, useCan } from '../../auth'
import { isEnabled } from '../../lib/editionFeatures'
import { useChangeOrgAction } from '../../components/ChangeOrgDialog'
import { danger, useConfirm } from '../../components/ConfirmDialog'
import { useOpenDetailPath } from '../../components/ListDetail'
import { useDebouncedCallback } from '../../hooks/useDebouncedCallback'
import { usePagedList } from '../../hooks/usePagedList'
import {
  CREDENTIAL_SLOT_COLUMNS,
  CREDENTIAL_TITLE_COLUMN,
  credentialFeedRow,
  credentialTypeSlotWidth,
  DESCRIPTION_QUERY,
} from '../../lib/credentialRow'
import NewButton from '../../ui/NewButton'
import { useOrgVisible, useOtherOrgOf } from '../../ui/OrgIndicator'
import { actAsOnce } from '../../lib/actAs'
import CredentialTreeView from './CredentialTreeView'
import { deletesLastGithub, withLastGithubWarning } from './lastGithubWarning'
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
import type { FeedColumns, FeedRowSpec } from '../../ui/feed/types'
import { CREDENTIALS_LIST_CONTAINER_SX, CREDENTIALS_TYPE_UNDER, tapTargetSx } from '../../ui/listStyles'
import { LoadMoreFooter } from '../../ui/PagedList'
import PageHeader from '../../ui/PageHeader'
import SortMenu, { type SortMenuColumn } from '../../ui/SortMenu'
import { useCredentialBulk } from './CredentialBulk'
import CredentialFilterStrip, {
  useCredentialFilterRosters,
} from './CredentialFilterStrip'
import { useCredentialTypes } from './useCredentialTypes'
import { testsCredentials } from './ownership'
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
// owns the fetch, Load more footer, delete confirmation, permission reads and the one viewport decision the row
// builder cannot make (`DESCRIPTION_QUERY`, a hook).
//
// Tree mode (lazy-loaded) is `./CredentialTreeView.tsx`: the server lists one level per
// request (tenants, homes, folders), so the tree is never a slice of the roster. This page owns the flat fetch and
// whether the footer is drawn, and lends the tree its row deps, its writes and their re-read. Naming is untouched:
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

/* A header press on Name, Type or Org orders by it (the route sorts those A–Z only); pressing the sorted one again
   returns to the default, newest first. */
function headerSort(current: CredentialSort, key: string): CredentialSort {
  return current === key ? 'newest' : (key as CredentialSort)
}

/** Tree mode's stand-in for the flat rows: a module constant (stable identity, no allocation). */
const NO_ROWS: FeedRowSpec[] = []

/** Does this patch touch ONLY how the roster is drawn (no restart, request or debounce)? Which fields
 *  those are is `CREDENTIAL_VIEW_ONLY_FIELDS`'s (shared with `hasActiveCredentialFilters` and
 *  `clearCredentialFilters`). A mixed patch counts as a filter change (re-fetch is the safe answer); an
 *  empty patch answers `true` and does nothing. */
function isViewOnlyPatch(patch: Partial<CredentialView>): boolean {
  const keys = Object.keys(patch) as (keyof CredentialView)[]
  return keys.every(key => CREDENTIAL_VIEW_ONLY_FIELDS.includes(key))
}

/** `view` with a "Last test failed" narrowing dropped for a viewer who cannot test; the same object otherwise. */
function withoutTestFilter(view: CredentialView, showTest: boolean): CredentialView {
  return showTest || view.health !== 'test_failed' ? view : { ...view, health: '' }
}

export default function Credentials() {
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
  /* A viewer who can never test sees no test-only control (`./ownership.ts::testsCredentials`); a "Last test
     failed" filter stored before that changed reads as none, so nothing narrows the list invisibly. */
  const showTest = testsCredentials(admin, can('credentials:edit'))
  const view = withoutTestFilter(listView.view, showTest)
  const { toggleGroup } = listView
  /* The tree's own writes to the open set (a reveal, the silent drop): the list view's patch (view-only), not the
     page's `patchView`, which also re-reads the roster. Stable, so the tree's effect does not re-run per render. */
  const patchListView = listView.patchView
  const setExpanded = useCallback((expanded: string[]) => { patchListView({ expanded }) }, [patchListView])
  const [rawAppliedView, setAppliedView] = useState<CredentialView>(view)
  const appliedView = withoutTestFilter(rawAppliedView, showTest)

  /* The hook reads its callback from a ref at fire time, so it sees the LATEST `view`. */
  const { trigger: scheduleSearch, cancel: cancelSearch } = useDebouncedCallback(
    () => setAppliedView(view),
    150,
    600,
  )

  /* ── Tree mode: `treeMode` is read off `view`, never `appliedView` (debouncing the toggle would lag
     it); filters still come from `appliedView`. The tree fetches its own levels: the flat page is not read. */
  const treeMode = view.mode === 'tree'

  /* One writer for every control: the view moves, the roster restarts at its first page (R2.7), and the change
     persists. Only Search is debounced.
     The five FILTERS and the order re-fetch; `mode`/`expanded` (view-only, per `hasActiveCredentialFilters`) must
     not apply a pending search or re-fetch — `appliedView` is deliberately NOT touched: `next` carries the
     unapplied `q`, and nothing reads view fields off `appliedView`. `view` is their one home. */
  function patchView(patch: Partial<CredentialView>) {
    const next = listView.patchView(patch)
    if (treeMode && patch.mode === 'flat') setFlatVisit(visit => visit + 1)

    if (isViewOnlyPatch(patch)) return

    if ('q' in patch && Object.keys(patch).length === 1) {
      scheduleSearch()
      return
    }
    // A discrete pick cancels a pending keystroke: `next` already carries `q`, so one request covers both.
    cancelSearch()
    setAppliedView(next)
  }

  /* Back to the whole roster in one gesture, via the same writer (the roster restarts, the cleared view persists). */
  function clearFilters() {
    /* The narrowings only (`lib/credentialFilterStorage.ts`'s `clearCredentialFilters`): the default view
       would also drop Tree mode and collapse folders. */
    patchView(clearCredentialFilters(view))
  }

  /* THE ROSTER READ (`hooks/usePagedList.ts`, keyset "Load more"), keyed on the request: the filter and order
     fields, NOT `appliedView` whole (its identity also changes on `expanded`, which would re-read per group toggle).
     Disabled in Tree mode (the tree reads its own levels); Flat's first page is re-read on the way back. Each answer
     carries the filters it answers, so the rows alone never have to say whose they are — and the visit to Flat it
     was read in: the rows Flat left may have been deleted or renamed in Tree. */
  const { type, q, mine, ownerIds, secrets, health, sort } = appliedView
  const [flatVisit, setFlatVisit] = useState(0)
  const filterKey = JSON.stringify({ type, q, mine, ownerIds, secrets, health, flatVisit })
  const restart = useRef(() => {})
  const fetchPage = useCallback(
    (cursor: string | null) =>
      listCredentials(1, PER_PAGE, { type, q, mine, ownerIds, secrets, health, sort, keyset: true, cursor })
        .then(list => ({ list, filterKey }), err => {
          /* A cursor the server no longer honours (422, "This page link is out of date") restarts at page one,
             quietly: the restart supersedes this read, so its error is never shown. */
          if (cursor !== null && err instanceof ApiError && err.status === 422) restart.current()
          throw err
        }),
    [type, q, mine, ownerIds, secrets, health, sort, filterKey],
  )
  const paging = usePagedList({
    fetchPage,
    rowsOf: page => page.list.items,
    nextOf: page => page.list.next_cursor ?? null,
    errorFallback: 'Failed to load secrets',
    enabled: !treeMode,
  })
  const { rows: items, lastPage, loading, error, refresh } = paging
  restart.current = () => void paging.reload()
  const total = lastPage?.list.total ?? 0
  const totalCapped = lastPage?.list.total_capped === true
  /* ROWS THAT ANSWER THESE FILTERS STAY ON SCREEN while a read is in flight (a new order, a write's re-read, the
     next page): the order controls are the header row's, and a skeleton unmounting it dropped the pressed header's
     focus to the body. A new filter shows the skeleton instead (the held rows answer another question, and would
     pair the new filter with the old total). */
  const skeleton = loading && lastPage?.filterKey !== filterKey

  /* A write's re-read, in whichever mode is up: Flat's loaded pages, its rows on screen until the fresh ones land
     (page one alone would drop the place and the ticks below it); the tree's open levels in place (`reloads` is `CredentialTreeView`'s `reloadToken`). */
  const [reloads, setReloads] = useState(0)
  const reread = useCallback(() => {
    if (treeMode) setReloads(n => n + 1)
    else void refresh()
  }, [treeMode, refresh])

  /* THE EDITOR BESIDE THE LIST CLOSING IS A RE-READ: this page stays mounted behind an open
     editor (components/ListDetail.tsx), so a credential created, edited or deleted there — Save and Delete
     both navigate back here, closing the pane — would otherwise stay invisible until a reload. Keyed on the
     close, not on every open or editor-to-editor move (`reread` is a dependency only for its freshness: a
     re-run without a close does nothing). Full-page editors (narrow viewports) remount this page instead, so
     `openPath` stays null there and nothing fires twice. */
  const openPath = useOpenDetailPath()
  const lastOpenPath = useRef(openPath)
  useEffect(() => {
    const wasOpen = lastOpenPath.current !== null
    lastOpenPath.current = openPath
    if (wasOpen && openPath === null) reread()
  }, [openPath, reread])

  async function handleDelete(row: Credential) {
    /* One write at a time: `busyId` is a single slot, so a Delete's `finally` would clear an in-flight
       move's busy state (and a folder move marks no row busy to disable Delete on). */
    if (busyId !== null) return
    /* The DELETE runs inside the confirm: a refusal stays in the dialog with its reason. Another org's
       row goes as that org for this one request (`actAsOnce`), as a task's Delete does. */
    const lastGithub = await deletesLastGithub(row, otherOrgOf(row.org))
    if (!await confirm(danger('Delete', {
      title: 'Delete secret',
      identity: `${row.type_label} · ${row.name}`,
      message: withLastGithubWarning(
        'This removes the secret and the values stored with it. This cannot be undone.',
        lastGithub,
      ),
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
    }))) return
    try {
      reread()
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
      reread()
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
    showTest,
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
     Re-reads through `reread`, as a closing editor does. */
  const bulk = useCredentialBulk({
    items,
    total,
    totalCapped,
    applied: appliedView,
    isOperator: admin?.is_operator === true,
    confirm,
    onDone: reread,
  })

  /* Each mode builds only its own rows: nothing is memoized and this re-renders per keystroke, so building
     both would double `credentialFeedRow` calls (400 at 200 rows). */
  const flatRows: FeedRowSpec[] = treeMode
    ? NO_ROWS
    : items.map((row): FeedRowSpec => {
        const spec = credentialFeedRow(row, { ...rowDeps, busy: busyId === row.id })
        return { ...spec, slots: [...(spec.slots ?? []), bulk.rowSlot(row)] }
      })

  /* THE HEADER ROW (Flat mode's; the tree is always in name order): a label over every value, the page tick over
     the rows' ticks, and the order menu over the ⋯ column — the one home of Created, which no column heads. The
     roster's size is the footer's caption. The Org order only where the
     viewer sees tenants (`useOrgVisible`): a tenant's roster is one org. */
  const flatColumns: FeedColumns = {
    title: CREDENTIAL_TITLE_COLUMN,
    slots: [...CREDENTIAL_SLOT_COLUMNS.filter(column => showTest || column.key !== 'last-test'), bulk.selectColumn],
    actions: (
      <SortMenu
        compact
        noun="secrets"
        columns={showOrg ? SORT_COLUMNS : SORT_COLUMNS.filter(column => column.key !== 'org')}
        // A stored `org` order with no Org item (no longer an operator) reads as Name: one org orders the same.
        sort={sortMenuState(!showOrg && view.sort === 'org' ? 'name' : view.sort)}
        onSort={key => patchView({ sort: nextSort(view.sort, key) })}
      />
    ),
  }
  const headerSortState = view.sort === 'name' || view.sort === 'type' || (showOrg && view.sort === 'org')
    ? { key: view.sort, dir: 'asc' as const }
    : { key: null, dir: 'asc' as const }

  return (
    /* The query container `credentialTypeSlotWidth`'s `cqw` cap and the Type slot's gate read: the list's own
       width, not the window's. */
    <Box sx={besideDetail ? [CREDENTIALS_LIST_CONTAINER_SX, NAME_FLOOR_SX] : CREDENTIALS_LIST_CONTAINER_SX}>
      {/* ADD CREDENTIAL, on the Directory strip via a titleless header (`components/TabActionsSlot`).
          Ungated: `POST /api/credentials` admits every session to create its OWN row (`own_credential`). */}
      <PageHeader
        actions={<NewButton label="Add secret" to="/credentials/new" sx={tapTargetSx} />}
      />

      {/* The filter strip (R2.1), above the data it narrows; it holds no state of its own. */}
      <CredentialFilterStrip
        view={view}
        onChange={patchView}
        types={credentialTypes.types}
        typesError={credentialTypes.error}
        typesLoading={credentialTypes.loading}
        rosters={rosters}
        showTest={showTest}
      />

      {error && <Banner severity="error">{error}</Banner>}

      {/* Never over rows a new read is replacing: "Select all N matching" would pair the new filter with the old N. */}
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
          loading={skeleton}
          skeleton={{ rows: PAGE_SKELETON_ROWS }}
          empty={items.length === 0}
          noMatch={hasActiveCredentialFilters(appliedView) && <NoMatchesState onClear={clearFilters} />}
          emptyTitle="No secrets yet"
          emptyScoped
          feed={{
            ariaLabel: 'Secrets',
            testId: 'credentials-list',
            sections: [{ key: 'credentials', rows: flatRows }],
            columns: flatColumns,
            sort: headerSortState,
            onSort: key => patchView({ sort: headerSort(view.sort, key) }),
          }}
        />
      )}

      {/* The footer is Flat mode's alone (R3.4). Its caption is the roster's one count: rows loaded of the wire
          `total` (`N+` when capped), never below the rows on screen (rows deleted since can leave it behind). */}
      {!treeMode && !skeleton && items.length > 0 && (
        <LoadMoreFooter
          hasMore={paging.hasMore}
          loading={loading}
          onMore={() => void paging.loadMore()}
          testId="credentials-load-more"
          caption={`${items.length.toLocaleString()} of ${Math.max(total, items.length).toLocaleString()}${totalCapped ? '+' : ''}`}
          sx={{ mt: 1 }}
        />
      )}

      {confirmDialog}
      {changeOrg.dialog}
    </Box>
  )
}
