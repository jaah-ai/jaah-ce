import { useCallback, useMemo, useRef, useState, type JSX } from 'react'
import Box from '@mui/material/Box'
import type { Theme } from '@mui/material/styles'
import Button from '@mui/material/Button'
import useMediaQuery from '@mui/material/useMediaQuery'
import { useLocation, useNavigate } from 'react-router-dom'
import { get } from '../../api'
import { useApiRead } from '../../hooks/useApiRead'
import { PER_PAGE } from '../../hooks/usePagedLogFilters'
import { compactAge } from '../../lib/relativeTime'
import { toggled } from '../../lib/sets'
import { localInputToIso, viewerZone, formatZoned } from '../../lib/zonedTime'
import Banner from '../../ui/Banner'
import EmptyState from '../../ui/EmptyState'
import NoMatchesState from '../../ui/NoMatchesState'
import { AGE_SLOT } from '../../ui/feed/geometry'
import type { FeedNote, FeedRowSpec, FeedSlot } from '../../ui/feed/types'
import StatusChip from '../../ui/StatusChip'
import {
  AUDIT_FOLD_FILTERS, AUDIT_LIST_CONTAINER_SX, AUDIT_NO_ACTOR, FEED_NO_ID, PHONE_QUERY, tapTargetSx,
} from '../../ui/listStyles'
import { StaleResultBanner } from '../../ui/LogPagerFooter'
import { Pager } from '../../ui/Pager'
import { useAuth } from '../../auth'
import { useOpenDetailPath } from '../../components/ListDetail'
import { isOpenPath } from '../../lib/listDetail'
import { AUDIT_TAB_PATHS, normalizePathname } from '../../routeGroups'
import SegmentedControl from '../../ui/SegmentedControl'
import { RadioPage } from '../../ui/ToolbarMenu'
import ActivitySearch, { type ActivityChip } from './ActivitySearch'
import {
  apiParams,
  chipLabel,
  defaultActionType,
  eventCount,
  readFilters,
  writeFilters,
  type ActionType,
  type ActivityFilters,
  type ChipFacet,
} from './activityFilters'
import { auditEventPath } from './AuditEventPage'
import ListStates from '../../ui/feed/ListStates'
import PagedLogView from './PagedLogView'
import {
  actionText,
  actorLabel,
  actorName,
  actorText,
  isMachineRow,
  isSignInRefusal,
  isUnattributed,
  rowKey,
  repeatSentence,
  rowSentence,
  namesSubject,
  signInAction,
  signInSentence,
  subjectText,
  SYSTEM_ACTOR_LABEL,
  taskReference,
  toLines,
  trailSteps,
  UNATTRIBUTED_LABEL,
  viaApiKey,
  type AuditLine,
} from './rows'
import { statusGlyph, statusTone } from './status'
import type { AuditOrg, AuditPage, AuditRow } from './types'

/* Audit › Activity (tab id `users`, address `/audit`) — the searchable activity log: "what changed
 * recently, and who changed it", built for 10M events.
 * Feed row: the sentence is the title, `status` the state disc, Action and Actor gated slots, When ungated;
 * the operator also gets an Organization slot. `Type` is dropped (it restates the filter); `subject`
 * is a note on the row's hover when the sentence does not already say it. On phones the title wraps, so the
 * sentence reads once, whole.
 * FILTERS LIVE IN THE URL (`./activityFilters.ts`): a pasted link
 * restores the view, Back undoes a chip. Every change pushes one; free text commits only on Enter.
 * KEYSET PAGING: a local stack of cursors (Newer pops, Older pushes), RESET by any filter change (so
 * changing a filter and changing it back starts at the newest page). Newer/Older are off while a read is in
 * flight, and Older only follows a `next_cursor` from a response for the current filters — the hook keeps
 * the previous response on screen while the next loads, and its cursor belongs to that query. No page
 * numbers and no ceiling: `total` is exact, the planner's estimate ("About 2.3M events") or a floor
 * ("10,000+ events"), never below the rows already paged through.
 * No live tail: a backward-looking reader should not have rows reorder under the cursor. */

/** Skeleton rows: the height a populated list settles at. */
const SKELETON_ROWS = 10

const ACTION_TYPES: readonly { value: ActionType; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'read', label: 'Views' },
  { value: 'write', label: 'Changes' },
]

/* Trailing cluster widths in px, measured against the widest real values (an email; an action label).
 * Gate order (`ui/feed/geometry.ts::SlotGate`): the operator's org drops first (`auditOrg`, ≤1365px —
 * priced in `ui/listStyles.ts::FEED_NO_AUDIT_ORG`), action next (`auditAction`, ≤899px — the sentence restates
 * it), actor third (`auditActor`, ≤767px — `ActorMark` carries it before the sentence), When never. Each also drops by the list's own
 * width (`AUDIT_NO_*`), so beside an open event the sentence keeps its room.
 * The age slot is `AGE_SLOT` with `compactAge` (bounded at four chars); verbose `relativeAge` clipped
 * every row at 44px. The instant is the slot's `title`. */
const ORG_SLOT = 130
const ACTION_SLOT = 150
const ACTOR_SLOT = 170
/* A keyed request's actor cell, one line: the key (body font, up to three quarters of the slot) before the address, each
 * ellipsising on its own. */
const ACTOR_KEYED_SX = { display: 'flex', gap: 0.75, minWidth: 0 } as const
const ACTOR_KEY_SX = (theme: Theme) => ({
  flex: '0 1 auto', maxWidth: '75%', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis',
  fontFamily: theme.typography.fontFamily, color: 'text.primary',
})
const ACTOR_ADDRESS_SX = { flex: '1 1 0', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' } as const
/** A folded line's slots that name who and whose: taken from the record the line opens, not its lead. */
const OPENS_SLOT_KEYS: ReadonlySet<string> = new Set(['actor', 'org'])

/* At phone width the title wraps (`titleLines: 'all'`) — an owner decision, and this list's only departure
 * from a one-line row. Its title is prose whose key half is at the end, so clipping deletes it.
 * jsdom's `matchMedia` makes `onPhone` false; `e2e/audit-users.spec.ts` grades the desktop sentence. */

/** The 4xx band's floor: a refused row's tone and glyph, for a record that carries no status of its own. */
const REFUSED_BAND = 400

const NO_CURSORS: readonly string[] = []

/* WHO DID IT, WHERE THE ACTOR COLUMN IS GONE: beside an open event or on a phone the `auditActor` slot
 * drops, and nothing on the row said who acted. A 16px mark before the sentence keeps it scannable down the list: a
 * person's initial, or ⚙ for the system. Shown exactly where the slot is hidden (the gate's two queries), absent
 * elsewhere. The letter is CSS content, so the sentence's text stays the sentence; the name is its `title` and
 * accessible name. */
const ACTOR_MARK_SX = {
  display: 'none',
  [FEED_NO_ID]: { display: 'inline-flex' },
  [AUDIT_NO_ACTOR]: { display: 'inline-flex' },
  alignItems: 'center',
  justifyContent: 'center',
  flex: '0 0 auto',
  width: 16,
  height: 16,
  mr: 0.75,
  borderRadius: '50%',
  bgcolor: 'action.selected',
  color: 'text.secondary',
  fontSize: '0.625rem',
  fontWeight: 600,
  lineHeight: 1,
  verticalAlign: 'text-bottom',
  '&::before': { content: 'attr(data-mark)' },
} as const

function ActorMark({ actor, system }: { actor: string; system: boolean }): JSX.Element {
  return (
    <Box
      component="span"
      role="img"
      aria-label={`by ${actor}`}
      title={actor}
      data-testid="audit-actor-mark"
      data-mark={system ? '⚙' : (actor.trim()[0] ?? '?').toUpperCase()}
      sx={ACTOR_MARK_SX}
    />
  )
}

export default function UsersTab(): JSX.Element {
  const zone = useMemo(viewerZone, [])
  /* The band as a value: the title wraps below it. */
  const onPhone = useMediaQuery(PHONE_QUERY)
  const { admin } = useAuth()
  const operator = admin?.is_operator === true
  const defaultType = defaultActionType(admin?.org_hipaa)

  const navigate = useNavigate()
  const location = useLocation()
  /* The URL is this tab's only while it is the address: `AuditPage` keeps the panel mounted (hidden)
     under the other tabs, whose own queries must not refilter it. Frozen there, it fetches nothing.
     Behind an open event pane the list's location is its own (`components/ListDetail.tsx`). */
  const active = normalizePathname(location.pathname) === AUDIT_TAB_PATHS.users
  const frozenSearch = useRef(location.search)
  if (active) frozenSearch.current = location.search
  const search = frozenSearch.current
  const filters = useMemo(
    () => readFilters(new URLSearchParams(search), { type: defaultType, operator }),
    [search, defaultType, operator],
  )

  /* The request without its cursor: the identity the cursor stack belongs to. */
  const base = useMemo(() => apiParams(filters, PER_PAGE).toString(), [filters])
  const [cursors, setCursors] = useState<{ base: string; stack: readonly string[] }>({ base, stack: NO_CURSORS })
  /* A filter change RESETS the stack (state, not a mask: masking it let changing a filter and changing it
     back restore the old depth). Page 3 of the old query is not page 3 of the new. Set during render, so no
     frame requests the old cursor under the new filters. */
  if (cursors.base !== base) setCursors({ base, stack: NO_CURSORS })
  const stack = cursors.base === base ? cursors.stack : NO_CURSORS
  const cursor = stack.length > 0 ? stack[stack.length - 1] : null
  const url = `/api/audit?${base}${cursor === null ? '' : `&cursor=${encodeURIComponent(cursor)}`}`

  /* Superseded responses are fenced by the hook. Each response carries the request it answers: the hook
     keeps the last one on screen while the next is in flight, so `data` alone cannot say whose it is. */
  const depth = stack.length
  const { data: read, loading, error, reload } = useApiRead(
    ['audit-user-activity', url, depth],
    () => get<AuditPage>(url).then(page => ({ page, url, depth })),
    'Failed to load the activity log',
  )
  const data = read?.page ?? null
  /* The shown page answers THIS request: only then is its `next_cursor` this query's next page. */
  const current = read !== null && read.url === url && !loading

  /* Labels a pick carried, so a chip reads as a name before (and after) the next response names it. */
  const [picked, setPicked] = useState<ReadonlyMap<string, string>>(() => new Map())
  /* Bumped by Clear filters, so the box drops typed-but-uncommitted text. */
  const [searchReset, setSearchReset] = useState(0)

  /** Writes the view into the URL. Pushes, except a From/To edit (`replace`): a datetime input changes per
   *  segment, and Back should not walk through half-typed dates. */
  const setFilters = useCallback((next: ActivityFilters, { replace = false }: { replace?: boolean } = {}) => {
    const query = writeFilters(next, defaultType).toString()
    navigate({ pathname: AUDIT_TAB_PATHS.users, search: query ? `?${query}` : '' }, { replace })
  }, [navigate, defaultType])

  const update = (change: Partial<ActivityFilters>, options?: { replace?: boolean }) =>
    setFilters({ ...filters, ...change }, options)

  const chips: ActivityChip[] = useMemo(() => {
    const served = data?.filter_labels
    const chip = (facet: ChipFacet, value: string): ActivityChip => ({
      facet, value, label: chipLabel(facet, value, picked, served),
    })
    return [
      ...(filters.q ? [chip('q', filters.q)] : []),
      ...filters.users.map(v => chip('user', v)),
      ...filters.orgs.map(v => chip('org', v)),
      ...(filters.result ? [chip('result', filters.result)] : []),
      ...(filters.record ? [chip('record', filters.record)] : []),
      ...(filters.session ? [chip('session', filters.session)] : []),
    ]
  }, [filters, picked, data])

  function addChip(chip: ActivityChip) {
    setPicked(prev => new Map(prev).set(`${chip.facet}:${chip.value}`, chip.label))
    /* A committed search stays: the text that found the chip was a lookup and never became `q`. */
    const change: Partial<ActivityFilters> = {}
    if (chip.facet === 'user') change.users = [...filters.users.filter(v => v !== chip.value), chip.value]
    if (chip.facet === 'org') change.orgs = [...filters.orgs.filter(v => v !== chip.value), chip.value]
    if (chip.facet === 'result') change.result = chip.value
    if (chip.facet === 'record') change.record = chip.value
    if (chip.facet === 'session') change.session = chip.value
    update(change)
  }

  function removeChip(chip: ActivityChip) {
    if (chip.facet === 'q') update({ q: '' })
    if (chip.facet === 'user') update({ users: filters.users.filter(v => v !== chip.value) })
    if (chip.facet === 'org') update({ orgs: filters.orgs.filter(v => v !== chip.value) })
    if (chip.facet === 'result') update({ result: '' })
    if (chip.facet === 'record') update({ record: '' })
    if (chip.facet === 'session') update({ session: '' })
  }

  const items = data?.items ?? []
  const lines = useMemo(() => toLines(items), [items])

  const hasFilters = writeFilters(filters, defaultType).toString() !== ''

  function clearFilters() {
    setSearchReset(n => n + 1)
    navigate({ pathname: AUDIT_TAB_PATHS.users, search: '' })
  }

  /* The event open in the pane beside this feed, so its row reads as current. */
  const openPath = useOpenDetailPath()
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set())

  const toggleExpanded = (key: string) => setExpanded(prev => toggled(prev, key))

  function older() {
    const next = current ? read.page.next_cursor : null
    if (!next) return
    setCursors({ base, stack: [...stack, next] })
  }

  function newer() {
    if (loading) return
    setCursors({ base, stack: stack.slice(0, -1) })
  }

  function newest() {
    setCursors({ base, stack: NO_CURSORS })
  }

  /** The operator's Organization slot — absent for a tenant, so their row is unchanged. */
  function orgSlot(org: AuditOrg | null, unattributed = false): FeedSlot[] {
    if (!operator) return []
    /* Once per row: when the actor slot already reads Unattributed, the org slot is a muted dash (its hover keeps the word). */
    const shown = org?.name ?? (unattributed ? '—' : UNATTRIBUTED_LABEL)
    return [{
      key: 'org',
      width: ORG_SLOT,
      gate: 'auditOrg',
      align: 'left',
      testId: 'audit-org',
      /* The operator is served every row's org; none means the record belongs to no org — a refused sign-in for an
         address no account holds — so it says so rather than leaving a blank. */
      title: org?.name ?? UNATTRIBUTED_LABEL,
      dim: org === null,
      node: shown,
    }]
  }

  /** The actor slot's hover text; the operator's also names the org, which drops first. */
  function actorTitle(actor: string, org: AuditOrg | null): string {
    return operator && org ? `${actor} · ${org.name}` : actor
  }

  /** A machine row reads as a chip, a human as their address, so the two rows for one mutation differ. A request
   *  made with an API key leads with the key, so a cut address never hides it; the row stays one line. */
  function actorNode(row: AuditRow): JSX.Element | string {
    if (isMachineRow(row)) {
      return <StatusChip label={SYSTEM_ACTOR_LABEL} tone="neutral" glyph="⚙" ariaLabel="System actor" />
    }
    const key = viaApiKey(row)
    if (!key) return actorText(row)
    return (
      <Box component="span" sx={ACTOR_KEYED_SX}>
        <Box component="span" data-testid="audit-actor-key" sx={ACTOR_KEY_SX}>Key {key}</Box>{' '}
        <Box component="span" sx={ACTOR_ADDRESS_SX}>{actorName(row)}</Box>
      </Box>
    )
  }

  /** A row's disc: its HTTP status; a refused sign-in's record has none, so it borrows its request's refused tone. */
  function rowIndicator(row: AuditRow): FeedRowSpec['indicator'] {
    if (row.status === null && isSignInRefusal(row)) {
      return { tone: statusTone(REFUSED_BAND), glyph: statusGlyph(REFUSED_BAND), label: 'Sign-in refused', data: { status: 'refused' } }
    }
    return {
      tone: row.status !== null ? statusTone(row.status) : 'neutral',
      glyph: statusGlyph(row.status),
      label: row.status !== null ? `HTTP ${row.status}` : 'no recorded status',
      data: { status: row.status ?? 'none' },
    }
  }

  /** One row (or an expanded child) as a feed row. No `ariaLabel`: it would make the disc decorative
   *  and replace status, action and actor with one string. The event opens (`AuditEventPage`, in the
   *  list/detail pane) by row click (pointer-only in `FeedList`) and by the ⋯ item, the keyboard path. The
   *  row rides along as state, so the page shows exactly what was clicked without a read. */
  function auditRowSpec(row: AuditRow, options: { child?: boolean; nextTitle?: string } = {}): FeedRowSpec {
    /* A child leads with its own event (the fold above names the task); its subject is still its note. */
    const primary = options.child ? row.label : rowSentence(row)
    /* Subject as a note only when it adds to the sentence (`namesSubject`: a whole token, so `task #5` is not
       found in "task #52") and the row under it does not already name it: a request is followed by its
       transition, whose title is the subject, and the pair printed it twice. */
    const subject = row.subject && !namesSubject(primary, row.subject) && !namesSubject(options.nextTitle ?? '', row.subject)
      ? subjectText(row.subject)
      : null
    const notes: FeedNote[] = subject ? [{ key: 'subject', text: subject, tone: 'muted' }] : []
    const path = auditEventPath(row.id)
    const current = isOpenPath(openPath, path)
    const openDetails = () => navigate(path, { state: { row } })
    /* A sentence that leads with the action ("Update account — Weekday rota") already says it: the slot keeps the
       label for the column's scan, muted. */
    const action = actionText(row)
    const saysAction = !options.child && !!action && (primary === action || primary.startsWith(`${action} `))
    return {
      key: rowKey(row),
      indicator: rowIndicator(row),
      /* Leaves reserve the disclosure width so sentences align down a mixed list. */
      disclosure: 'spacer',
      title: <><ActorMark actor={actorText(row)} system={isMachineRow(row) || (actorLabel(row) === null && !isUnattributed(row))} />{primary}</>,
      titleText: primary,
      /* Beside its open event the list is narrow: the selected row wraps to two lines rather than cut its sentence. */
      titleLines: onPhone ? 'all' : current ? 2 : undefined,
      onRowClick: openDetails,
      current,
      rowClickLabel: `Open details for ${primary}`,
      actions: [
        { key: 'details', icon: '▤', label: 'Details', ariaLabel: `Open details for ${primary}`, onClick: openDetails },
      ],
      /* `task-id` is the row's only handle on its task: the subject is a note, not drawn in the row. Matches the
         pipeline line's attribute. */
      data: {
        kind: row.kind,
        'row-kind': options.child ? 'event' : 'row',
        'task-id': row.subject?.type === 'task' ? row.subject.id : undefined,
      },
      slots: [
        ...orgSlot(row.org, isUnattributed(row)),
        {
          key: 'action',
          width: ACTION_SLOT,
          gate: 'auditAction',
          align: 'left',
          testId: 'audit-action',
          /* A child's action is an indent marker; its label is the title (at 180px `task:complete` and
             `task:done` clipped to near-identical text). */
          dim: saysAction,
          title: options.child ? 'A folded pipeline event' : action,
          node: options.child ? '↳' : action,
        },
        {
          key: 'actor',
          width: ACTOR_SLOT,
          gate: 'auditActor',
          align: 'left',
          /* Mono for an address (compared char by char), not for the System chip. */
          mono: !isMachineRow(row),
          /* No one to name: muted, as the org slot's Unattributed is. */
          dim: isUnattributed(row),
          testId: 'audit-actor',
          title: actorTitle(actorText(row), row.org),
          node: actorNode(row),
        },
        {
          key: 'when',
          width: AGE_SLOT,
          mono: true,
          dim: true,
          testId: 'audit-when',
          /* Age in the slot (the scan), zoned instant on `title` (the exact comparison). */
          title: formatZoned(row.created_at, zone),
          node: compactAge(row.created_at),
        },
      ],
      notes,
    }
  }

  /** A collapsed run of machine rows for one task (D24), expandable. Told apart from the operator's
   *  own row for the same mutation by the ⚙ disc, the System pill and the disclosure.
   *  Children are built only while open: `FeedList` renders any supplied `row.children`. */
  /** A fold's sentence, `<subject> — <trail>`; the trail is also a row attribute for exact assertions. */
  function pipelineSentence(line: Extract<AuditLine, { kind: 'pipeline' }>): string {
    const subject = line.subject ? subjectText(line.subject) : taskReference(line.taskId)
    return `${subject} — ${trailSteps(line.rows).join(' → ')}`
  }

  function pipelineRowSpec(line: Extract<AuditLine, { kind: 'pipeline' }>): FeedRowSpec {
    const isOpen = expanded.has(line.key)
    const newest = line.rows[0]
    const trail = trailSteps(line.rows)
    const subject = line.subject ? subjectText(line.subject) : taskReference(line.taskId)
    const sentence = pipelineSentence(line)
    return {
      key: line.key,
      indicator: {
        tone: 'neutral',
        glyph: '⚙',
        label: 'Task pipeline',
        title: `${line.rows.length} pipeline event${line.rows.length === 1 ? '' : 's'} for ${subject}`,
        data: { status: 'pipeline' },
      },
      disclosure: {
        expanded: isOpen,
        onToggle: () => toggleExpanded(line.key),
        /* `FeedList` builds the accessible name from this ("Expand pipeline events of #2008 …"). */
        label: 'pipeline events',
      },
      title: sentence,
      titleText: sentence,
      titleLines: onPhone ? 'all' : undefined,
      data: { 'task-id': line.taskId, 'pipeline-trail': trail.join(' → '), 'row-kind': 'pipeline' },
      slots: [
        ...orgSlot(newest.org),
        {
          key: 'action',
          width: ACTION_SLOT,
          gate: 'auditAction',
          align: 'left',
          testId: 'audit-action',
          title: 'Folded pipeline events',
          node: `${line.rows.length} events`,
        },
        {
          key: 'actor',
          width: ACTOR_SLOT,
          gate: 'auditActor',
          align: 'left',
          testId: 'audit-actor',
          title: actorTitle(SYSTEM_ACTOR_LABEL, newest.org),
          node: <StatusChip label={SYSTEM_ACTOR_LABEL} tone="neutral" glyph="⚙" ariaLabel="System actor" />,
        },
        {
          key: 'when',
          width: AGE_SLOT,
          mono: true,
          dim: true,
          testId: 'audit-when',
          title: formatZoned(newest.created_at, zone),
          node: compactAge(newest.created_at),
        },
      ],
      children: isOpen ? line.rows.map(row => auditRowSpec(row, { child: true })) : undefined,
    }
  }

  /** A fold of one person's own events as one line: the `lead` row's spec (status, slots, opening it), titled
   *  `sentence`, expandable to every event it folds (each still opens on its own). `action` (default: the lead's) fills
   *  the action slot, muted when the fold's sentence already leads with it. `opens` (default: the lead) is the record
   *  the line opens and names as its actor. */
  function foldedRowSpec(
    line: { key: string; rows: AuditRow[] }, lead: AuditRow, sentence: string, what: string, rowKind: string,
    action?: string, opens: AuditRow = lead,
  ): FeedRowSpec {
    const isOpen = expanded.has(line.key)
    const newest = auditRowSpec(lead)
    const opened = opens === lead ? newest : auditRowSpec(opens)
    /* Who and whose come from the record that opens, so the fold's Org reads as its detail's Account does: an
       unknown address's refusal is Unattributed, never its request's operator org. */
    const opensSlot = (key: string) => (OPENS_SLOT_KEYS.has(key) ? opened.slots?.find(slot => slot.key === key) : undefined)
    const said = action ?? actionText(lead)
    const dim = !!said && (sentence === said || sentence.startsWith(`${said} `))
    return {
      ...newest,
      slots: newest.slots?.map(slot => (
        slot.key === 'action' ? { ...slot, title: said, node: said, dim } : opensSlot(slot.key) ?? slot
      )),
      key: line.key,
      title: <><ActorMark actor={actorText(opens)} system={opens !== lead && actorLabel(opens) === null && !isUnattributed(opens)} />{sentence}</>,
      titleText: sentence,
      onRowClick: opened.onRowClick,
      rowClickLabel: `Open details for ${sentence}`,
      /* The open event highlights as its own child row, never twice. */
      current: false,
      actions: opened.actions?.map(action => ({ ...action, ariaLabel: `Open details for ${sentence}` })),
      disclosure: { expanded: isOpen, onToggle: () => toggleExpanded(line.key), label: what },
      data: { ...newest.data, 'row-kind': rowKind },
      children: isOpen ? line.rows.map(row => {
        const child = auditRowSpec(row)
        return { ...child, data: { ...child.data, 'row-kind': 'event' } }
      }) : undefined,
    }
  }

  /** A counted run of one person's identical self-writes: the newest row, its sentence counted. */
  function repeatRowSpec(line: Extract<AuditLine, { kind: 'repeat' }>): FeedRowSpec {
    return foldedRowSpec(line, line.rows[0], repeatSentence(line), 'repeated events', 'repeat')
  }

  /** One sign-in: its request's status and slots, its factor record's sentence. One refused for an address
   *  no account holds opens its refusal record and names it "Unattributed"; any other opens and names its request. */
  function signInRowSpec(line: Extract<AuditLine, { kind: 'signin' }>): FeedRowSpec {
    const opens = isUnattributed(line.factor) ? line.factor : line.request
    return foldedRowSpec(line, line.request, signInSentence(line), 'sign-in events', 'signin', signInAction(line), opens)
  }

  /** Every line kind's title. */
  function lineSentence(line: AuditLine): string {
    switch (line.kind) {
      case 'row': return rowSentence(line.row)
      case 'repeat': return repeatSentence(line)
      case 'signin': return signInSentence(line)
      case 'pipeline': return pipelineSentence(line)
    }
  }

  /* Each row's title, so a row can see what the one under it already names. */
  const lineTitles = lines.map(lineSentence)
  const auditRows: FeedRowSpec[] = lines.map((line, i) => {
    switch (line.kind) {
      case 'row': return auditRowSpec(line.row, { nextTitle: lineTitles[i + 1] })
      case 'repeat': return repeatRowSpec(line)
      case 'signin': return signInRowSpec(line)
      case 'pipeline': return pipelineRowSpec(line)
    }
  })

  const range = {
    from: filters.from,
    to: filters.to,
    fromUnparseable: localInputToIso(filters.from) === false,
    toUnparseable: localInputToIso(filters.to) === false,
    onFrom: (value: string) => update({ from: value }, { replace: true }),
    onTo: (value: string) => update({ to: value }, { replace: true }),
  }

  /* The count is the shown page's; the rows already paged through set its floor. */
  const pager = read && (
    <Pager
      testId="audit-pagination"
      labelTestId="audit-pagination-count"
      depth={stack.length}
      label={eventCount(read.page.total, read.page.total_kind, read.depth * PER_PAGE + read.page.items.length)}
      previousLabel="Newer"
      nextLabel="Older"
      previousDisabled={!(stack.length > 0 && !loading)}
      nextDisabled={!(current && read.page.next_cursor !== null)}
      onPrevious={newer}
      onNext={older}
    />
  )

  return (
    /* The query container the `audit*` gates and `contentSlotWidth`'s `cqw` read: the tab's own width, which is
       the list's. On the frame, not the list, so the filter strip folds on it too (`AUDIT_FOLD_FILTERS`). */
    <PagedLogView
      sx={{ pt: 2, ...AUDIT_LIST_CONTAINER_SX }}
      testIdPrefix="audit"
      search={{
        control: (
          <ActivitySearch
            query={filters.q}
            onQuery={next => update({ q: next })}
            chips={chips}
            onAddChip={addChip}
            onRemoveChip={removeChip}
            operator={operator}
            resetKey={searchReset}
          />
        ),
      }}
      filters={
        <SegmentedControl
          value={filters.type}
          options={ACTION_TYPES}
          onChange={next => update({ type: next })}
          ariaLabel="Activity type"
          buttonSx={tapTargetSx}
        />
      }
      foldQuery={AUDIT_FOLD_FILTERS}
      /* On a phone the type is a Filters page, beside From/To: on its own row it pushed the list a row down. */
      phoneFilters={{
        items: [{
          kind: 'page',
          id: 'type',
          label: 'Type',
          value: ACTION_TYPES.find(t => t.value === filters.type)?.label ?? 'All',
          render: nav => (
            <RadioPage
              testId="audit-type"
              label="Activity type"
              options={ACTION_TYPES.map(t => ({ ...t, hint: '' }))}
              value={filters.type}
              onPick={next => update({ type: next })}
              nav={nav}
            />
          ),
        }],
        count: filters.type === defaultType ? 0 : 1,
        onClear: () => update({ type: defaultType, from: '', to: '' }),
      }}
      range={range}
      zone={zone}
      hasFilters={hasFilters}
      onClear={clearFilters}
    >
      {/* Visible disclosure: `status` is NULL on every non-middleware row (reads, `task:*`, hand-written
          records incl. the superadmin-permission-bypass event), and a status predicate cannot match NULL. */}
      {filters.result !== '' && (
        <Box sx={{ mt: 1 }}>
          <Banner severity="info" testId="audit-status-exclusion">
            Result filters match web requests only. Views, pipeline events and system records are hidden.
          </Banner>
        </Box>
      )}

      {error && data !== null && (
        <StaleResultBanner error={error} onRetry={() => void reload()} retryTestId="audit-retry" />
      )}

      {/* Busy while a read is in flight: the rows turn only when the page lands. */}
      <Box sx={{ mt: 1 }} aria-busy={loading || undefined} data-testid="audit-list">
        {/* A load failure is never shown as an empty log; `any_visible` decides "filters match nothing" vs
            "nothing captured". `ariaLabel`/`testId` keep their historical names: `e2e/audit-users.spec.ts`
            and `audit-tabs` wait on `audit-table`. No phone full bleed: the bleed belongs to the table idiom
            (`ui/listStyles.ts`); feed rows drop slots instead. */}
        <ListStates
          loading={loading && data === null}
          skeleton={{ rows: SKELETON_ROWS, label: 'Loading activity…' }}
          failed={error && data === null && (
            <EmptyState
              title={`Could not load the activity log: ${error}`}
              action={<Button variant="outlined" data-testid="audit-retry" onClick={() => void reload()}>Retry</Button>}
            />
          )}
          empty={items.length === 0}
          /* An empty page past the newest (rows aged out, or the last page was exactly full) keeps its way
             back: without it the pager would vanish with the rows. */
          noMatch={read !== null && read.depth > 0 ? (
            <>
              <EmptyState
                title="No older activity"
                action={<Button variant="outlined" data-testid="audit-newest" onClick={newest}>Newest</Button>}
              />
              {pager}
            </>
          ) : (data?.any_visible ?? false) && (
            <NoMatchesState title="No activity matches your filters" onClear={hasFilters ? clearFilters : undefined} />
          )}
          emptyTitle="No activity recorded yet"
          feed={{ ariaLabel: 'Activity log', testId: 'audit-table', sections: [{ key: 'activity', rows: auditRows }] }}
        >
          {pager}
        </ListStates>
      </Box>
    </PagedLogView>
  )
}
