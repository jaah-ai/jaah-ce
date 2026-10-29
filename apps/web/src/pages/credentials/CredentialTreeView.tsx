/* THE ROSTER'S TREE MODE, LOADED A LEVEL AT A TIME. The server lists one level per request
 * (`GET /api/credentials/tree`, `routes/credentials/tree.py`): an operator's tenants, then one org's homes (org
 * shared, system, project, personal — a tenant's root), then a home's folders with the credentials directly in each.
 * Nothing beneath a closed node is fetched; a node opens by fetching its children, and every level pages with
 * "Load more" on the server's cursor. Counts are the server's (every filter applied), capped at 1,000.
 *
 * WHICH NODES ARE OPEN is the persisted `view.expanded` set (`lib/credentialFilterStorage.ts`), keyed by `nodeKey`:
 * a stored key whose node no longer exists is dropped silently once its parent's listing is whole and unfiltered.
 *
 * RE-FILING BY DRAGGING a credential renames it (`PATCH /api/credentials/{id}`, the page's `onRefile`), onto a folder
 * or the top of its own home — never another home. A FOLDER is not draggable here: its move must name every
 * credential beneath it (the bulk move, `POST /api/credentials/bulk/move-folder`), which a lazily loaded folder
 * does not hold. */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { apiDetail, ApiError } from '../../api'
import { credentialFeedRow, credentialTypeSlotWidth, type CredentialRowDeps } from '../../lib/credentialRow'
import { useOrgScope } from '../../lib/orgScope'
import { nameSegments, NAME_PATH_SEPARATOR } from '../../lib/nameTree'
import { isNameDraggable, movedName, nameParentPath } from '../../lib/nameTreeMove'
import { NAME_GROUP_GLYPH } from '../../lib/nameTreeRows'
import { usePointerDrag } from '../../hooks/usePointerDrag'
import DragChip from '../../ui/DragChip'
import FeedList, { feedDropKeyAt } from '../../ui/feed/FeedList'
import type { FeedDrag, FeedRowSpec } from '../../ui/feed/types'
import LoadingState from '../../ui/LoadingState'
import Banner from '../../ui/Banner'
import {
  listCredentialTree,
  type Credential,
  type CredentialRosterFilters,
  type CredentialTreeCount,
  type CredentialTreeLevel,
  type CredentialTreePlace,
} from './api'
import { homeExpansionKey } from './credentialTree'

/** Skeleton rows while the root loads, as Flat mode's. */
const ROOT_SKELETON_ROWS = 5

/** The root level's cache key; every other node's is `nodeKey`'s. Never `''`: the fetch effect keys on the joined
 *  wanted keys, and a lone `''` would read as "nothing wanted", so a reset would never refetch the root. */
const ROOT = 'root'

/* ── NODE KEYS: one string per node, the cache's key and the persisted expansion set's ─────────────────
 * A tenant `org:<id>`, a home `home:<home key>`, a folder its home's key, `␟`, then its path
 * (`./credentialTree.ts::homeExpansionKey`). Home keys (`o:…`, `p:…`) never hold `␟`, paths never start with it. */
const tenantKey = (orgId: string) => `org:${orgId}`
const homeKey = (home: string) => `home:${home}`
const folderKey = (home: string, path: string) => homeExpansionKey(home, path)

/** Is `key` one this view mints? A stored key from the 200-row tree (a home LABEL, a bare path) is not. */
function isNodeKey(key: string): boolean {
  return key.startsWith('org:') || key.startsWith('home:') || /^[ospu]:[^\u001f]+\u001f./.test(key)
}

/** `1,000+` past the server's cap. */
export function treeCountText({ count, count_capped }: CredentialTreeCount): string {
  return `${count.toLocaleString()}${count_capped ? '+' : ''}`
}

type LevelState = {
  status: 'loading' | 'ready' | 'error'
  error?: string
  data: CredentialTreeLevel
  loadingMore?: boolean
  /** The filters it was read under (`filterKey`): a listing from before a filter change proves nothing now. */
  asked?: string
}

const EMPTY_LEVEL: CredentialTreeLevel = { tenants: [], homes: [], folders: [], leaves: [], next_cursor: null }

/** What a leaf drag carries, and where it may land: its own home, a folder there or the top (`path` null). */
type LeafDrag = { row: Credential; home: string }
const dropKey = (home: string, path: string | null) => `${home}\u001f${path ?? ''}`

export type CredentialTreeViewProps = {
  filters: CredentialRosterFilters
  /** Does a filter narrow the roster? Then a node missing from a listing may be filtered out, not gone. */
  filtered: boolean
  /** The operator plane reads tenants first (`ui/OrgIndicator.tsx::useOrgVisible`); everyone else, homes. */
  operator: boolean
  /** The org whose homes are the root when the server reads this viewer as the operator but the page draws no
   *  tenants: the community edition's one org (the server asks the operator for an `org_id`). */
  rootOrgId?: string
  expanded: readonly string[]
  onToggle: (key: string) => void
  /** Replace the persisted set (the silent drop of vanished nodes, a reveal after a re-file). */
  onExpanded: (next: string[]) => void
  rowDeps: Omit<CredentialRowDeps, 'busy'>
  busyId: string | null
  canDrag: boolean
  /** Rename `row` into `destPath` of its home (`null`: its top); rejects with the server's refusal. */
  onRefile: (row: Credential, name: string) => Promise<void>
  /** Bumped by the page after a write it made (a delete, an editor closing): every open level reloads. */
  reloadToken: number
  /** The root holds nothing: the page's own empty state (filtered or not). */
  empty: ReactNode
}

export default function CredentialTreeView(props: CredentialTreeViewProps) {
  const { filters, filtered, operator, rootOrgId, expanded, onToggle, onExpanded, rowDeps, busyId, reloadToken } = props
  const [levels, setLevels] = useState<Record<string, LevelState>>({})
  const [error, setError] = useState('')
  /* The tenant level 422s for an operator acting as one org (the API binds it to that org): its root is then that
     org's homes, as a tenant's. */
  const [rootLevel, setRootLevel] = useState<'tenants' | 'homes'>(operator ? 'tenants' : 'homes')
  const filterKey = JSON.stringify([
    filters.type, filters.q, filters.mine, filters.ownerIds ?? [], filters.secrets, filters.health,
  ])
  // Each new filter set asks for tenants again: the fallback answers the refusal it met, not every later read.
  useEffect(() => { setRootLevel(operator ? 'tenants' : 'homes') }, [operator, filterKey])
  /* The console's global org filter narrows every level too (`lib/orgScope.ts`), so with it on a listing proves no
     node gone; neither does an operator's homes root, which is one org's of many. */
  const orgNarrowed = useOrgScope() !== null
  const provesAbsence = !filtered && !orgNarrowed && !(operator && rootLevel === 'homes')
  /* Every request carries the generation it was asked under: a filter change or a reload starts a new one, and
     an answer from an older one is dropped (`ui/AsyncLookup`'s discipline). */
  const generation = useRef(0)
  const places = useRef(new Map<string, CredentialTreePlace>())

  useEffect(() => {
    generation.current += 1
    setLevels({})
    setError('')
  }, [filterKey, rootLevel])

  /* `keep`: a re-read after a write (`reloadToken`) refetches a level in place, its rows on screen until the answer
     lands, so a delete three folders down does not blank the tree. */
  const fetchLevel = useCallback((key: string, place: CredentialTreePlace, cursor?: string | null, keep = false) => {
    places.current.set(key, place)
    const asked = generation.current
    if (!keep) {
      setLevels(current => ({
        ...current,
        [key]: cursor
          ? { ...current[key], loadingMore: true }
          : { status: 'loading', data: EMPTY_LEVEL },
      }))
    }
    listCredentialTree(place, filters, { cursor }).then(
      data => {
        if (generation.current !== asked) return
        setLevels(current => {
          const before = current[key]?.data ?? EMPTY_LEVEL
          const merged = cursor
            ? {
                tenants: [...before.tenants, ...data.tenants],
                homes: [...before.homes, ...data.homes],
                folders: [...before.folders, ...data.folders],
                leaves: [...before.leaves, ...data.leaves],
                next_cursor: data.next_cursor,
              }
            : data
          return { ...current, [key]: { status: 'ready', data: merged, asked: filterKey } }
        })
        if (cursor) setError('')
      },
      err => {
        if (generation.current !== asked) return
        // Only the plane's own refusal (`tree.py`: an operator acting as one org); a bad filter's 422 is an error.
        if (
          key === ROOT && rootLevel === 'tenants' && err instanceof ApiError && err.status === 422
          && err.message.startsWith('level=tenants')
        ) {
          setRootLevel('homes')
          return
        }
        const detail = apiDetail(err, 'Failed to load secrets')
        if (cursor) {
          setLevels(current => ({ ...current, [key]: { ...current[key], loadingMore: false } }))
          setError(detail)
        } else {
          setLevels(current => ({ ...current, [key]: { status: 'error', error: detail, data: EMPTY_LEVEL } }))
        }
      },
    )
    // `filters` rides `filterKey`, which resets the cache: a fetch never outlives the filters it was asked under.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterKey, rootLevel])

  /* The re-read after a write: a new generation, every loaded level refetched in place. Not on mount. */
  const levelsRef = useRef(levels)
  levelsRef.current = levels
  const lastReload = useRef(reloadToken)
  useEffect(() => {
    if (lastReload.current === reloadToken) return
    lastReload.current = reloadToken
    generation.current += 1
    for (const key of Object.keys(levelsRef.current)) {
      const place = places.current.get(key)
      if (place) fetchLevel(key, place, null, true)
    }
  }, [reloadToken, fetchLevel])

  /* The drop's reveal reads the set as it is when the rename lands, not as it was at the drop. */
  const expandedRef = useRef(expanded)
  expandedRef.current = expanded
  const open = useMemo(() => new Set(expanded), [expanded])
  /* The Type column sized over the leaves this tree has loaded, as Flat sizes it over its page. */
  const typeWidth = useMemo(
    () => credentialTypeSlotWidth(Object.values(levels).flatMap(level => level.data.leaves), rowDeps.besideDetail),
    [levels, rowDeps.besideDetail],
  )
  const rootPlace: CredentialTreePlace = rootLevel === 'tenants' ? { level: 'tenants' }
    : rootOrgId ? { level: 'homes', orgId: rootOrgId } : { level: 'homes' }

  /* ── Rows. Building them also names the open nodes not yet loaded (`wanted`), fetched after the render. ── */
  const wanted: [string, CredentialTreePlace][] = []
  if (!levels[ROOT]) wanted.push([ROOT, rootPlace])

  const drag = usePointerDrag<LeafDrag, string>({
    resolve: feedDropKeyAt,
    onDrop: (item, key) => {
      const sep = key.indexOf('\u001f')
      const path = key.slice(sep + 1)
      const name = movedName(item.row.name, path === '' ? null : path)
      if (name === null) return
      props.onRefile(item.row, name).then(
        () => {
          setError('')
          if (path !== '') onExpanded(revealed(expandedRef.current, item.home, path))
        },
        err => setError(apiDetail(err, 'Failed to move secret')),
      )
    },
  })
  const lifted = drag.item

  /** A destination's drag props during a leaf drag: its own home only, and never the folder it already sits in. */
  function dropProps(home: string, path: string | null): FeedDrag | undefined {
    if (!lifted) return undefined
    if (lifted.home !== home) return { dragging: true }
    // Folder paths are stored lowercase (`parent_path`); a name from before names were lowercased may not be.
    const here = nameParentPath(lifted.row.name).toLowerCase()
    if ((path ?? '') === here) return { dragging: true, drop: 'reject', dropReason: 'Already in this folder' }
    const key = dropKey(home, path)
    return { dragging: true, drop: drag.target === key ? 'over' : 'accept', dropKey: key }
  }

  function childRows(key: string, place: CredentialTreePlace, build: (data: CredentialTreeLevel) => FeedRowSpec[]): FeedRowSpec[] {
    const level = levels[key]
    if (!level) {
      wanted.push([key, place])
      return []
    }
    if (level.status === 'loading') return []
    if (level.status === 'error') return [errorRow(key, level.error ?? '', () => fetchLevel(key, place))]
    const rows = build(level.data)
    if (level.data.next_cursor) {
      rows.push(loadMoreRow(key, level.loadingMore === true, () => fetchLevel(key, place, level.data.next_cursor)))
    }
    return rows
  }

  function nodeRow(key: string, label: string, counted: CredentialTreeCount, children: () => FeedRowSpec[], drop?: FeedDrag): FeedRowSpec {
    const isOpen = open.has(key)
    const count = treeCountText(counted)
    const noun = counted.count === 1 && !counted.count_capped ? 'secret' : 'secrets'
    return {
      key: `node:${key}`,
      indicator: { tone: 'neutral', glyph: NAME_GROUP_GLYPH, label: 'secret group' },
      // The whole name, wrapped: a folder is told apart by its name alone.
      title: <Box component="span" data-testid="credential-group-name" sx={{ overflowWrap: 'anywhere' }}>{label}</Box>,
      titleText: label,
      titleLines: 'all',
      ariaLabel: `secret group ${label}, ${count} ${noun}`,
      disclosure: {
        expanded: isOpen,
        busy: isOpen && levels[key]?.status === 'loading',
        onToggle: () => onToggle(key),
        label: 'secrets',
      },
      slots: [{
        key: 'count', width: 72, align: 'right', node: count, fontSize: '13px', testId: 'credential-group-count',
        title: `${count} ${noun}`,
      }],
      children: isOpen ? children() : [],
      data: { 'credential-group': key, 'credential-group-count': count },
      drag: drop ?? (lifted ? { dragging: true } : undefined),
    }
  }

  function leafRow(row: Credential, home: string): FeedRowSpec {
    const flat = credentialFeedRow(row, { ...rowDeps, typeWidth, busy: busyId === row.id })
    const segments = nameSegments(row.name)
    const editable = row.editable ?? rowDeps.canEdit
    const liftable = props.canDrag && editable && busyId === null && isNameDraggable(row)
    const onLift = liftable ? (event: Parameters<NonNullable<FeedDrag['onLift']>>[0]) => drag.lift({ row, home }, event) : undefined
    return {
      ...flat,
      // The last segment: the folders above already spell the rest. The full name stays in `titleText` and the label.
      title: segments.length > 0 ? segments[segments.length - 1] : row.name,
      ariaLabel: [row.name, ...(flat.markers ?? []).map(marker => marker.label), flat.indicator.label].filter(Boolean).join(', '),
      drag: lifted ? { dragging: true, onLift, lifted: lifted.row.id === row.id } : onLift ? { onLift } : undefined,
    }
  }

  function folderRows(home: string, path: string, data: CredentialTreeLevel): FeedRowSpec[] {
    const folders = data.folders.map(folder => {
      const child = path === '' ? folder.name : `${path}${NAME_PATH_SEPARATOR}${folder.name}`
      const key = folderKey(home, child)
      return nodeRow(key, folder.name, folder, () => childRows(key, { level: 'folder', home, path: child }, d => folderRows(home, child, d)), dropProps(home, child))
    })
    const spacer = folders.length > 0
    const leaves = data.leaves.map(row => ({ ...leafRow(row, home), disclosure: spacer ? ('spacer' as const) : undefined }))
    return [...folders, ...leaves]
  }

  function homeRows(data: CredentialTreeLevel): FeedRowSpec[] {
    return data.homes.map(home => {
      const key = homeKey(home.key)
      // The org's shared home is named by its org, which the tenant row above (or the whole console) already says.
      return nodeRow(key, home.kind === 'org' ? 'Shared' : home.label, home, () => childRows(key, { level: 'folder', home: home.key, path: '' }, d => folderRows(home.key, '', d)), dropProps(home.key, null))
    })
  }

  function tenantRows(data: CredentialTreeLevel): FeedRowSpec[] {
    return data.tenants.map(tenant => {
      const key = tenantKey(tenant.org_id)
      return nodeRow(key, tenant.name, tenant, () => childRows(key, { level: 'homes', orgId: tenant.org_id }, homeRows))
    })
  }

  const root = levels[ROOT]
  const rows = root?.status === 'ready'
    ? childRows(ROOT, rootPlace, rootLevel === 'tenants' ? tenantRows : homeRows)
    : []
  const wantedKey = wanted.map(([key]) => key).join('\n')

  useEffect(() => {
    for (const [key, place] of wanted) fetchLevel(key, place)
    // `wanted` is rebuilt every render; its keys are what changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wantedKey, fetchLevel])

  /* THE SILENT DROP (AC-5.4): a stored key this view never mints, or an open child missing from a whole,
     unfiltered listing, leaves the persisted set. A filtered listing proves nothing: the node may be filtered out. */
  useEffect(() => {
    const stale = new Set(expanded.filter(key => !isNodeKey(key)))
    if (provesAbsence) {
      for (const [key, level] of Object.entries(levels)) {
        if (level.status !== 'ready' || level.data.next_cursor || level.asked !== filterKey) continue
        const place = places.current.get(key)
        if (!place) continue
        for (const child of expanded) {
          if (isChildOf(child, place, level.data) === false) stale.add(child)
        }
      }
    }
    if (stale.size > 0) onExpanded(expanded.filter(key => !stale.has(key)))
  }, [levels, expanded, provesAbsence, filterKey, onExpanded])

  if (!root || root.status === 'loading') return <LoadingState rows={ROOT_SKELETON_ROWS} />
  if (root.status === 'error') {
    const rows = [errorRow(ROOT, root.error ?? '', () => fetchLevel(ROOT, rootPlace))]
    return <FeedList ariaLabel="Secrets" testId="credentials-list" sections={[{ key: 'credentials', rows }]} />
  }
  const top = root.data
  // A short page may still carry a cursor (`tree.py`'s dense branch): then it is not empty, it has more.
  if (top.tenants.length + top.homes.length === 0 && !top.next_cursor) return <>{props.empty}</>
  return (
    <>
      {error && <Banner severity="error">{error}</Banner>}
      <FeedList ariaLabel="Secrets" testId="credentials-list" sections={[{ key: 'credentials', rows }]} />
      {lifted && <DragChip label={lifted.row.name} />}
    </>
  )
}

/** Is `key` a direct child of the node listed at `place`, and is it in `data`? `null`: not that node's child at all. */
function isChildOf(key: string, place: CredentialTreePlace, data: CredentialTreeLevel): boolean | null {
  if (place.level === 'tenants') {
    if (!key.startsWith('org:')) return null
    return data.tenants.some(tenant => tenantKey(tenant.org_id) === key)
  }
  if (place.level === 'homes') {
    // An operator's homes are listed per org, and a home key does not say which: only a tenant's root can tell.
    if (!key.startsWith('home:') || place.orgId) return null
    return data.homes.some(home => homeKey(home.key) === key)
  }
  const head = place.path === '' ? folderKey(place.home, '') : `${folderKey(place.home, place.path)}${NAME_PATH_SEPARATOR}`
  if (!key.startsWith(head)) return null
  const rest = key.slice(head.length)
  if (rest === '' || rest.includes(NAME_PATH_SEPARATOR)) return null
  return data.folders.some(folder => folder.name === rest)
}

/** `expanded` with the home and every folder down to `path` open, so a re-filed credential lands visible. */
function revealed(expanded: readonly string[], home: string, path: string): string[] {
  const next = new Set(expanded)
  next.add(homeKey(home))
  const segments = path.split(NAME_PATH_SEPARATOR)
  for (let depth = 1; depth <= segments.length; depth += 1) {
    next.add(folderKey(home, segments.slice(0, depth).join(NAME_PATH_SEPARATOR)))
  }
  return [...next]
}

function loadMoreRow(key: string, busy: boolean, onLoad: () => void): FeedRowSpec {
  return {
    key: `more:${key}`,
    disclosure: 'spacer',
    title: (
      <Button size="small" onClick={onLoad} disabled={busy} data-testid="credential-tree-more">
        {busy ? 'Loading…' : 'Load more'}
      </Button>
    ),
    titleText: 'Load more',
  }
}

function errorRow(key: string, message: string, onRetry: () => void): FeedRowSpec {
  return {
    key: `error:${key}`,
    disclosure: 'spacer',
    indicator: { tone: 'error', glyph: '!', label: 'not loaded' },
    title: (
      <Box component="span" sx={{ display: 'inline-flex', alignItems: 'center', gap: 1 }}>
        <span>{message}</span>
        <Button size="small" onClick={onRetry}>Retry</Button>
      </Box>
    ),
    titleText: message,
  }
}
