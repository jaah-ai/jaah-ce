import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import { apiDetail, get } from '../../api'
import { useAuth } from '../../auth'
import { useChangeOrgAction } from '../../components/ChangeOrgDialog'
import { useConfirm, type ConfirmOptions } from '../../components/ConfirmDialog'
import { deleteItem, type OverflowItem } from '../../components/tasks/rowActions'
import { ACTION_ICON } from '../../components/tasks/shared'
import PaneModeToggle from '../../components/tasks/pane/PaneModeToggle'
import { usePersonaRegistry } from '../../components/tasks/usePersonaRegistry'
import { useApiRead } from '../../hooks/useApiRead'
import { useRowActions } from '../../hooks/useBusyIds'
import { useUrlFilters } from '../../hooks/useUrlFilters'
import { loadListView, saveListView, usePersistedListView, type ListView } from '../../lib/listViewStorage'
import { buildNameTree, type NameTreeKind, type NameTreeNode } from '../../lib/nameTree'
import { NO_REFILING_DEPS, nameTreeRows } from '../../lib/nameTreeRows'
import { compactAge, compactSpan, NO_VALUE } from '../../lib/relativeTime'
import { formatZoned } from '../../lib/zonedTime'
import Banner from '../../ui/Banner'
import ToolbarMenu, { filterRadioPage } from '../../ui/ToolbarMenu'
import LoadFailedState from '../../ui/LoadFailedState'
import ListStates from '../../ui/feed/ListStates'
import { contentSlotWidth } from '../../ui/feed/geometry'
import type { FeedColumn, FeedRowSpec, FeedSlot } from '../../ui/feed/types'
import FilterBar from '../../ui/FilterBar'
import { FilterRow, FilterSearch, FilterSelect, matchesAny } from '../../ui/FilterRow'
import { AUTOMATIONS_FOLD_FILTERS, AUTOMATIONS_LIST_CONTAINER_SX } from '../../ui/listStyles'
import NewButton from '../../ui/NewButton'
import NoMatchesState from '../../ui/NoMatchesState'
import PageHeader from '../../ui/PageHeader'
import type { Tone } from '../../ui/StatusChip'
import {
  removedSuffix,
  summarizeTarget,
  type TargetDangling,
  type TargetItem,
  type TargetKind,
  type TargetRule,
} from '../../ui/TargetSelector'
import { VISUALLY_HIDDEN } from '../../ui/visuallyHidden'
import UnmatchedEmails from '../triggers/UnmatchedEmails'
import { MAX_TEMPLATE_REFS, templateRefProblem, templateRefs, type TemplateRef } from '../templates/api'
import { isEnabled } from '../../lib/editionFeatures'
import { useSourceKindLabels } from '../triggers/vocabulary'
import './actions'
import { actionBrief, actionSummary } from './actions/registry'
import {
  AUTOMATIONS_KEY,
  deleteAutomation,
  listActionTypes,
  listAutomations,
  pauseAutomation,
  previewTarget,
  releaseAutomation,
  resumeAutomation,
  runAutomation,
  RUNS_REREAD_MS,
  type Automation,
} from './api'
import {
  lastRunOutcome,
  STATUS_GLYPH,
  STATUS_LABELS,
  STATUS_TONE,
  unrecordedOutcome,
  whenShort,
  whenSummary,
} from './vocabulary'

/* AUTOMATION — every automation this viewer may manage, one feed row each (`ui/feed/FeedList`, the console's
 * one list), in THE STANDARD LIST SHAPE Playbooks draws (`pages/playbooks/PlaybooksPage.tsx`): the Flat/Tree
 * toggle on the tab strip, Tree filing `org/area/name` rows into folders by prefix (`lib/nameTree.ts`), the view
 * remembered per viewer (`lib/listViewStorage.ts`). A FIRST visit opens in Tree with every folder open: an
 * operator's automations are named by prefix, and a flat roster of thirteen `jaah/…` rows was the shape this
 * replaced. The row is ONE LINE of facts an operator SCANS DOWN as columns (a second line
 * per row was clutter) — Target · Action in their shortest words ("jaah · Coder", the full sentence on hover),
 * When (the server's compact phrase; the full sentence and the next run on hover), the last run as an outcome
 * glyph and age. Folders carry no mark: the disclosure and count say folder. Where those columns gate off — a narrow
 * window, or a narrow list beside an open automation (`AUTOMATIONS_NO_META`) — the row's hover carries
 * When · Target · Action in full; the status is the mark, a failing one's reason an error note. The ⋯
 * carries Run now beside
 * Pause/Resume and Delete. The strip above is `components/RouteTabLayout.tsx`'s; the boundary is
 * `/api/automations`' own (`routes/automations.py`), which never serves a system row to a non-operator. The list
 * is unpaged, so search and the selects narrow in the browser; an operator's Owner select splits
 * the platform's rows from the rest. */

const SKELETON_ROWS = 5

/** The header row over the slots: each label sits over its column and drops off at the same width (`FeedList`'s
 *  `columns`). The last-run slot is priced for "✓ 364d", so its header is the one word that fits. */
const COLUMNS: { title: FeedColumn; slots: FeedColumn[] } = {
  title: { key: 'name', label: 'Name' },
  slots: [
    { key: 'facts', label: 'Target · Action' },
    { key: 'when', label: 'When' },
    { key: 'last', label: 'Last', ariaLabel: 'Last run' },
  ],
}

/** An outcome glyph and `compactAge`'s widest (`364d`), mono 11px. */
const LAST_SLOT = 52
/* When and Target · Action are sized to the roster's widest value (`ui/feed/geometry.ts::contentSlotWidth`), never
 * narrower than these floors nor wider than their share of the list: at 1440px "acme, acme-ops,
 * org-a-probe-projects · Coder" and "GitHub: issues opened, reopened, labeled" read whole. The floors
 * bind only where the share is narrower — a 1024px window, or 1440px with the editor open — and there the Name
 * comes first: at 170px and 148px they left it ~110px, wrapping "Compliance manifest sweep" onto three lines.
 * What overflows ellipsises, its full value on hover. */
/** Fits "Daily 06:00 EDT" (15ch at the cluster's 11px). */
const WHEN_SLOT = 100
/** At most this percent of the list's width (`cqw`). */
const WHEN_MAX_CQW = 20
/** Fits "acme-shop · Coder" (~18ch at 11px). */
const FACTS_SLOT = 120
/** At most this percent of the list's width (`cqw`). */
const FACTS_MAX_CQW = 24
/** An all-system roster, whose column is mostly functions' descriptions (`factsShown`), sizes it to what it shows
 *  instead, up to this share: the Name column's leaf names are short, and at 1440px most of the row was empty Name
 *  space beside an ellipsised description. */
const DESCRIPTIONS_MAX_CQW = 40
/** ...and never past what leaves the Name column ~220px ("Evidence: Security Hub findings" on one line) beside the
 *  status mark, When, Last and ⋯: at 1024px the share alone wrapped a third of the names. */
const DESCRIPTIONS_NAME_ROOM = 500

/** The Target · Action width on an all-system roster (`DESCRIPTIONS_MAX_CQW`, `DESCRIPTIONS_NAME_ROOM`). */
function descriptionsWidth(texts: readonly string[]): number | string {
  const fit = contentSlotWidth(texts, FACTS_SLOT, DESCRIPTIONS_MAX_CQW)
  return typeof fit === 'number' ? fit : `min(${fit}, max(${FACTS_SLOT}px, 100cqw - ${DESCRIPTIONS_NAME_ROOM}px))`
}

/** Tree mode's vocabulary; `nameMax` only bounds drags, and this roster offers none. */
const AUTOMATION_TREE_KIND: NameTreeKind = {
  noun: { one: 'automation', many: 'automations' },
  nameMax: null,
  idPrefix: 'automation',
  groupMark: false,
}

/** The view before this viewer ever touched it: Tree, with every folder open (`openAll` below; identity tells
 *  that first visit from a stored view — `lib/listViewStorage.ts::loadListView`). */
const FIRST_VIEW: ListView = { q: '', mode: 'tree', expanded: [] }

const NOUNS: Record<TargetKind, { one: string; many: string }> = {
  projects: { one: 'project', many: 'projects' },
  nodes: { one: 'node', many: 'nodes' },
}

type Roster = { state: 'loading' | 'error' | 'ready'; items: TargetItem[] }
const rosterKey = (kind: string, orgId: string) => `${kind}\u0000${orgId}`

/** One read per distinct (kind, org) the list's targets name — never one per row. */
function useRosters(rows: Automation[]): Map<string, Roster> {
  const pairs = useMemo(() => {
    const seen = new Map<string, { kind: TargetKind; orgId: string }>()
    for (const row of rows) {
      if (row.target) seen.set(rosterKey(row.target.kind, row.org_id), { kind: row.target.kind, orgId: row.org_id })
    }
    return seen
  }, [rows])
  const [rosters, setRosters] = useState<Map<string, Roster>>(new Map())

  useEffect(() => {
    let live = true
    for (const [key, { kind, orgId }] of pairs) {
      if (rosters.has(key)) continue
      setRosters(prev => new Map(prev).set(key, { state: 'loading', items: [] }))
      get<{ items: TargetItem[]; total: number }>(
        `/api/automations/targets/${kind}?org_id=${encodeURIComponent(orgId)}`,
      ).then(
        data => live && setRosters(prev => new Map(prev).set(key, { state: 'ready', items: data.items ?? [] })),
        () => live && setRosters(prev => new Map(prev).set(key, { state: 'error', items: [] })),
      )
    }
    return () => {
      live = false
    }
    // `rosters` is read only to skip pairs already asked for; re-running on it would re-ask nothing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pairs])
  return rosters
}

/** A stored id the roster does not hold is named by the server's preview,
 *  one read per such row — the common row holds none and costs nothing. */
function useDangling(rows: Automation[], rosters: Map<string, Roster>): Map<string, TargetDangling[]> {
  const [dangling, setDangling] = useState<Map<string, TargetDangling[]>>(new Map())
  /* Keyed by the row AND its rule, so an edited rule is asked again; a mount-scoped liveness flag, so a roster
     landing later (a new `rosters` map, a re-run) never drops an answer still in flight. */
  const asked = useRef(new Set<string>())
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  useEffect(() => {
    for (const row of rows) {
      const rule = row.target
      const key = rule ? `${row.id}\u0000${rule.kind}\u0000${rule.mode}\u0000${rule.ids.join(',')}` : ''
      if (!rule || rule.mode === 'all' || asked.current.has(key)) continue
      const roster = rosters.get(rosterKey(rule.kind, row.org_id))
      if (roster?.state !== 'ready') continue
      const known = new Set(roster.items.map(item => String(item.id)))
      if (rule.ids.every(id => known.has(String(id)))) continue
      asked.current.add(key)
      previewTarget(row.org_id, rule, row.id).then(
        wire => mounted.current && setDangling(prev => new Map(prev).set(row.id, wire.dangling)),
        // Unanswered: the row keeps its "(removed)" guess, and a later pass may ask again.
        () => asked.current.delete(key),
      )
    }
  }, [rows, rosters])
  return dangling
}

/** Whether each Task action's stored template still resolves: one `refs` read for the whole list. */
function useTemplateRefs(rows: Automation[]): Map<string, TemplateRef> {
  const ids = useMemo(() => [...new Set(rows.flatMap(row => {
    const id = row.action.type === 'task' ? row.action.params.template_id : null
    return typeof id === 'string' && id !== '' ? [id] : []
  }))].slice(0, MAX_TEMPLATE_REFS), [rows])
  const { data } = useApiRead(['automation-template-refs', ids.join(',')], () => templateRefs(ids), 'failed',
    { enabled: ids.length > 0 })
  return useMemo(() => new Map((data ?? []).map(ref => [ref.id, ref])), [data])
}

/** A target without its roster: counts only ("3 projects", "All nodes except 2"). */
function targetWithoutNames(rule: TargetRule): string {
  const noun = NOUNS[rule.kind]
  const n = rule.ids.length
  if (rule.mode === 'all') return `All ${noun.many}`
  if (rule.mode === 'all_except') return n === 0 ? `All ${noun.many}` : `All ${noun.many} except ${n}`
  return `${n} ${n === 1 ? noun.one : noun.many}`
}

/** A future instant, compactly: "in 5m", "in 3h"; a past-due one reads "due now". */
function untilLabel(iso: string): string {
  const ms = Date.parse(iso) - Date.now()
  if (Number.isNaN(ms)) return NO_VALUE
  return ms < 60_000 ? 'due now' : `in ${compactSpan(ms)}`
}

/** The target in words — the sentence, and the column's shortest form (a picked list is its names alone, "jaah";
 *  every other rule keeps its lead, "All nodes (4)"); `null` for an action that takes none. */
function targetText(
  row: Automation,
  roster: Roster | undefined,
  dangling: TargetDangling[] | undefined,
): { full: string; brief: string } | null {
  if (!row.target) return null
  const rule = row.target
  if (roster?.state !== 'ready') {
    const words = targetWithoutNames(rule)
    return { full: words, brief: words }
  }
  const summary = summarizeTarget(rule, roster.items, NOUNS[rule.kind], dangling)
  if (rule.mode !== 'specific' || summary.names.length === 0) return { full: summary.full, brief: summary.full }
  const skipped = summary.skipped > 0 ? ` (${summary.skipped} skipped)` : ''
  return { full: summary.full, brief: `${summary.names.map(n => n.name + removedSuffix(n)).join(', ')}${skipped}` }
}

/** The next run, for the When's hover ("next in 3h, Oct 14, 2026, 09:00 EDT"); none for an event row, a paused one
 *  (the mark says Paused) or one not scheduled. */
function nextRun(row: Automation): string | null {
  if (row.when.kind === 'event' || row.paused || !row.next_run_at) return null
  const until = untilLabel(row.next_run_at)
  return `${until === 'due now' ? until : `next ${until}`}, ${formatZoned(row.next_run_at)}`
}

/** The When's hover leads with the cell's own phrase, so the two read as one value ("Daily 09:00 EDT · At 09:00 AM
 *  (EDT) · next in 3h, …"); a sentence the cell already shows whole is not said twice. */
function whenHover(when: string, whenFull: string, next: string | null): string {
  return [when, whenFull === when ? null : whenFull, next].filter(Boolean).join(' · ')
}

const toneSx = (tone: Tone) => (tone === 'neutral' ? undefined : { color: `${tone}.main` })

/** A restart's backfill as ONE clause ("12 missed while down"), never one failure per tick. */
function missedGap(row: Automation): string | null {
  const gap = row.last_run_missed
  if (!gap || gap.count === 0) return null
  return `${gap.count}${gap.more ? '+' : ''} missed while down`
}

/** Outcomes that did no work: their hover adds when the schedule last did (`last_acted_at`). */
const IDLE_OUTCOMES = new Set(['noop', 'skipped', 'missed'])

/** Past this, a never-run schedule's first run is overdue rather than due now. */
const OVERDUE_MS = 2 * 60_000

/** "last did work 3h ago", for an idle run's hover; null where no work is on record. */
function lastWork(row: Automation): string | null {
  if (!row.last_acted_at) return null
  const age = compactAge(row.last_acted_at)
  return `last did work ${age === 'now' ? 'just now' : `${age} ago`}`
}

/** The last run as its outcome and age ("✓ 3h"), the words on hover ("Filed Oct 11, 2026, 05:01 EDT") and for a
 *  screen reader. A run the server has an age for but no surviving record of reads "○" where the schedule's
 *  retention mode explains the missing row (`unrecordedOutcome`: an `acted` row's noop), so a healthy roster never
 *  mixes glyphed and bare cells; else the age alone. The firing right after a restart is such a run,
 *  its gap said once on hover ("Nothing to do … · after 12 missed while down"). An idle run's hover also says when
 *  the schedule last did work, and a never-run one's when it first runs. */
function lastRunSlot(row: Automation): FeedSlot {
  const base = { key: 'last', width: LAST_SLOT, mono: true, dim: true, align: 'right' as const, testId: 'automation-last-run' }
  if (!row.last_run_at) {
    // An overdue first run (a stalled scheduler) is not "due now": the hover then says only that it never ran.
    const next = row.when.kind === 'schedule' && !row.paused ? row.next_run_at : null
    const title = next && Date.parse(next) > Date.now() - OVERDUE_MS ? `Never run · first run ${untilLabel(next)}` : 'Never run'
    return { ...base, node: 'never', title, data: { outcome: 'never' } }
  }
  const at = formatZoned(row.last_run_at)
  const age = compactAge(row.last_run_at)
  const gap = missedGap(row)
  // A recorded `missed` row IS the gap; an unrecorded firing came after it.
  const said = (label: string) => {
    if (!gap) return `${label} ${at}`
    return row.last_run_outcome ? `${label} ${at} · ${gap}` : `${label} ${at} · after ${gap}`
  }
  const unrecorded = row.last_run_outcome ? null : unrecordedOutcome(row.when)
  const outcome = row.last_run_outcome ?? unrecorded?.data
  if (!outcome) return { ...base, node: age, title: said('Ran'), data: { outcome: 'unknown' } }
  const word = unrecorded ?? lastRunOutcome(outcome)
  const work = IDLE_OUTCOMES.has(outcome) ? lastWork(row) : null
  return {
    ...base,
    title: work ? `${said(word.label)} · ${work}` : said(word.label),
    data: { outcome },
    node: (
      <>
        <Box component="span" aria-hidden="true" sx={toneSx(word.tone)}>{word.glyph}</Box>
        <Box component="span" sx={VISUALLY_HIDDEN}>{word.label}</Box>
        {' '}
        {age}
      </>
    ),
  }
}

/** The Target · Action column's text: never blank, even where a system function is named for itself ("Billing
 *  events" runs "Billing events") — an operator's roster is mostly such rows, and a column blank all the way down
 *  under its header reads as broken. */
function factsText(target: { brief: string } | null, actionBrief: string): string {
  return target ? `${target.brief} · ${actionBrief}` : actionBrief
}

/** What the column SHOWS: a value that only repeats the row's name says nothing new, so a system function named
 *  for itself shows what it does instead (its description, ellipsised, whole on hover), else "System". */
function factsShown(row: Automation, facts: string): string {
  if (facts !== row.name || !row.is_system) return facts
  return row.description.trim() || 'System'
}

function automationRow(row: Automation, { when, whenFull, target, action, actionBrief, widths, showSystem, menu, busy, templateProblem }: {
  /** The compact When, the slot's text. */
  when: string
  /** The full sentence, the slot's hover. */
  whenFull: string
  target: { full: string; brief: string } | null
  /** The summary ("Task · Coder") and the column's word ("Coder"). */
  action: string
  actionBrief: string
  /** The list's one width per sized column, so every row lines up. */
  widths: { facts: number | string; when: number | string }
  showSystem: boolean
  /** The ⋯ items. */
  menu: OverflowItem[]
  busy: boolean
  /** "Template deleted" / "Template unavailable": the Task action's template no longer resolves. */
  templateProblem: string | null
}): FeedRowSpec {
  const label = STATUS_LABELS[row.status] ?? row.status
  const reason = row.status_reason
  const next = nextRun(row)
  const plain = factsText(target, actionBrief)
  const facts = factsShown(row, plain)
  /* The hover's line after the name is the one place a repeat of the name is clutter: it reads right below it. */
  const inlineAction = target !== null || actionBrief !== row.name
  const factsFull = target ? `${target.full} · ${action}` : facts === actionBrief ? action : facts
  return {
    key: row.id,
    indicator: {
      tone: STATUS_TONE[row.status] ?? 'neutral',
      // `○`, not `?`: the feed's `?` means "needs input".
      glyph: STATUS_GLYPH[row.status] ?? '○',
      label,
      title: reason ? `${label}: ${reason}` : label,
      data: { status: row.status },
    },
    title: row.name,
    titleText: row.name,
    titleLines: 'all',
    href: `/automations/${encodeURIComponent(row.id)}`,
    muted: row.paused,
    busy,
    data: { id: row.id, status: row.status },
    markers: [
      // The label is the hover and the announced name, so it says what the mark means.
      ...(showSystem && row.is_system ? [{ key: 'system', glyph: '⚙', label: 'System automation', testId: 'automation-system' }] : []),
      ...(templateProblem ? [{
        key: 'template', glyph: '⚠', label: templateProblem, tone: 'warning' as const, hug: true,
        word: { text: templateProblem }, testId: 'automation-template-status',
      }] : []),
    ],
    /* Window-gated first, so the survivors stay flush right (`ui/feed/geometry.ts::SlotGate`). */
    slots: [
      {
        key: 'facts',
        width: widths.facts,
        gate: 'automationsMeta',
        align: 'left',
        dim: true,
        testId: 'automation-facts',
        /* A description is prose: two lines read whole at 1024px, where one ellipsised it. */
        ...(facts !== plain && facts === row.description.trim() ? { lines: 2 as const } : null),
        title: factsFull,
        node: facts,
      },
      {
        key: 'when',
        width: widths.when,
        gate: 'automationsMeta',
        align: 'left',
        testId: 'automation-when',
        title: whenHover(when, whenFull, next),
        node: when,
      },
      lastRunSlot(row),
    ],
    actions: menu,
    /* The hover: When · Target · Action in full, the columns' words where they gate off; a failing
       one's cause and the raw exception text; a Stuck one's hint. */
    notes: [
      { key: 'facts', tone: 'muted', text: [whenFull, target?.full, inlineAction ? action : null].filter(Boolean).join(' · ') },
      ...(reason && row.status === 'failing'
        ? [{
            key: 'reason',
            text: row.status_cause && row.status_cause !== reason ? `${row.status_cause} — ${reason}` : reason,
            tone: 'error' as const,
          }]
        : []),
      /* A pause hides the Stuck cause, not the held run: the ⋯ still offers Release, so the row says why. */
      ...(row.stuck && row.status !== 'failing'
        ? [{ key: 'stuck', text: `Stuck. ${STUCK_PAUSED_HINT}`, tone: 'error' as const }]
        : []),
    ],
  }
}

/** Every folder's path, however deep — what a first visit opens. */
function groupPaths(nodes: readonly NameTreeNode<Automation>[], into: string[] = []): string[] {
  for (const node of nodes) {
    if (node.kind === 'group') {
      into.push(node.path)
      groupPaths(node.children, into)
    }
  }
  return into
}

/* Below `AUTOMATIONS_FOLD_FILTERS` When and Owner leave the strip for the Filters menu (Status stays: the
   failing rows are the ones an operator narrows to most); above it the menu is gone. */
const UNFOLDED_SX = { display: 'flex', [AUTOMATIONS_FOLD_FILTERS]: { display: 'none' } } as const
// `alignSelf: stretch` lets the Filters trigger inside take the strip's field height (`ui/ToolbarMenu`).
const FOLDED_SX = { display: 'none', alignSelf: 'stretch', [AUTOMATIONS_FOLD_FILTERS]: { display: 'flex' } } as const

const WHEN_OPTIONS = [
  { value: 'schedule', label: 'Schedule' },
  { value: 'event', label: 'Event' },
]
const STATUS_OPTIONS = ['active', 'paused', 'failing'].map(value => ({ value, label: STATUS_LABELS[value] }))
/** An operator's narrowing: the platform's rows, or the orgs'. Not "Mine": a teammate's row is not. */
const ORIGIN_OPTIONS = [
  { value: 'system', label: 'System' },
  { value: 'org', label: 'Org' },
]

/** A select's value from the address: one of its options, else none. */
function urlOption(value: string | null, options: readonly { value: string }[]): string {
  return options.some(option => option.value === value) ? (value as string) : ''
}

/** The address keys that make a visit a link: the selects' and the search's. */
const LINK_KEYS = ['q', 'when', 'status', 'owner'] as const

/** An empty filter result says which filter emptied it ("No failing org automations", "No automations matching
 *  “sweep”"), each set filter as the word its picker shows. */
export function noMatchesTitle({ q, status, origin, whenKind }: { q: string; status: string; origin: string; whenKind: string }): string {
  const label = (options: readonly { value: string; label: string }[], value: string) =>
    options.find(option => option.value === value)?.label.toLowerCase()
  const words = [label(STATUS_OPTIONS, status), label(ORIGIN_OPTIONS, origin), label(WHEN_OPTIONS, whenKind)]
  const search = q.trim()
  return ['No', ...words.filter(Boolean), 'automations', ...(search ? [`matching “${search}”`] : [])].join(' ')
}

/** Delete's confirm — one home for the list's ⋯ and the detail page's Delete button. */
export const deleteAutomationConfirm = (row: Automation): ConfirmOptions => ({
  title: 'Delete automation',
  identity: row.name,
  message: row.when.kind === 'event'
    ? 'Its URL stops accepting events, and its run history is deleted. Filed tasks stay.'
    : 'It stops running, and its run history is deleted. Filed tasks stay.',
  confirmLabel: 'Delete automation',
  destructive: true,
})

/** A paused schedule whose run is still held — the cause the pause hides (`automation_service.is_stuck`) — as its
 *  hover on the list and the detail header. */
export const STUCK_PAUSED_HINT = 'Its last run still holds the lock: Release it, or the schedule stays stuck after Resume.'

/** Release's confirm — one home for the list's ⋯ and the detail page's Release button. The held firing is not
 *  killed, only let go of, so the confirm says it may still land its work. */
export const releaseAutomationConfirm = (row: Automation): ConfirmOptions => ({
  title: 'Release',
  identity: row.name,
  message: 'Release? The stuck run may still finish.',
  confirmLabel: 'Release',
})

/** Why Run now is off, or null: a stuck schedule's lock refuses the firing. A paused one runs once and stays paused. */
export function runNowBlocked(row: Automation): string | null {
  return row.stuck ? 'Release it first' : null
}

export default function AutomationList() {
  /* `refetchOnMount: 'always'`: the edit page's writes land before it navigates back here, so every visit
     re-reads (cached rows paint meanwhile). A Change-org move invalidates every query, this one included. */
  const query = useQuery({ queryKey: AUTOMATIONS_KEY, queryFn: () => listAutomations(), refetchOnMount: 'always' })
  const data = query.data ?? null
  const loading = query.isPending || query.isFetching
  const error = query.error ? apiDetail(query.error, 'Failed to load automations') : ''
  const reload = () => query.refetch()
  const { data: actionTypes } = useApiRead(['automation-action-types'], listActionTypes, 'Failed to load action types')
  usePersonaRegistry()
  const sourceLabel = useSourceKindLabels()
  const { admin } = useAuth()
  const isOperator = admin?.is_operator === true
  /* A schedule or trigger moves with its automation (`tenancy/transfer_objects.py::AUTOMATION_RULE`). The row
     carries only `org_id`; the dialog reads only the org's id. A system automation is the platform's, never
     moved: no Change org item. Run now, Pause/Resume and Delete are every row's, as on the detail page. */
  const changeOrg = useChangeOrgAction('automation')
  const [confirm, confirmDialog] = useConfirm()
  const { busy: busyIds, error: actionError, run } = useRowActions()
  /** Run now's answer ("Running now — …"), a warning when nothing fired; the run lands after the firing, so
   *  the list re-reads then (`RUNS_REREAD_MS`), timers dropped with the page. */
  const [notice, setNotice] = useState<{ text: string; fired: boolean } | null>(null)
  const rereads = useRef<number[]>([])
  useEffect(() => () => rereads.current.forEach(id => window.clearTimeout(id)), [])
  /** One row's write; the list re-reads after it either way. */
  function rowVerb(row: Automation, write: () => Promise<unknown>, fallback: string) {
    setNotice(null)
    return run(row.id, fallback, write).then(() => { void reload() })
  }
  async function confirmAndDelete(row: Automation) {
    if (!(await confirm(deleteAutomationConfirm(row)))) return
    await rowVerb(row, () => deleteAutomation(row.id), 'Couldn’t delete this automation.')
  }
  async function confirmAndRelease(row: Automation) {
    if (!(await confirm(releaseAutomationConfirm(row)))) return
    await rowVerb(row, () => releaseAutomation(row.id), 'Couldn’t release.')
  }
  async function runNow(row: Automation) {
    await rowVerb(row, async () => {
      const out = await runAutomation(row.id)
      setNotice({ text: out.detail, fired: out.fired })
      if (!out.fired) return
      rereads.current.forEach(id => window.clearTimeout(id))
      rereads.current = RUNS_REREAD_MS.map(delay => window.setTimeout(() => void reload(), delay))
    }, 'Couldn’t run it.')
  }
  const menuOf = (row: Automation): OverflowItem[] => {
    const verb = row.paused ? 'Resume' : 'Pause'
    const blocked = runNowBlocked(row)
    const items: OverflowItem[] = [
      {
        key: 'run',
        icon: ACTION_ICON.run,
        label: 'Run now',
        ariaLabel: `Run ${row.name} now`,
        ...(blocked ? { disabled: true, disabledReason: blocked } : {}),
        onClick: () => void runNow(row),
      },
      // A stuck row's Run now points at Release, so Release sits right under it.
      ...(row.stuck ? [{
        key: 'release',
        icon: ACTION_ICON.reset,
        label: 'Release',
        ariaLabel: `Release ${row.name}`,
        testId: 'automation-release',
        onClick: () => void confirmAndRelease(row),
      }] : []),
      {
        key: row.paused ? 'resume' : 'pause',
        icon: row.paused ? ACTION_ICON.start : ACTION_ICON.wait,
        label: verb,
        ariaLabel: `${verb} ${row.name}`,
        onClick: () => void rowVerb(
          row,
          () => (row.paused ? resumeAutomation(row.id) : pauseAutomation(row.id)),
          row.paused ? 'Couldn’t resume.' : 'Couldn’t pause.',
        ),
      },
    ]
    // The failure's raw text is a hover on the row; a phone has none, so the item opens the page that shows it.
    if (row.status === 'failing' && row.status_reason) {
      items.unshift({
        key: 'failure',
        icon: ACTION_ICON.failure,
        label: 'View failure',
        ariaLabel: `View why ${row.name} is failing`,
        testId: 'automation-view-failure',
        to: `/automations/${encodeURIComponent(row.id)}?tab=runs`,
      })
    }
    const move = row.is_system ? null : changeOrg.menuItem({
      id: row.id,
      name: row.name,
      org: { id: row.org_id, name: '', slug: '' },
      href: `/automations/${encodeURIComponent(row.id)}`,
    })
    if (move) items.push(move)
    items.push(deleteItem(row.name, () => void confirmAndDelete(row)))
    return items
  }
  const [searchParams, setFilters] = useUrlFilters()
  const gone = searchParams.get('gone') === '1'

  /* ── The view (search, Flat/Tree, open folders), restored at mount and written through on each change. The
     When, Status and Owner selects are the URL's (`?when=`, `?status=`, `?owner=`), a link's narrowing, not the
     view: a visit without them shows every row. A LINK — any of those keys or `?q=` in the address — also says the
     search: its `?q=`, else none, so a remembered search never hides what `/automations?when=event` asked for.
     Typing and every select keep the address in step (`hooks/useUrlFilters`). */
  const firstVisit = useRef(false)
  const linked = LINK_KEYS.some(key => searchParams.has(key))
  const urlQ = linked ? (searchParams.get('q') ?? '') : null
  const { view, patchView, toggleGroup } = usePersistedListView(
    () => {
      const stored = loadListView('automations', admin?.id ?? null, FIRST_VIEW)
      firstVisit.current = stored === FIRST_VIEW
      return urlQ === null ? stored : { ...stored, q: urlQ }
    },
    next => saveListView('automations', admin?.id ?? null, next),
  )
  const search = (q: string) => {
    patchView({ q })
    setFilters({ q })
  }
  // A later link to this mounted list re-seeds the search; typing writes both already.
  useEffect(() => {
    if (urlQ !== null && urlQ !== view.q) patchView({ q: urlQ })
  }, [urlQ])
  /** A select's write carries the search along: the address then names the whole narrowing, so it reads as a
   *  link and a remembered search is not dropped by it. */
  const filterSetter = (key: string) => (value: string) => setFilters({ [key]: value, q: view.q })
  /* A value no picker offers is no filter. Owner is an operator's alone, who is the only one shown its select; the
     community edition has one org, so it has no Owner filter. */
  const ownerFilter = isOperator && isEnabled('multi_org_tenancy')
  const whenKind = urlOption(searchParams.get('when'), WHEN_OPTIONS)
  const status = urlOption(searchParams.get('status'), STATUS_OPTIONS)
  const origin = ownerFilter ? urlOption(searchParams.get('owner'), ORIGIN_OPTIONS) : ''
  const setWhenKind = filterSetter('when')
  const setStatus = filterSetter('status')
  const setOrigin = filterSetter('owner')
  const treeMode = view.mode === 'tree'
  /* A FIRST VISIT opens every folder: drawn open from the first paint (`expanded` below), and persisted as the
     open set once the rows say which folders exist, so the next visit opens the same way. A search typed before
     the rows land does not forfeit it; the viewer's first toggle is what ends it. */
  const [openAll, setOpenAll] = useState(() => firstVisit.current)

  const rows = useMemo(() => data?.items ?? [], [data])
  const rosters = useRosters(rows)
  const dangling = useDangling(rows, rosters)
  const templateRefsById = useTemplateRefs(rows)
  const actionLabels = new Map((actionTypes?.items ?? []).map(t => [t.type, t.label]))
  // The action registry words each type (AC4.5): the list knows no action type by name.
  const actionOf = (row: Automation) =>
    actionSummary(row.action.type, row.action.params, actionLabels.get(row.action.type), row.is_system)
  const actionBriefOf = (row: Automation) =>
    actionBrief(row.action.type, row.action.params, actionLabels.get(row.action.type), row.is_system)

  useEffect(() => {
    if (!openAll || rows.length === 0) return
    patchView({ expanded: groupPaths(buildNameTree(rows)) })
    setOpenAll(false)
    // `patchView` is this render's closure; `openAll` is what bounds the effect to once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openAll, rows])

  const shown = rows.filter(
    row =>
      matchesAny(view.q, row.name) &&
      (!whenKind || row.when.kind === whenKind) &&
      (!status || row.status === status) &&
      (!origin || row.is_system === (origin === 'system')),
  )
  const filtered = Boolean(view.q.trim() || whenKind || status || origin)
  /* The ⚙ tells a system row from the operator's own; on a roster of nothing but system rows it says nothing a
     row does not, so it drops off until another row shows beside them. */
  const showSystem = isOperator && shown.some(row => !row.is_system)
  const clearFilters = () => {
    patchView({ q: '' })
    setFilters({ q: '', when: '', status: '', owner: '' })
  }

  const targetOf = (row: Automation) => targetText(
    row,
    row.target ? rosters.get(rosterKey(row.target.kind, row.org_id)) : undefined,
    dangling.get(row.id),
  )
  /* Off every row, not the filtered ones: a keystroke in the search never moves the columns. A system row's
     description (`factsShown`) is measured only on an all-system roster; beside an operator's own rows it fills
     the column their short values size and ellipsises, leaving their names the width. */
  const factsOf = rows.map(row => factsText(targetOf(row), actionBriefOf(row)))
  const widths = {
    facts: rows.every(row => row.is_system)
      ? descriptionsWidth(rows.map((row, i) => factsShown(row, factsOf[i])))
      : contentSlotWidth(factsOf, FACTS_SLOT, FACTS_MAX_CQW),
    when: contentSlotWidth(rows.map(row => whenShort(row.when, sourceLabel)), WHEN_SLOT, WHEN_MAX_CQW),
  }
  const rowOf = (row: Automation, busy: boolean): FeedRowSpec => automationRow(row, {
    when: whenShort(row.when, sourceLabel),
    whenFull: whenSummary(row.when, sourceLabel),
    target: targetOf(row),
    action: actionOf(row),
    actionBrief: actionBriefOf(row),
    widths,
    showSystem,
    menu: menuOf(row),
    busy,
    templateProblem: typeof row.action.params.template_id === 'string'
      ? templateRefProblem(templateRefsById.get(row.action.params.template_id)) : null,
  })

  /* Flat, or Tree mode's folders (`lib/nameTreeRows`) with re-filing off — `dragging: null` is its whole off
     switch; a row's busy state is the page's set, not the tree's one `busyId`. */
  const listRows: FeedRowSpec[] = treeMode
    ? nameTreeRows(buildNameTree(shown), {
        ...NO_REFILING_DEPS,
        kind: AUTOMATION_TREE_KIND,
        leafRow: row => rowOf(row, busyIds.has(row.id)),
        busyId: null,
        expanded: new Set(openAll ? groupPaths(buildNameTree(shown)) : view.expanded),
        onToggleGroup: toggleGroup,
      })
    : shown.map(row => rowOf(row, busyIds.has(row.id)))

  /* THE NON-HAPPY STATES ARE DIFFERENT ANSWERS: a failed fetch must not read "No automations yet". */
  const unreadable = Boolean(error) && rows.length === 0

  return (
    // The query container `contentSlotWidth`'s `cqw` cap and `AUTOMATIONS_NO_META` read: the list's own width, not the window's.
    <Box data-testid="automation-list" sx={AUTOMATIONS_LIST_CONTAINER_SX}>
      {/* Titleless: the Automations tab names the page, so the view toggle and ＋ ride the strip. */}
      <PageHeader
        testId="automations-page-header"
        actions={
          <>
            <PaneModeToggle
              value={treeMode ? 'hierarchy' : 'flat'}
              onChange={next => patchView({ mode: next === 'hierarchy' ? 'tree' : 'flat' })}
            />
            <NewButton label="New automation" to="/automations/new" testId="automation-new" />
          </>
        }
      />

      {gone && <Banner severity="info">That automation no longer exists.</Banner>}

      <FilterBar>
        <FilterRow>
          <FilterSearch value={view.q} onChange={search} placeholder="Name" ariaLabel="Search automations" minWidth={120} />
          <Box sx={UNFOLDED_SX}>
            <FilterSelect
              label="When"
              ariaLabel="Filter by when"
              value={whenKind}
              onChange={setWhenKind}
              allLabel="All"
              options={WHEN_OPTIONS}
              minWidth={120}
            />
          </Box>
          <FilterSelect
            label="Status"
            ariaLabel="Filter by status"
            value={status}
            onChange={setStatus}
            allLabel="All"
            options={STATUS_OPTIONS}
            minWidth={120}
          />
          {ownerFilter && (
            <Box sx={UNFOLDED_SX}>
              <FilterSelect
                label="Owner"
                ariaLabel="Filter by owner"
                value={origin}
                onChange={setOrigin}
                allLabel="All"
                options={ORIGIN_OPTIONS}
                minWidth={120}
              />
            </Box>
          )}
          <Box sx={FOLDED_SX}>
            <ToolbarMenu
              name="Filter automations"
              heading="Filters"
              hint="Filters"
              badgeCount={[whenKind, origin].filter(Boolean).length}
              items={[
                filterRadioPage('automation-filter-when', 'when', 'When', 'All', WHEN_OPTIONS, whenKind, setWhenKind),
                ...(ownerFilter
                  ? [filterRadioPage('automation-filter-owner', 'owner', 'Owner', 'All', ORIGIN_OPTIONS, origin, setOrigin)]
                  : []),
              ]}
              onClear={() => setFilters({ when: '', owner: '', q: view.q })}
            />
          </Box>
        </FilterRow>
      </FilterBar>

      {error && !unreadable && <Banner severity="error">{error}</Banner>}
      {actionError && <Banner severity="error">{actionError}</Banner>}
      {notice && <Banner severity={notice.fired ? 'success' : 'warning'}>{notice.text}</Banner>}

      <ListStates
        loading={loading && data === null}
        skeleton={{ rows: SKELETON_ROWS }}
        failed={unreadable && <LoadFailedState title="Automations unavailable" error={error} onRetry={() => void reload()} />}
        empty={shown.length === 0}
        noMatch={rows.length > 0 && (
          <NoMatchesState
            title={noMatchesTitle({ q: view.q, status, origin, whenKind })}
            onClear={filtered ? clearFilters : undefined}
          />
        )}
        emptyTitle="No automations yet"
        emptyScoped
        feed={{
          ariaLabel: 'Automations',
          testId: 'automations-table',
          sections: [{ key: 'automations', rows: listRows }],
          columns: COLUMNS,
        }}
      />

      {/* Inbound mail arrives through SES or a tenant Gmail, neither of which the community edition has. */}
      {isOperator && isEnabled('tenant_mail') && <UnmatchedEmails />}
      {changeOrg.dialog}
      {confirmDialog}
    </Box>
  )
}
