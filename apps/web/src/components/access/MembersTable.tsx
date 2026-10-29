import { useEffect, useMemo, useState, type JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import FeedList from '../../ui/feed/FeedList'
import type { FeedColumn, FeedRowSpec, OverflowItem } from '../../ui/feed/types'
import { PageNav } from '../../ui/Pager'
import { useClientPage } from '../../hooks/usePageClamp'
import RelativeTime from '../../ui/RelativeTime'
import { absoluteTime, relativeAge, shortDay } from '../../lib/relativeTime'
import { customerUrl } from '../../lib/customers'
import { toggled } from '../../lib/sets'
import type { MemberRow, ScopeType } from './api'

// The role-binding list: the console's one list (`ui/feed/FeedList`) at every width, one renderer for two modes.
// `members` (a scope's Access tab) leads with the principal; `principal` (a user's/group's view) leads with the
// scope. Both feed `AccessTableRow`.

export type AccessTableMode = 'members' | 'principal'

export type AccessSubject = {
  kind: 'user' | 'group' | 'scope'
  name: string
  href?: string
  /** A group's size; shown under its name. */
  memberCount?: number | null
}

export type AccessTableRow = {
  /** Unique per (subject, role, source) — the dedupe key. */
  key: string
  bindingId: string
  subject: AccessSubject
  role: { slug: string; name: string }
  /** `via`: the group's or scope's name, and `at` its kind, for the read-only row's "Remove at …" link (to `href`). */
  source: { kind: 'direct' | 'inherited' | 'group'; text: string; href?: string; via?: string; at?: 'group' | 'org' | 'project' }
  expiresAt: string | null
  status: 'active' | 'expired'
  lastActiveAt: string | null
  editable: boolean
  /** A group binding's members, nested under it (never editable). */
  children: AccessTableRow[]
  /** The same person's other, read-only grants (a role via a group, an inherited one), folded into this row so a
   *  person reads once. */
  also?: Pick<AccessTableRow, 'key' | 'role' | 'source' | 'status'>[]
}

export const PAGE_SIZE = 100

/* An org's Access: the operator's customer page directly. A tenant has no `/customers`, so its link keeps the
   retired `/orgs/:id`, whose redirect (`pages/customers/LegacyRedirects.tsx`) lands it on its own org in Settings. */
function orgAccessHref(id: string, operator: boolean): string {
  return operator ? customerUrl(id, { tab: 'access' }) : `/orgs/${id}?tab=access`
}

/** Where an inherited binding is editable: a uuid names an org, an integer a project. */
export function scopeAccessHref(viaId: string, operator = false): string {
  return /^\d+$/.test(viaId) ? `/projects/${viaId}?tab=access` : orgAccessHref(viaId, operator)
}

function sourceOf(src: MemberRow['source'], operator: boolean): AccessTableRow['source'] {
  if (src.kind === 'inherited') {
    return {
      kind: 'inherited',
      text: `From ${src.via_name ?? 'parent'}`,
      via: src.via_name ?? undefined,
      at: src.via_id && /^\d+$/.test(src.via_id) ? 'project' : 'org',
      href: src.via_id ? scopeAccessHref(src.via_id, operator) : undefined,
    }
  }
  if (src.kind === 'group') {
    return {
      kind: 'group',
      text: `Via ${src.via_name ?? 'group'}`,
      via: src.via_name ?? undefined,
      at: 'group',
      href: src.via_id ? `/groups/${src.via_id}` : undefined,
    }
  }
  return { kind: 'direct', text: 'Direct' }
}

function rowKey(subject: string, role: string, src: MemberRow['source']) {
  return `${subject}|${role}|${src.kind}:${src.via_id ?? ''}`
}

/** The largest group whose members stand as their own rows: past it (an org's Everyone) one row per member
 *  would bury the scope's own grants, so the group's disclosure lists them instead. */
export const GROUP_SPREAD_MAX = 25

/** `GET /api/access/members` → table rows: a group's member rows (source `group`, same `binding_id`) stand as
 *  their own row ("Via <group>") for a group of at most `GROUP_SPREAD_MAX`, so who effectively holds a role reads
 *  per person; a larger group nests them under its row instead — never both, or each member reads
 *  twice. One row per (principal, role, source). `operator` picks the org links' home. */
export function toMemberRows(rows: MemberRow[], operator = false): AccessTableRow[] {
  const out: AccessTableRow[] = []
  const seen = new Set<string>()
  const groupByBinding = new Map<string, AccessTableRow>()
  const userOf = new Map<AccessTableRow, string>()
  for (const m of rows) {
    const p = m.principal
    const row: AccessTableRow = {
      key: rowKey(`${p.kind}:${p.id}`, m.role.slug, m.source),
      bindingId: m.binding_id,
      subject: {
        kind: p.kind,
        name: p.name,
        href: p.kind === 'group' ? `/groups/${p.id}` : `/users/${p.id}`,
        memberCount: p.kind === 'group' ? p.member_count : undefined,
      },
      role: m.role,
      source: sourceOf(m.source, operator),
      expiresAt: m.expires_at,
      status: m.status,
      lastActiveAt: m.last_active_at,
      // Read-only here unless bound here: an inherited row is edited where its source link points.
      editable: m.editable && m.source.kind === 'direct',
      children: [],
    }
    const parent = m.source.kind === 'group' ? groupByBinding.get(m.binding_id) : undefined
    /* A group bound on an enclosing scope: the member's row says both hops, or it reads as bound here. */
    if (parent?.source.kind === 'inherited' && parent.source.via) {
      row.source = { ...row.source, text: `${row.source.text} · from ${parent.source.via}` }
    }
    if (parent && (parent.subject.memberCount ?? Infinity) > GROUP_SPREAD_MAX) {
      const childKey = `${parent.key}>${row.key}`
      if (!seen.has(childKey)) {
        seen.add(childKey)
        parent.children.push({ ...row, key: childKey, editable: false })
      }
      continue
    }
    if (seen.has(row.key)) continue
    seen.add(row.key)
    out.push(row)
    if (p.kind === 'group') groupByBinding.set(m.binding_id, row)
    else userOf.set(row, p.id)
  }
  return foldPeople(out, userOf)
}

/** A lead row's precedence: a direct grant first, then an active one. */
const leadRank = (r: AccessTableRow) => (r.source.kind === 'direct' ? 2 : 0) + (r.status === 'active' ? 1 : 0)

/** One row per person: a user's grants via a group or inherited fold into their lead row — the first they hold
 *  directly (active first), else the first active — which then names every source. A second direct grant keeps its
 *  own row: it is a binding of its own, with its own Remove. A member nested under a large group's row
 *  (`GROUP_SPREAD_MAX`) is not a row here, so it never folds. */
function foldPeople(rows: AccessTableRow[], userOf: Map<AccessTableRow, string>): AccessTableRow[] {
  const lead = new Map<string, AccessTableRow>()
  for (const r of rows) {
    const id = userOf.get(r)
    if (id === undefined) continue
    const held = lead.get(id)
    if (!held || leadRank(r) > leadRank(held)) lead.set(id, r)
  }
  const folded = new Map<AccessTableRow, AccessTableRow>()
  for (const r of rows) {
    const id = userOf.get(r)
    const into = id === undefined ? undefined : lead.get(id)
    if (into && into !== r && r.source.kind !== 'direct') folded.set(r, into)
  }
  if (folded.size === 0) return rows
  const out: AccessTableRow[] = []
  const also = new Map<AccessTableRow, NonNullable<AccessTableRow['also']>>()
  for (const r of rows) {
    const into = folded.get(r)
    if (into) {
      if (!also.has(into)) also.set(into, [])
      also.get(into)!.push({ key: r.key, role: r.role, source: r.source, status: r.status })
    } else out.push(r)
  }
  return out.map(r => (also.has(r) ? { ...r, also: also.get(r) } : r))
}

/** One binding a principal holds, for `principal` mode. `scope.href` overrides the default link. */
export type PrincipalBindingRow = Omit<MemberRow, 'principal'> & {
  scope: { type: ScopeType; id: string; name: string; href?: string }
}

function scopeHref(scope: PrincipalBindingRow['scope'], operator: boolean): string | undefined {
  if (scope.href) return scope.href
  if (scope.type === 'org') return orgAccessHref(scope.id, operator)
  if (scope.type === 'project') return `/projects/${scope.id}?tab=access`
  if (scope.type === 'account') return `/accounts/${scope.id}?tab=access`
  return undefined
}

export function toPrincipalRows(rows: PrincipalBindingRow[], operator = false): AccessTableRow[] {
  const seen = new Set<string>()
  return rows.flatMap(b => {
    const key = rowKey(`${b.scope.type}:${b.scope.id}`, b.role.slug, b.source)
    if (seen.has(key)) return []
    seen.add(key)
    return [{
      key,
      bindingId: b.binding_id,
      subject: { kind: 'scope' as const, name: b.scope.name, href: scopeHref(b.scope, operator) },
      role: b.role,
      source: sourceOf(b.source, operator),
      expiresAt: b.expires_at,
      status: b.status,
      lastActiveAt: b.last_active_at,
      editable: b.editable,
      children: [],
    }]
  })
}

export type SortKey = 'name' | 'role' | 'lastActive'
export type Sort = { key: SortKey; dir: 'asc' | 'desc' }

/** A row's highest role rank, its folded grants' included; undefined when the catalog ranks none of them. */
function bestRank(r: AccessTableRow, roleRank: Record<string, number>): number | undefined {
  const ranks = [r.role, ...(r.also ?? []).map(a => a.role)].map(role => roleRank[role.slug]).filter(n => n !== undefined)
  return ranks.length ? Math.max(...ranks) : undefined
}

const byName = (a: AccessTableRow, b: AccessTableRow) =>
  a.subject.name.localeCompare(b.subject.name, undefined, { sensitivity: 'base' })

/** A member's own "Via <group>" row (`toMemberRows`' spread): it shares its group row's binding. */
const isSpreadMember = (r: AccessTableRow) => r.source.kind === 'group' && r.subject.kind !== 'group'

/** Sorts top-level rows; children stay name-ordered. `role` asc = highest rank first (the catalog's
 *  order); `lastActive` asc = most recent first, never-active last. By name, a group's spread member rows
 *  follow their group's row, name-ordered, rather than scatter through the roster. */
export function sortRows(rows: AccessTableRow[], sort: Sort, roleRank: Record<string, number> = {}) {
  const sign = sort.dir === 'asc' ? 1 : -1
  const cmp = (a: AccessTableRow, b: AccessTableRow): number => {
    if (sort.key === 'role') {
      const ra = bestRank(a, roleRank)
      const rb = bestRank(b, roleRank)
      const d = ra !== undefined && rb !== undefined
        ? rb - ra
        : a.role.name.localeCompare(b.role.name)
      return sign * d || byName(a, b)
    }
    if (sort.key === 'lastActive') {
      const ta = a.lastActiveAt ? Date.parse(a.lastActiveAt) : -Infinity
      const tb = b.lastActiveAt ? Date.parse(b.lastActiveAt) : -Infinity
      if (ta === tb) return byName(a, b)
      return sign * (tb > ta ? 1 : -1)
    }
    return sign * byName(a, b)
  }
  const withKids = (r: AccessTableRow) => (r.children.length ? { ...r, children: [...r.children].sort(byName) } : r)
  if (sort.key !== 'name') return [...rows].sort(cmp).map(withKids)
  const groupBindings = new Set(rows.filter(r => r.subject.kind === 'group').map(r => r.bindingId))
  const attached = (r: AccessTableRow) => isSpreadMember(r) && groupBindings.has(r.bindingId)
  const members = new Map<string, AccessTableRow[]>()
  for (const r of rows) if (attached(r)) members.set(r.bindingId, [...(members.get(r.bindingId) ?? []), r])
  return rows
    .filter(r => !attached(r))
    .sort(cmp)
    .flatMap(r => [withKids(r), ...(r.subject.kind === 'group' ? (members.get(r.bindingId) ?? []).sort(cmp) : [])])
}

const matches = (r: AccessTableRow, q: string) =>
  [r.subject.name, r.role.name, r.source.text, ...(r.also ?? []).flatMap(a => [a.role.name, a.source.text])]
    .some(s => s.toLowerCase().includes(q))

/** Case-insensitive over name, role and source. A group survives when it or any member matches; a
 *  member-only match keeps just the matching members and flags the group `forceOpen`. */
export function filterRows(rows: AccessTableRow[], filter: string): (AccessTableRow & { forceOpen?: boolean })[] {
  const q = filter.trim().toLowerCase()
  if (!q) return rows
  return rows.flatMap(r => {
    if (matches(r, q)) return [r]
    const kids = r.children.filter(c => matches(c, q))
    return kids.length ? [{ ...r, children: kids, forceOpen: true }] : []
  })
}

/** Where a read-only row's access is removed, as a ⋯ item (a hover title was invisible on touch): a
 *  member's "Via <group>" row at the group (leave it), an inherited row at the scope it is inherited from.
 *  Only where this list removes anything; none for a group's own row. */
export function removeAt(row: AccessTableRow, onRemove: boolean): { text: string; title: string; href?: string } | undefined {
  if (!onRemove || !(isSpreadMember(row) || row.source.kind === 'inherited')) return undefined
  const at = row.source.at ?? 'group'
  return { text: `Remove at ${at}`, title: `Remove at ${row.source.via ?? `the ${at}`}`, href: row.source.href }
}

/** One drawn row: a top-level binding, or (`child`) an expanded group's member. */
type Line = { row: AccessTableRow; expanded: boolean; child: boolean; nested: boolean; spacer: boolean }

/* The trailing cluster, priced at the slot face (11px): a role name ("Project Maintainer"), a source ("Via Ops ·
   from Operator", ellipsised, whole on hover), a short date, a prose age. Gated in `SlotGate`'s drop order — the
   dates at `meta` (≤899px, as the old table's own query folded them), the source at `id`, the role off the phone,
   where the row's hover note says them all — and dropped sooner by the list's own width (`ui/feed/fit.ts`). */
const EXPIRES_SLOT = 80
const LAST_ACTIVE_SLOT = 88
const SOURCE_SLOT = 160
const ROLE_SLOT = 120

const SLOT_COLUMNS: FeedColumn[] = [
  { key: 'expires', label: 'Expires' },
  { key: 'lastActive', label: 'Last active', sortKey: 'lastActive' },
  { key: 'source', label: 'Source' },
  { key: 'role', label: 'Role', sortKey: 'role' },
]

/* A binding as a feed row: the state is the disc, the name the title, the facts the slots, and all of them the
   row's note on hover for the bands that drop a slot. Where to remove a read-only row, or the
   source's link, is a ⋯ item. */
function bindingFeedRow(
  { row, expanded, child, nested, spacer }: Line,
  toggle: (key: string) => void,
  onRemove?: (row: AccessTableRow) => void,
): FeedRowSpec {
  const expired = row.status === 'expired'
  const count = row.subject.kind === 'group' ? row.subject.memberCount : null
  const at = child ? undefined : removeAt(row, !!onRemove)
  const also = (row.also ?? []).map(a => `${a.role.name} (${a.source.text}${a.status === 'expired' ? ', expired' : ''})`)
  const meta = [
    row.role.name,
    row.source.text,
    ...also,
    count != null ? (count === 1 ? '1 member' : `${count} members`) : null,
    !expired && row.expiresAt ? `Expires ${shortDay(row.expiresAt)}` : null,
    row.lastActiveAt ? `Active ${relativeAge(row.lastActiveAt)}` : null,
    at && !at.href ? at.title : null,
  ].filter(Boolean).join(' · ')
  const actions: OverflowItem[] = []
  if (at?.href) actions.push({ key: 'remove-at', label: at.title, ariaLabel: at.title, testId: 'member-remove-at', to: at.href })
  else if (row.source.href) {
    actions.push({ key: 'source', label: `Open ${row.source.text}`, ariaLabel: `Open ${row.source.text}`, to: row.source.href })
  }
  if (!child && row.editable && onRemove) {
    actions.push({
      key: 'remove',
      label: 'Remove',
      ariaLabel: `Remove ${row.subject.name} ${row.role.name}`,
      testId: 'member-remove',
      danger: true,
      onClick: () => onRemove(row),
    })
  }
  return {
    key: row.key,
    depth: child || nested ? 1 : 0,
    indicator: expired
      ? { tone: 'neutral', glyph: '✕', label: 'Expired', title: absoluteTime(row.expiresAt) }
      : { tone: 'success', glyph: '●', label: 'Active' },
    disclosure: child || nested
      ? undefined
      : row.children.length > 0
        ? { expanded, onToggle: () => toggle(row.key), label: 'members' }
        : spacer ? 'spacer' : undefined,
    title: row.subject.name,
    titleText: row.subject.name,
    titleLines: 2,
    href: row.subject.href,
    muted: expired,
    notes: [{ key: 'meta', tone: 'muted', text: meta }],
    slots: [
      {
        key: 'expires', width: EXPIRES_SLOT, gate: 'meta', align: 'right', dim: true, testId: 'member-expires',
        title: row.expiresAt ? absoluteTime(row.expiresAt) : undefined,
        node: expired ? 'Expired' : row.expiresAt ? shortDay(row.expiresAt) : 'Never',
      },
      {
        key: 'lastActive', width: LAST_ACTIVE_SLOT, gate: 'meta', align: 'right', dim: true, testId: 'member-last-active',
        node: <RelativeTime iso={row.lastActiveAt} />,
      },
      {
        key: 'source', width: SOURCE_SLOT, gate: 'id', align: 'left', testId: 'member-source',
        title: [row.source.text, ...also].join(' · '),
        node: row.source.href ? <Link component={RouterLink} to={row.source.href}>{row.source.text}</Link> : row.source.text,
      },
      { key: 'role', width: ROLE_SLOT, gate: 'phone', align: 'left', testId: 'member-role', title: row.role.name, node: row.role.name },
    ],
    actions: actions.length > 0 ? actions : undefined,
    data: { kind: row.subject.kind, source: row.source.kind, status: row.status },
  }
}

export function MembersTable({
  rows,
  mode = 'members',
  filter = '',
  roleRank,
  onRemove,
}: {
  rows: AccessTableRow[]
  mode?: AccessTableMode
  filter?: string
  /** role slug → rank, for the Role sort. Absent → by role name. */
  roleRank?: Record<string, number>
  onRemove?: (row: AccessTableRow) => void
}): JSX.Element {
  const [sort, setSort] = useState<Sort>({ key: 'name', dir: 'asc' })
  const [open, setOpen] = useState<Set<string>>(() => new Set())

  const shown = useMemo(
    () => filterRows(sortRows(rows, sort, roleRank), filter),
    [rows, sort, roleRank, filter],
  )
  // Pages count drawn rows, an expanded group's members included: a large group opened (or forced
  // open by the filter) must not render all of its members at once.
  const lines = useMemo((): Line[] => {
    // By name a group's "Via <group>" rows follow its row (`sortRows`), so they indent under it.
    const groupBindings = new Set(shown.filter(r => r.subject.kind === 'group').map(r => r.bindingId))
    const nestedUnder = (r: AccessTableRow) => sort.key === 'name' && isSpreadMember(r) && groupBindings.has(r.bindingId)
    // A disclosure's width is reserved only when some row has one; else it is an empty gap before every name.
    const spacer = shown.some(r => r.children.length > 0)
    return shown.flatMap(row => {
      const expanded = !!row.forceOpen || open.has(row.key)
      return [
        { row, expanded, child: false, nested: nestedUnder(row), spacer },
        ...(expanded ? row.children.map(c => ({ row: c, expanded: false, child: true, nested: false, spacer })) : []),
      ]
    })
  }, [shown, open, sort.key])
  const { current, setPage, totalPages, pageItems, label } = useClientPage(lines, PAGE_SIZE)
  useEffect(() => { setPage(1) }, [filter])
  // A "Via <group>" row that opens a page has no group row above it there: drawn flush, not as an orphan child.
  const pageLines = useMemo(() => {
    const groupsAbove = new Set<string>()
    return pageItems.map(line => {
      if (!line.child && line.row.subject.kind === 'group') groupsAbove.add(line.row.bindingId)
      return line.nested && !groupsAbove.has(line.row.bindingId) ? { ...line, nested: false } : line
    })
  }, [pageItems])

  const onSort = (key: string) =>
    setSort(s => (s.key === key ? { key: s.key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key: key as SortKey, dir: 'asc' }))
  const toggle = (key: string) => setOpen(prev => toggled(prev, key))

  return (
    <Box sx={{ minWidth: 0 }}>
      <FeedList
        ariaLabel={mode === 'principal' ? 'Access' : 'Members'}
        testId="members-table"
        emptyMessage="No matches."
        sections={pageLines.length ? [{ key: 'members', rows: pageLines.map(line => bindingFeedRow(line, toggle, onRemove)) }] : []}
        columns={{ title: { key: 'name', label: mode === 'principal' ? 'Scope' : 'Member', sortKey: 'name' }, slots: SLOT_COLUMNS }}
        sort={sort}
        onSort={onSort}
      />
      <PageNav
        page={current}
        totalPages={totalPages}
        setPage={setPage}
        label={label}
      />
    </Box>
  )
}

export default MembersTable
