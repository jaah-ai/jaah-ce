import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type JSX, type KeyboardEvent, type ReactNode } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import ButtonBase from '@mui/material/ButtonBase'
import Checkbox from '@mui/material/Checkbox'
import Chip from '@mui/material/Chip'
import Dialog from '@mui/material/Dialog'
import Popover from '@mui/material/Popover'
import TextField from '@mui/material/TextField'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import useMediaQuery from '@mui/material/useMediaQuery'
import { get } from '../api'
import { useApiRead } from '../hooks/useApiRead'
import { matchesAny } from './FilterRow'
import { ELLIPSIS_SX, PHONE } from './listStyles'
import { NO_AUTOFILL_INPUT } from './noAutofill'
import SegmentedControl from './SegmentedControl'
import { capitalize } from '../lib/humanize'

/* THE TARGET SELECTOR — one control for every kind of thing an automation can run on (Projects, Nodes,
 * and whatever comes next): the kind's ROSTER comes from an adapter, the control itself is identical.
 *
 * A selection is a LIVE RULE, resolved at each firing, not a frozen list: All (ids []), Specific (ids
 * are the members) or All except (ids are the exclusions). "None selected" is Specific with no ids — the
 * empty state, not a fourth mode. A mode switch on the segmented control KEEPS what is currently ticked
 * (Specific <-> All except trade a list for its complement) and only changes how a NEW item is treated;
 * the shortcuts are the resets (Select all -> All, Clear -> Specific [], Except… -> All except []).
 *
 * COLLAPSED it is one line of words a person reads ("3 projects: api, web, docs"). Names are never
 * shredded: the first is whole or middle-ellipsised to at least MIN_VISIBLE_CHARS, a further name joins
 * only while it too keeps that many, the rest fold into "+N more", and the tooltip carries every name.
 *
 * A STORED ID THE ROSTER NO LONGER HOLDS is never dropped: it renders as an error-coloured, named chip
 * (top of the list, and in the summary) and leaves `value` only when someone removes it. */

export type TargetKind = 'projects' | 'nodes'
export type TargetMode = 'all' | 'specific' | 'all_except'
export type TargetId = number | string
export type TargetRule = { kind: TargetKind; mode: TargetMode; ids: TargetId[] }
export type TargetItem = { id: TargetId; name: string; state: 'ok' | 'disabled' | 'offline'; detail?: string }
export type TargetRoster = {
  state: 'loading' | 'error' | 'ready'
  items: TargetItem[]
  error?: string
  retry: () => void
}
export type TargetAdapter = {
  kind: TargetKind
  noun: { one: string; many: string }
  useRoster: (orgId: string | null) => TargetRoster
}
/** The server's resolution of a rule (what a firing would run on now). Optional: without it the control
 *  resolves against its own roster. */
export type TargetPreview = {
  resolved: Array<{ id: TargetId; name: string }>
  resolvedCount: number
  universeCount: number
  dangling: TargetDangling[]
}
/** A stored id the roster does not hold (`removed`), named as it was last known. */
export type TargetDangling = { id: TargetId; label: string; state?: 'removed' }
export type TargetSelectorProps = {
  adapter: TargetAdapter
  orgId: string | null
  value: TargetRule
  onChange: (next: TargetRule) => void
  preview?: TargetPreview
  disabled?: boolean
  label?: string
  /** Under the field when the org has none to pick: the one action that makes one. */
  emptyAction?: ReactNode
}

/** Below this many visible characters a name stops identifying anything (1–4 character stubs identify nothing). */
export const MIN_VISIBLE_CHARS = 12
/** Past this many rows the list draws only what is on screen. */
export const VIRTUALIZE_ABOVE = 200

export function emptyTarget(kind: TargetKind): TargetRule {
  return { kind, mode: 'specific', ids: [] }
}

/** Whether a rule may be saved: All and All except always resolve; Specific needs at least one id. */
export function isValidTarget(rule: TargetRule): boolean {
  return rule.mode !== 'specific' || rule.ids.length > 0
}

/* ---------- Adapters ---------- */

type RosterResponse = { items: TargetItem[]; total: number }

function useTargetRoster(kind: TargetKind, orgId: string | null): TargetRoster {
  const { data, loading, error, reload } = useApiRead(
    ['automation-target-roster', kind, orgId],
    () => get<RosterResponse>(`/api/automations/targets/${kind}?org_id=${encodeURIComponent(orgId ?? '')}`),
    `Couldn’t load ${kind}.`,
    { enabled: orgId !== null },
  )
  const retry = useCallback(() => { void reload() }, [reload])
  // Loading first: a Retry in flight, or a new org's read, is not the previous read's failure.
  if (loading) return { state: 'loading', items: [], retry }
  if (error || data === null) return { state: 'error', items: [], error, retry }
  return { state: 'ready', items: data.items, retry }
}

export const projectsAdapter: TargetAdapter = {
  kind: 'projects',
  noun: { one: 'project', many: 'projects' },
  useRoster: orgId => useTargetRoster('projects', orgId),
}

export const nodesAdapter: TargetAdapter = {
  kind: 'nodes',
  noun: { one: 'node', many: 'nodes' },
  useRoster: orgId => useTargetRoster('nodes', orgId),
}

/* ---------- Pure rule logic (exported for tests) ---------- */

function idKey(id: TargetId): string {
  return `${typeof id}:${id}`
}

/** Whether `item` is ticked under `rule`. */
export function isTicked(rule: TargetRule, id: TargetId): boolean {
  const listed = rule.ids.some(x => idKey(x) === idKey(id))
  return rule.mode === 'all' ? true : rule.mode === 'all_except' ? !listed : listed
}

/** One item ticked or unticked, with the mode rules: All untick -> All except [it]; All except with its
 *  last exclusion re-ticked -> All; Specific stays Specific whatever is ticked. */
export function toggleTarget(rule: TargetRule, id: TargetId): TargetRule {
  const key = idKey(id)
  const without = rule.ids.filter(x => idKey(x) !== key)
  const listed = without.length !== rule.ids.length
  if (rule.mode === 'all') return { ...rule, mode: 'all_except', ids: [id] }
  if (rule.mode === 'all_except') {
    if (!listed) return { ...rule, ids: [...rule.ids, id] }
    return without.length === 0 ? { ...rule, mode: 'all', ids: [] } : { ...rule, ids: without }
  }
  return { ...rule, ids: listed ? without : [...rule.ids, id] }
}

/** A mode picked on the segmented control: the ticked set is kept, only the treatment of new items
 *  changes. Ids the roster no longer holds ride along (removed only explicitly); All has no ids. */
export function switchMode(rule: TargetRule, mode: TargetMode, items: TargetItem[]): TargetRule {
  if (mode === rule.mode) return rule
  if (mode === 'all') return { ...rule, mode, ids: [] }
  const known = new Set(items.map(i => idKey(i.id)))
  const dangling = rule.ids.filter(id => !known.has(idKey(id)))
  const ticked = items.filter(i => isTicked(rule, i.id)).map(i => i.id)
  const unticked = items.filter(i => !isTicked(rule, i.id)).map(i => i.id)
  if (mode === 'specific') return { ...rule, mode, ids: [...dangling, ...ticked] }
  return { ...rule, mode, ids: [...dangling, ...unticked] }
}

/** What a firing would run on, from the control's own roster: the ticked items, minus disabled ones. */
export function resolveLocally(rule: TargetRule, items: TargetItem[]): TargetPreview {
  const known = new Set(items.map(i => idKey(i.id)))
  const resolved = items.filter(i => isTicked(rule, i.id) && i.state !== 'disabled')
  return {
    resolved: resolved.map(({ id, name }) => ({ id, name })),
    resolvedCount: resolved.length,
    universeCount: items.length,
    dangling: rule.mode === 'all'
      ? []
      : rule.ids.filter(id => !known.has(idKey(id))).map(id => ({ id, label: `#${id}` })),
  }
}

/** `removed`: not in the roster (drawn as an error). */
export type SummaryName = { id: TargetId; name: string; removed: boolean }
export type TargetSummary = {
  /** The never-truncated head: "All projects (12)", "3 projects: ", "All nodes except ". */
  lead: string
  names: SummaryName[]
  /** Ticked items that are disabled (skipped at firing). */
  skipped: number
  /** The whole sentence, every name in full — the tooltip and the accessible name. */
  full: string
}

/** The words for a rule. A dangling id is named by `dangling`'s label (else "#id") plus "(removed)". The LAST entry
 *  for an id wins, so a server answer listed after the local guess replaces it. */
export function summarizeTarget(
  rule: TargetRule,
  items: TargetItem[],
  noun: { one: string; many: string },
  dangling: TargetDangling[] = [],
): TargetSummary {
  const byKey = new Map(items.map(i => [idKey(i.id), i]))
  const known = new Map(dangling.map(d => [idKey(d.id), d]))
  const names: SummaryName[] = rule.mode === 'all' ? [] : rule.ids.map(id => {
    const item = byKey.get(idKey(id))
    if (item) return { id, name: item.name, removed: false }
    const entry = known.get(idKey(id))
    return { id, name: entry?.label ?? `#${id}`, removed: true }
  })
  const skipped = items.filter(i => i.state === 'disabled' && isTicked(rule, i.id)).length
  let lead: string
  if (rule.mode === 'all' || (rule.mode === 'all_except' && names.length === 0)) {
    lead = `All ${noun.many} (${items.length})`
  } else if (rule.mode === 'all_except') {
    lead = `All ${noun.many} except `
  } else if (names.length === 0) {
    lead = `No ${noun.many} selected`
  } else {
    lead = `${names.length} ${names.length === 1 ? noun.one : noun.many}: `
  }
  const tail = skipped > 0 ? ` · ${skipped} disabled (skipped)` : ''
  return { lead, names, skipped, full: `${lead}${names.map(n => n.name + removedSuffix(n)).join(', ')}${tail}` }
}

/** `name` cut in the MIDDLE to `visible` characters ("customer-…n-eu-1"): the tail is where siblings
 *  differ, so two cut names stay told apart. */
export function middleEllipsis(name: string, visible: number): string {
  if (visible >= name.length) return name
  const head = Math.ceil(visible * 0.6)
  return `${name.slice(0, head)}…${name.slice(name.length - (visible - head))}`
}

/** A name the roster no longer holds says why; the suffix is never cut. */
export function removedSuffix(name: SummaryName): string {
  return name.removed ? ' (removed)' : ''
}

export const MORE_SEPARATOR = ', '
export function moreLabel(n: number): string {
  return `+${n} more`
}

/** Which names fit `widthPx` on one line (after the lead). The first name always shows — whole, or cut
 *  to no fewer than MIN_VISIBLE_CHARS; a later name joins whole, or cut once to no fewer than
 *  MIN_VISIBLE_CHARS, and only while the "+N more" for the rest still fits; at most one name is cut. */
export function fitNames(
  names: string[],
  widthPx: number,
  measure: (text: string) => number,
  /** Per name, text that follows it and is never cut ("(removed)"). */
  suffixes: string[] = [],
): { shown: string[]; more: number } {
  const shown: string[] = []
  let used = 0
  for (let i = 0; i < names.length; i += 1) {
    const rest = names.length - i - 1
    const sep = i === 0 ? 0 : measure(MORE_SEPARATOR)
    const reserve = rest > 0 ? measure(` ${moreLabel(rest)}`) : 0
    const avail = widthPx - used - sep - reserve
    const suffix = suffixes[i] ?? ''
    const whole = measure(names[i] + suffix)
    if (whole <= avail) {
      shown.push(names[i] + suffix)
      used += sep + whole
      continue
    }
    const cut = fitCut(names[i], avail - measure(suffix), measure)
    if (i === 0) {
      shown.push((cut ?? middleEllipsis(names[i], MIN_VISIBLE_CHARS)) + suffix)
    } else if (cut !== null) {
      shown.push(cut + suffix)
    }
    break
  }
  return { shown, more: names.length - shown.length }
}

/** The longest middle cut of `name` (at least MIN_VISIBLE_CHARS kept) within `avail`, or null. */
function fitCut(name: string, avail: number, measure: (text: string) => number): string | null {
  if (name.length <= MIN_VISIBLE_CHARS) return null
  let lo = MIN_VISIBLE_CHARS
  let hi = name.length - 1
  if (measure(middleEllipsis(name, lo)) > avail) return null
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2)
    if (measure(middleEllipsis(name, mid)) <= avail) lo = mid
    else hi = mid - 1
  }
  return middleEllipsis(name, lo)
}

/* ---------- Measuring (browser only; jsdom measures nothing) ---------- */

let canvas: HTMLCanvasElement | null = null
function textMeasurer(el: HTMLElement | null): ((text: string) => number) | null {
  if (!el || el.clientWidth === 0) return null
  try {
    canvas ??= document.createElement('canvas')
    const ctx = canvas.getContext('2d')
    if (!ctx) return null
    const style = getComputedStyle(el)
    ctx.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`
    return text => ctx.measureText(text).width
  } catch {
    return null
  }
}

/* ---------- The control ---------- */

const MODE_OPTIONS = [
  { value: 'all' as const, label: 'All', testId: 'target-mode-all' },
  { value: 'specific' as const, label: 'Specific', testId: 'target-mode-specific' },
  { value: 'all_except' as const, label: 'All except', testId: 'target-mode-all_except' },
]

/** The panel's full height (search, shortcuts, the 320px list, footer); below this the page scrolls to make room. */
const PANEL_ROOM_PX = 480
/** Kept clear above the trigger when scrolling for room: the field label and the control above it (the type). */
const KEEP_ABOVE_PX = 120
const VIEWPORT_MARGIN_PX = 16

/** Never shorter than this: a panel squeezed to nothing is worse than one that overlaps a little. */
const MIN_PANEL_PX = 240

function spaceBelow(el: HTMLElement): number {
  return Math.max(MIN_PANEL_PX, window.innerHeight - el.getBoundingClientRect().bottom - VIEWPORT_MARGIN_PX)
}

function scrollParent(el: HTMLElement): HTMLElement | null {
  for (let node = el.parentElement; node; node = node.parentElement) {
    const { overflowY } = getComputedStyle(node)
    if ((overflowY === 'auto' || overflowY === 'scroll') && node.scrollHeight > node.clientHeight) return node
  }
  return document.scrollingElement as HTMLElement | null
}

/** The trigger, after scrolling just enough that the panel fits BELOW it: a popover that does not fit is lifted
 *  over whatever sits above the trigger (the Projects/Nodes switch), out of reach until it closes. Never scrolls
 *  the trigger closer than KEEP_ABOVE_PX to the top, so that switch stays in view. */
function roomBelow(trigger: HTMLElement | null): HTMLElement | null {
  if (!trigger) return trigger
  const short = PANEL_ROOM_PX - (window.innerHeight - trigger.getBoundingClientRect().bottom - VIEWPORT_MARGIN_PX)
  const slack = trigger.getBoundingClientRect().top - KEEP_ABOVE_PX
  const by = Math.min(short, slack)
  const parent = by > 0 ? scrollParent(trigger) : null
  if (parent) parent.scrollTop += by
  return trigger
}

export default function TargetSelector({
  adapter,
  orgId,
  value,
  onChange,
  preview,
  disabled = false,
  label,
  emptyAction,
}: TargetSelectorProps): JSX.Element {
  const { noun } = adapter
  const roster = adapter.useRoster(orgId)
  const items = roster.items
  // Below MUI's `sm` (600px) the panel is a full-screen dialog. A plain query: no theme needed.
  const phone = useMediaQuery('(max-width:599.95px)')
  const uid = useId()
  const labelId = `${uid}-label`
  const listId = `${uid}-list`

  /* AC1.2: a rule of another kind is never kept — reset to Specific [] and say why, until the next edit.
     Fired once per (adapter kind, value kind) pair, so a parent that applies it late cannot loop it. */
  const [kindChangedFrom, setKindChangedFrom] = useState<TargetKind | null>(null)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const handledPair = useRef<string | null>(null)
  useEffect(() => {
    if (value.kind === adapter.kind) {
      handledPair.current = null
      return
    }
    const pair = `${value.kind}->${adapter.kind}`
    if (handledPair.current === pair) return
    handledPair.current = pair
    setKindChangedFrom(value.kind)
    onChangeRef.current(emptyTarget(adapter.kind))
  }, [adapter.kind, value.kind])
  const rule: TargetRule = value.kind === adapter.kind ? value : emptyTarget(adapter.kind)
  const showKindReason = kindChangedFrom !== null && !isValidTarget(rule)
  const edit = useCallback((next: TargetRule) => {
    setKindChangedFrom(null)
    onChange(next)
  }, [onChange])

  /* Until the roster answers, nothing can be called removed: every stored id would look dangling. */
  const ready = roster.state === 'ready'

  const local = useMemo(() => resolveLocally(rule, items), [rule, items])
  const resolution = preview ?? local
  const summary = useMemo(() => {
    const words = summarizeTarget(rule, items, noun, [...local.dangling, ...(preview?.dangling ?? [])])
    return ready ? words : { ...words, names: words.names.map(n => ({ ...n, removed: false })) }
  }, [rule, items, noun, local.dangling, preview?.dangling, ready])

  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const open = anchor !== null
  const triggerRef = useRef<HTMLButtonElement>(null)
  const blocked = disabled || orgId === null

  const fieldLabel = label ?? capitalize(noun.many)
  const empty = ready && items.length === 0 && summary.names.length === 0
  const stateText =
    orgId === null
      ? 'Pick the organization first.'
      : roster.state === 'loading'
        ? `Loading ${noun.many}…`
        : roster.state === 'error'
          ? `Couldn’t load ${noun.many}.`
          : empty
            ? `No ${noun.many} yet`
            : null
  // The one panel, housed by a full-screen Dialog on a phone and a Popover otherwise.
  const panelProps = { adapter, roster, rule, onChange: edit, resolution, summary, listId, fieldLabel, onClose: () => setAnchor(null) }

  return (
    <Box sx={{ minWidth: 0, width: '100%' }} data-testid={`target-selector-${adapter.kind}`}>
      <Typography id={labelId} component="div" variant="caption" sx={{ color: 'text.secondary', mb: 0.25 }}>
        {fieldLabel}
      </Typography>
      <Tooltip title={stateText ?? summary.full} placement="bottom-start" describeChild>
        <ButtonBase
          ref={triggerRef}
          disabled={blocked}
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-label={`${fieldLabel}: ${stateText ?? summary.full}`}
          data-testid="target-trigger"
          onClick={() => setAnchor(phone ? triggerRef.current : roomBelow(triggerRef.current))}
          sx={theme => ({
            display: 'flex',
            alignItems: 'center',
            gap: 1,
            width: '100%',
            minWidth: 0,
            minHeight: 40,
            px: 1.5,
            border: 1,
            borderColor: showKindReason ? 'error.main' : 'divider',
            borderRadius: 1,
            textAlign: 'left',
            justifyContent: 'flex-start',
            ...theme.typography.body2,
            '&:hover': { borderColor: 'text.primary' },
            '&.Mui-focusVisible': { outline: `2px solid ${theme.palette.primary.main}`, outlineOffset: -1 },
            '&.Mui-disabled': { color: 'text.disabled' },
          })}
        >
          {stateText !== null ? (
            <Box
              component="span"
              data-testid="target-summary"
              sx={{ ...ELLIPSIS_SX, flex: '1 1 0', color: roster.state === 'error' ? 'error.main' : 'text.secondary' }}
            >
              {stateText}
            </Box>
          ) : (
            <SummaryLine summary={summary} />
          )}
          {stateText === null && (
            <Chip
              size="small"
              label={`${resolution.resolvedCount} of ${resolution.universeCount}`}
              data-testid="target-resolved-chip"
              title={`Will run on ${resolution.resolvedCount} of ${resolution.universeCount} ${noun.many}`}
              /* On a phone the names need the room more; the panel footer says the same. */
              sx={{ flexShrink: 0, height: 22, [PHONE]: { display: 'none' } }}
            />
          )}
          <Box component="span" aria-hidden sx={{ flexShrink: 0, color: 'text.secondary' }}>▾</Box>
        </ButtonBase>
      </Tooltip>
      {orgId !== null && empty && emptyAction && (
        <Typography variant="body2" sx={{ mt: 0.5 }} data-testid="target-empty-action">{emptyAction}</Typography>
      )}
      {showKindReason && (
        <Typography variant="caption" role="status" sx={{ color: 'error.main', display: 'block', mt: 0.25 }}>
          {`Type changed, selection cleared. Pick ${noun.many}.`}
        </Typography>
      )}
      {phone ? (
        <Dialog
          open={open}
          onClose={() => setAnchor(null)}
          fullScreen
          aria-labelledby={labelId}
        >
          <Panel {...panelProps} phone />
        </Dialog>
      ) : (
        <Popover
          open={open}
          anchorEl={anchor}
          onClose={() => setAnchor(null)}
          anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
          transformOrigin={{ vertical: 'top', horizontal: 'left' }}
          slotProps={{
            paper: {
              role: 'dialog',
              'aria-labelledby': labelId,
              sx: {
                width: Math.max(anchor?.clientWidth ?? 0, 360),
                maxWidth: 'calc(100vw - 32px)',
                // Below the trigger only: the panel scrolls rather than rising over the fields above it.
                maxHeight: anchor ? spaceBelow(anchor) : undefined,
              },
            },
          }}
        >
          <Panel {...panelProps} />
        </Popover>
      )}
    </Box>
  )
}

/* THE ONE LINE: lead (never cut), names fitted to the measured width, "+N more", the skipped note. */
function SummaryLine({ summary }: { summary: TargetSummary }): JSX.Element {
  const boxRef = useRef<HTMLSpanElement>(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    const el = boxRef.current
    if (!el) return
    setWidth(el.clientWidth)
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => setWidth(el.clientWidth))
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  const names = summary.names.map(n => n.name)
  const suffixes = summary.names.map(removedSuffix)
  // Unmeasured (jsdom, first paint): every name, the CSS ellipsis as the backstop.
  const measure = width > 0 ? textMeasurer(boxRef.current) : null
  /* The skipped note gives way before a name does: in full, then short, then only in the tooltip. */
  const notes = summary.skipped > 0
    ? [` · ${summary.skipped} disabled (skipped)`, ` · ${summary.skipped} skipped`, '']
    : ['']
  let skippedText = notes[0]
  if (measure && names.length > 0) {
    const firstMin = Math.min(
      measure(names[0] + suffixes[0]),
      measure(middleEllipsis(names[0], MIN_VISIBLE_CHARS) + suffixes[0]),
    )
    const need = firstMin + (names.length > 1 ? measure(` ${moreLabel(names.length - 1)}`) : 0)
    skippedText = notes.find(note => width - measure(summary.lead) - measure(note) - 2 >= need) ?? ''
  } else if (measure) {
    skippedText = notes.find(note => measure(summary.lead + note) + 2 <= width) ?? ''
  }
  const fit = measure
    ? fitNames(names, width - measure(summary.lead) - measure(skippedText) - 2, measure, suffixes)
    : { shown: names.map((n, i) => n + suffixes[i]), more: 0 }

  return (
    <Box
      component="span"
      ref={boxRef}
      data-testid="target-summary"
      sx={{ ...ELLIPSIS_SX, flex: '1 1 0', display: 'block' }}
    >
      {summary.lead}
      {fit.shown.map((name, index) => (
        <Box
          component="span"
          key={idKey(summary.names[index].id)}
          sx={summary.names[index].removed ? { color: 'error.main' } : undefined}
        >
          {index > 0 ? MORE_SEPARATOR : ''}{name}
        </Box>
      ))}
      {fit.more > 0 && (
        <Box component="span" sx={{ color: 'text.secondary' }}>{` ${moreLabel(fit.more)}`}</Box>
      )}
      {skippedText && <Box component="span" sx={{ color: 'text.secondary' }}>{skippedText}</Box>}
    </Box>
  )
}

/* THE EXPANDED PANEL: modes, search, shortcuts, the dangling chips, the checkbox list, the footer. */
function Panel({
  adapter,
  roster,
  rule,
  onChange,
  resolution,
  summary,
  listId,
  fieldLabel,
  onClose,
  phone = false,
}: {
  adapter: TargetAdapter
  roster: TargetRoster
  rule: TargetRule
  onChange: (next: TargetRule) => void
  resolution: TargetPreview
  summary: TargetSummary
  listId: string
  fieldLabel: string
  onClose: () => void
  phone?: boolean
}): JSX.Element {
  const { noun } = adapter
  const items = roster.items
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const [showList, setShowList] = useState(false)
  const searchRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement | null>(null)
  // The virtualizer needs the scroll element as STATE: a ref set after the first render never re-renders it.
  const [scrollEl, setScrollEl] = useState<HTMLDivElement | null>(null)

  const filtered = useMemo(() => items.filter(i => matchesAny(query, i.name)), [items, query])
  useEffect(() => { setActive(a => Math.min(a, Math.max(filtered.length - 1, 0))) }, [filtered.length])

  const virtual = filtered.length > VIRTUALIZE_ABOVE
  const virtualizer = useVirtualizer({
    count: virtual ? filtered.length : 0,
    getScrollElement: () => scrollEl,
    estimateSize: () => 36,
    overscan: 8,
    initialRect: { width: 360, height: 320 },
  })
  useEffect(() => {
    if (virtual) virtualizer.scrollToIndex(active)
    else listRef.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView?.({ block: 'nearest' })
  }, [active, virtual, virtualizer])

  const ready = roster.state === 'ready'
  const removed = ready ? summary.names.filter(n => n.removed) : []
  const [focusList, setFocusList] = useState(false)
  useEffect(() => {
    if (!focusList) return
    setFocusList(false)
    listRef.current?.focus()
  }, [focusList])
  const allTicked = items.length > 0 && items.every(i => isTicked(rule, i.id))

  function toggle(id: TargetId) {
    onChange(toggleTarget(rule, id))
  }
  function removeDangling(id: TargetId) {
    const ids = rule.ids.filter(x => idKey(x) !== idKey(id))
    onChange(rule.mode === 'all_except' && ids.length === 0 ? { ...rule, mode: 'all', ids: [] } : { ...rule, ids })
  }
  function except() {
    onChange({ ...rule, mode: 'all_except', ids: [] })
    setActive(0)
    // After the re-render: the list may only now be drawn.
    setFocusList(true)
  }

  function onListKey(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActive(a => Math.min(a + 1, filtered.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      if (active === 0) searchRef.current?.focus()
      else setActive(a => a - 1)
    } else if (event.key === 'Home') {
      event.preventDefault()
      setActive(0)
    } else if (event.key === 'End') {
      event.preventDefault()
      setActive(Math.max(filtered.length - 1, 0))
    } else if (event.key === ' ') {
      event.preventDefault()
      const item = filtered[active]
      if (item) toggle(item.id)
    } else if (event.key === 'Enter') {
      event.preventDefault()
      onClose()
    } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      // Type to filter: the keystroke goes to the search box.
      event.preventDefault()
      setQuery(q => q + event.key)
      searchRef.current?.focus()
    }
  }

  function onSearchKey(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'ArrowDown' && filtered.length > 0) {
      event.preventDefault()
      listRef.current?.focus()
    } else if (event.key === 'Enter') {
      event.preventDefault()
      onClose()
    }
  }

  function row(item: TargetItem, index: number, top?: number) {
    const ticked = isTicked(rule, item.id)
    const optionId = `${listId}-opt-${index}`
    return (
      <Box
        key={idKey(item.id)}
        id={optionId}
        role="option"
        aria-selected={ticked}
        aria-setsize={filtered.length}
        aria-posinset={index + 1}
        data-index={index}
        data-active={index === active}
        data-testid={`target-option-${item.id}`}
        onMouseDown={event => event.preventDefault()}
        onClick={() => { setActive(index); toggle(item.id); listRef.current?.focus() }}
        title={item.name}
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 1,
          minHeight: 36,
          pr: 1.5,
          cursor: 'pointer',
          typography: 'body2',
          '&:hover': { bgcolor: 'action.hover' },
          ...(top === undefined ? {} : { position: 'absolute', top: 0, left: 0, right: 0, transform: `translateY(${top}px)` }),
        }}
      >
        <Checkbox
          size="small"
          checked={ticked}
          tabIndex={-1}
          disableRipple
          sx={{ pointerEvents: 'none', py: 0.5 }}
          slotProps={{ input: { 'aria-hidden': true, tabIndex: -1 } }}
        />
        <Box component="span" sx={{ ...ELLIPSIS_SX, flex: 1 }}>{item.name}</Box>
        {/* The detail ("Offline — its task waits for it") already names the state: never both. */}
        {item.state !== 'ok' && !item.detail && (
          <Typography component="span" variant="caption" sx={{ color: 'text.secondary', flexShrink: 0 }}>
            {item.state}
          </Typography>
        )}
        {item.detail && (
          <Typography component="span" variant="caption" sx={{ color: 'text.secondary', flexShrink: 0 }}>
            {item.detail}
          </Typography>
        )}
      </Box>
    )
  }

  // Only an option that is in the DOM may be named (a virtualised one may not be drawn yet).
  const activeDrawn = !virtual || virtualizer.getVirtualItems().some(v => v.index === active)
  const activeOptionId = filtered.length > 0 && activeDrawn ? `${listId}-opt-${active}` : undefined

  return (
    <Box
      sx={{ display: 'flex', flexDirection: 'column', gap: 1, p: 1.5, minWidth: 0, height: phone ? '100%' : undefined }}
      data-testid="target-panel"
    >
      {phone && (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <Typography variant="subtitle1" sx={{ flex: 1 }}>{fieldLabel}</Typography>
          <Button size="small" variant="contained" onClick={onClose}>Done</Button>
        </Box>
      )}
      <SegmentedControl<TargetMode>
        value={rule.mode}
        options={MODE_OPTIONS}
        onChange={mode => onChange(switchMode(rule, mode, items))}
        disabled={!ready}
        ariaLabel="Mode"
        color="primary"
        fullWidth
      />
      <TextField
        inputRef={searchRef}
        autoFocus
        size="small"
        placeholder={`Search ${noun.many}`}
        value={query}
        onChange={event => { setQuery(event.target.value); setActive(0) }}
        onKeyDown={onSearchKey}
        slotProps={{
          htmlInput: {
            ...NO_AUTOFILL_INPUT,
            'aria-label': `Search ${noun.many}`,
            'aria-controls': listId,
            'data-testid': 'target-search',
          },
        }}
      />
      <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
        <Button size="small" onClick={() => onChange({ ...rule, mode: 'all', ids: [] })} disabled={!ready} data-testid="target-select-all">
          Select all
        </Button>
        <Button size="small" onClick={() => onChange({ ...rule, mode: 'specific', ids: [] })} disabled={!ready} data-testid="target-clear">
          Clear
        </Button>
        <Button size="small" onClick={except} disabled={!ready} data-testid="target-except">Except…</Button>
      </Box>
      {rule.mode === 'specific' && allTicked && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }} data-testid="target-hint">
          Use All to include new ones.
        </Typography>
      )}
      {removed.length > 0 && (
        <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }} data-testid="target-dangling">
          {removed.map(n => (
            <Chip
              key={idKey(n.id)}
              size="small"
              color="error"
              label={`${n.name}${removedSuffix(n)}`}
              title={`${n.name}: no longer available. Remove it, or its firings fail.`}
              onDelete={() => removeDangling(n.id)}
              data-testid={`target-dangling-${n.id}`}
              sx={{ maxWidth: '100%' }}
            />
          ))}
        </Box>
      )}
      {roster.state === 'loading' ? (
        <Typography variant="body2" sx={{ color: 'text.secondary', py: 1 }} role="status" data-testid="target-loading">
          {`Loading ${noun.many}…`}
        </Typography>
      ) : roster.state === 'error' ? (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, py: 0.5 }} role="alert" data-testid="target-error">
          <Typography variant="body2" sx={{ color: 'error.main', flex: 1 }}>
            {`Couldn’t load ${noun.many}.`}
          </Typography>
          <Button size="small" onClick={roster.retry}>Retry</Button>
        </Box>
      ) : items.length === 0 ? (
        <Typography variant="body2" sx={{ color: 'text.secondary', py: 1 }} data-testid="target-empty">
          {`No ${noun.many} yet`}
        </Typography>
      ) : (
        <>
          {filtered.length === 0 && (
            <Typography variant="body2" sx={{ color: 'text.secondary', py: 1 }} data-testid="target-no-match">
              {`No matching ${noun.one}`}
            </Typography>
          )}
          <Box
            ref={(el: HTMLDivElement | null) => { listRef.current = el; setScrollEl(el) }}
            id={listId}
            role="listbox"
            aria-multiselectable
            aria-label={capitalize(noun.many)}
            aria-activedescendant={activeOptionId}
            tabIndex={0}
            onKeyDown={onListKey}
            data-testid="target-list"
            sx={{
              listStyle: 'none',
              m: 0,
              p: 0,
              overflowY: 'auto',
              maxHeight: phone ? undefined : 320,
              flex: phone ? 1 : undefined,
              border: 1,
              borderColor: 'divider',
              borderRadius: 1,
              position: 'relative',
              display: filtered.length === 0 ? 'none' : 'block',
              '&:focus-visible': { outline: 'none' },
              /* The active row is marked only while the list has focus: the keyboard cursor, not a choice. */
              '&:focus [data-active="true"]': {
                bgcolor: 'action.hover',
                outline: '2px solid',
                outlineColor: 'primary.main',
                outlineOffset: -2,
              },
            }}
          >
            {virtual ? (
              <Box role="presentation" sx={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
                {virtualizer.getVirtualItems().map(v => row(filtered[v.index], v.index, v.start))}
              </Box>
            ) : (
              filtered.map((item, index) => row(item, index))
            )}
          </Box>
        </>
      )}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }} data-testid="target-footer">
        <Typography variant="body2" sx={{ flex: 1 }}>
          {`Will run on ${resolution.resolvedCount} of ${resolution.universeCount} ${noun.many}`}
        </Typography>
        <Button
          size="small"
          aria-expanded={showList}
          onClick={() => setShowList(s => !s)}
          disabled={!showList && resolution.resolvedCount === 0}
        >
          {showList ? 'Hide list' : 'Show list'}
        </Button>
        {!phone && <Button size="small" variant="contained" onClick={onClose}>Done</Button>}
      </Box>
      {showList && (
        <Box
          component="ul"
          data-testid="target-resolved-list"
          sx={{ m: 0, pl: 2.5, maxHeight: 160, overflowY: 'auto', typography: 'body2' }}
        >
          {resolution.resolved.map(r => (
            <Box component="li" key={idKey(r.id)} sx={{ overflowWrap: 'anywhere' }}>{r.name}</Box>
          ))}
        </Box>
      )}
    </Box>
  )
}
