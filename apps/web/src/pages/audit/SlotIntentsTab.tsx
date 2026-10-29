import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { FilterRow, FilterSelect, vocabularyOptions, withUnmatched } from '../../ui/FilterRow'
import { humanDuration } from '../../lib/relativeTime'
import { useEventStream } from '../../hooks/useEventStream'
import { useLiveRefetch } from '../../hooks/useLiveRefetch'
import { useApiRead } from '../../hooks/useApiRead'
import { usePagedLogFilters } from '../../hooks/usePagedLogFilters'
import { useUrlFilters } from '../../hooks/useUrlFilters'
import Banner from '../../ui/Banner'
import NoMatchesState from '../../ui/NoMatchesState'
import type { FeedRowSpec } from '../../ui/feed/types'
import FilterBar from '../../ui/FilterBar'
import StatusChip, { type Tone } from '../../ui/StatusChip'
import { PHONE } from '../../ui/listStyles'
import { PageNav } from '../../ui/Pager'
import ListStates from '../../ui/feed/ListStates'
import { usePageClamp } from '../../hooks/usePageClamp'
import { Stat, StatGrid } from '../../ui/StatGrid'
import {
  listFleetIntents,
  type FleetIntentPage,
  type FleetIntentRow,
  type IntentQueueCounts,
  type IntentSet,
} from './intentsApi'

/* AUDIT > NODES > SLOT INTENTS — the fleet's slot-intent queue, and the wedge detector.
 * An intent is an order the CP places against one pool SLOT (lease, release, stop its stack), minted by a
 * 60s decision pass and drained by a ~5s pool tick; a healthy queue is near-empty, so anything minutes old is
 * stuck. Fleet-wide (not per node) because the question is WHERE work is stuck; the node lens narrows it.
 * NO TIME ARITHMETIC AND NO JUDGEMENT: `state`, `age_seconds`, `withheld_seconds` and the recovered refusal
 * halves are server-derived (a skewed browser clock would misread them); `humanDuration` only formats.
 * PERMISSION: `GET /api/nodes/intents` is `system:view`, like the command log beside it, so no second gate.
 * Node grants still scope it: "the fleet" means the fleet THIS READER may see.
 */

/* ── The vocabularies, and where each one's meaning is settled ───────────────────────────────────── */

/* The retiring-outcome vocabulary (`apps/api/app/fleet_verdicts.py`), toned. `parked` is `error`: the CP's
 * record that a row refused until its budget ran out and was put before a human. `superseded` is
 * neutral bookkeeping. Unlisted outcomes fall to neutral (`ui/statusTone.ts`'s contract). This file is the
 * map's home. */
const OUTCOME_TONE: Record<string, Tone> = {
  done: 'success',
  accepted: 'success',
  refused_stale: 'warning',
  refused_precondition: 'warning',
  failed: 'error',
  unsupported_verb: 'error',
  superseded: 'neutral',
  parked: 'error',
}

/** The server's one-word answer for why a row is not moving. */
const STATE_TONE: Record<FleetIntentRow['state'], Tone> = {
  due: 'info',
  withheld: 'warning',
  retired: 'neutral',
}

/* The disc's glyphs (no `badge-*` tokens exist, so `toneFor` cannot answer). Chevron on its way, ellipsis held
 * back, filled square over; retired — tick succeeded, slashed ring refused at a gate, cross a genuine failure,
 * question mark an unknown verb, return arrow withdrawn by the CP, double bang `parked`. Unlisted → `·`. */
const STATE_GLYPH: Record<FleetIntentRow['state'], string> = {
  due: '»',
  withheld: '⋯',
  retired: '■',
}

const OUTCOME_GLYPH: Record<string, string> = {
  done: '✓',
  accepted: '✓',
  refused_stale: '⊘',
  refused_precondition: '⊘',
  failed: '✕',
  unsupported_verb: '?',
  superseded: '↺',
  parked: '‼',
}

/** A one-option `FilterSelect`, not a toggle, so at 390px `FilterRow` packs it two per line with its peers. */
const REFUSING_OPTIONS = [{ value: 'yes', label: 'Refused at least once' }]

/** `retired` is absent: the server 422s it (`_INTENT_STATES` in routes/nodes/intents.py is the single home). */
const STATE_OPTIONS = [
  { value: 'due', label: 'Due now' },
  { value: 'withheld', label: 'Withheld' },
]

/* Per-set copy. The PENDING empty state is a positive statement (a blank reads as a failed fetch). The
 * RETIRED one is bounded by the purge and points at the retention note: empty means "nothing recently",
 * NEVER "nothing ever". Under a node filter the panel says so separately (`NODE_SCOPE_NOTE`). */
const INTENT_SET_COPY: Record<IntentSet, {
  ariaLabel: string
  testId: string
  rowTestId: string
  emptyTitle: string
}> = {
  pending: {
    ariaLabel: 'Pending intents',
    testId: 'intents-pending-table',
    rowTestId: 'intent-pending',
    emptyTitle: 'Queue empty — nothing is stuck',
  },
  retired: {
    ariaLabel: 'Recently retired intents',
    testId: 'intents-retired-table',
    rowTestId: 'intent-retired',
    emptyTitle: 'Nothing retired in the retention window',
  },
}

/* ── Layout ────────────────────────────────────────────────────────────────────────────────────────
 * Row geometry is `ui/feed/FeedList`'s. The REFUSAL SENTENCE (unbounded; names WHICH gate refuses) is the row's
 * note (`FeedRowSpec.notes`): its hover, and a tap on its disc on a phone. The rules below are this panel's prose and
 * controls. */
/* THE READABLE MEASURE for every explanatory sentence here: full-width lines ran ~163 characters, past the
 * ~90 where reading turns to skimming. `ch` tracks the type scale; a MAXIMUM, so narrow containers win. */
const PROSE_SX = { maxWidth: '78ch' } as const

/* ONE INTENT SUBSECTION (`Pending`, `Recently retired`) as a labelled zone, so it is clear where each set
 * stops. THE RULE IS ON THE SECOND ZONE ONLY, as a second constant: `:first-of-type` would match the banner
 * and stat-strip `div`s sharing the stack. */
const INTENT_SET_SX = {
  display: 'flex',
  flexDirection: 'column',
  gap: 0.5,
} as const

/** The second zone only: the first sits under the scope note, and two close rules read as a mistake. */
const INTENT_SET_DIVIDED_SX = {
  ...INTENT_SET_SX,
  pt: 1,
  mt: 0.5,
  borderTop: 1,
  borderColor: 'divider',
} as const

/* The filter strip's width rule, from the call site (`ui/FilterBar` is shared and frozen). Measured at 390px:
 * selects rendered 150/130/206px, a ragged staircase; equal widths fix it. Wider, `flex: 1 1 150px` shares
 * spare width while keeping each control's floor. */
const FILTER_STRIP_SX = {
  '& .MuiFormControl-root': { flex: '1 1 150px', maxWidth: 240 },
  [PHONE]: { '& .MuiFormControl-root': { flex: '1 1 100%', maxWidth: 'none' } },
} as const

/* `intentRowSpec` below is the ONE place a row is described. */

/* ONE PAGED, FILTERED LIST, INSTANTIATED TWICE — once per set (they differ by the state-vs-outcome disc and
 * which filters apply).
 * `refreshKey` IS THE PANEL'S SSE TICK, threaded rather than re-subscribed, so both tables and the stat strip
 * describe the queue at ONE instant.
 * `node` IS THE PANEL'S FILTER, threaded too: it narrows the whole panel (tables, banners, stats), and it is
 * the one filter in the URL — the finding an operator pastes into an incident channel. */
function IntentTable({
  set,
  node,
  refreshKey,
  onPage,
}: {
  set: IntentSet
  /** The panel-level node lens, or `''` for the whole fleet. */
  node: string
  /** Bumped on every `node` event; 0 on mount, so the live effect skips a duplicate first fetch. */
  refreshKey: number
  /** Reports landed pages up, so the panel gets the summary and node options without a third request. */
  onPage: (page: FleetIntentPage) => void
}) {
  const copy = INTENT_SET_COPY[set]
  /* EVERY FILTER CHANGE RESETS TO PAGE 1, through one helper: staying on a page past the end reads as "no
     such rows". */
  const { page, setPage, narrow: applyFilter } = usePagedLogFilters(1)
  const [verb, setVerb] = useState('')
  const [outcome, setOutcome] = useState('')
  const [state, setState] = useState('')
  const [refusing, setRefusing] = useState('')

  /* THE NODE LENS RESETS THE PAGE TOO (it bypasses `applyFilter`). ADJUSTED DURING RENDER, NOT IN AN EFFECT:
     an effect runs after `filters` is rebuilt, so a request for the stale page would fire before the reset's.
     React re-runs the render immediately, so the first request under the new lens is the right one. */
  const lensRef = useRef(node)
  if (lensRef.current !== node) {
    lensRef.current = node
    setPage(1)
  }

  /* The request, built in one place: untouched it is `set=<set>&page=1` and NOTHING else. Unset filters are
     OMITTED (`./intentsApi.ts`). */
  const filters = useMemo(() => ({
    set,
    page,
    verb,
    outcome,
    state,
    refusing: refusing !== '',
    node,
  }), [set, page, verb, outcome, state, refusing, node])

  /* The hook's generation fence: a stale winner would show a past queue as current. On failure the payload is
     NOT cleared: the last good reading under a stale banner beats a skeleton. */
  const { data, loading, error, refresh } = useApiRead(
    ['slot-intents', filters],
    () => listFleetIntents(filters),
    'Failed to load the intent queue',
  )
  useEffect(() => {
    if (data) onPage(data)
  }, [data, onPage])

  /* THE LIVE REFETCH, WITHOUT THE SKELETON (a near-identical redraw needs no flash). `refreshKey` 0 skips the
     mount pass. `refresh` IS DELIBERATELY NOT A DEPENDENCY — it changes on every filter edit and would make this
     a duplicate of the hook's own read. */
  useEffect(() => {
    if (refreshKey === 0) return
    void refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey])

  const total = data?.total ?? 0
  const per = data?.per ?? 0
  /* Next is disabled on the known last page (`total`). The set can shrink under the operator (filter, lens,
     failures ageing out of the purge) — the hook clamps back. */
  const totalPages = usePageClamp(page, setPage, total, per)

  /* Options from the server's scan of the set, never a client copy of `fleet_verdicts`. The ACTIVE value is
     unioned in so an applied filter never vanishes from its own control. */
  const verbOptions = useMemo(() => vocabularyOptions(data?.verbs ?? [], verb, { sort: true }), [data?.verbs, verb])
  const outcomeOptions = useMemo(
    () => vocabularyOptions(data?.outcomes ?? [], outcome, { sort: true }),
    [data?.outcomes, outcome],
  )

  const rows = data?.items ?? []
  const filtered = Boolean(verb || outcome || state || refusing)

  /* THE QUEUE AS FEED ROWS. State/outcome is the MARK (`data-status` keeps the retired pill's string); slot is
     the LEAD; the title is the VERB plus the countdown as VISIBLE TEXT (no hover on phones). THE REASON IS A
     NOTE carrying the refusal's CAUSE (it names a remedy); the COUNT stays in the cluster at
     `gate: 'meta'`. Node is `gate: 'id'` and a LINK, allowed only because every row's ⋯ carries the same
     destination. Age is ungated. `humanDuration` REMAINS THE ONLY ARITHMETIC. */
  const intentRows: FeedRowSpec[] = rows.map((row): FeedRowSpec => {
    /* THE REFUSAL'S DETAIL WINS OVER `reason`: a re-minted slot carries the retry budget forward
       (`supersede_pending_intents` → `lease_drain`), so its `reason` is the mint's unrelated sentence.
       ONCE THERE IS A REFUSAL, `reason` IS NEVER SUBSTITUTED: `refusal_key` is `f"{outcome}\n{detail}"`, and an
       empty detail reads `no detail reported`. */
    const cause = row.refusals > 0 ? row.refusal_outcome : null
    const detail = cause !== null ? row.refusal_detail : row.reason
    const detailText = detail ?? (cause !== null ? 'no detail reported' : null)
    // The refusal's cause names its remedy, so it leads the sentence.
    const reasonText = detailText && cause ? `${cause}: ${detailText}` : detailText

    /* Separates resuming-in-seconds from parked-after-failures (same mark); positive only while withheld
       (`IntentRow.withheld_seconds`). */
    const waiting = row.withheld_seconds !== null && row.withheld_seconds > 0
      ? `retries in ${humanDuration(row.withheld_seconds)}`
      : null

    /* `unknown`, not an em-dash: unreachable (the retired set is `outcome IN (retiring)`), but it matches what
       `_intent_queue` counts an absent outcome under. */
    const retiredOutcome = row.outcome ?? 'unknown'
    const stateWord = set === 'pending' ? row.state : retiredOutcome

    return {
      key: String(row.id),
      indicator: {
        tone: set === 'pending'
          ? (STATE_TONE[row.state] ?? 'neutral')
          : (OUTCOME_TONE[retiredOutcome] ?? 'neutral'),
        glyph: set === 'pending'
          ? (STATE_GLYPH[row.state] ?? '·')
          : (OUTCOME_GLYPH[retiredOutcome] ?? '·'),
        label: stateWord,
        title: waiting ? `${stateWord} — ${waiting}` : stateWord,
        data: { status: set === 'pending' ? row.state : `outcome-${retiredOutcome}` },
      },
      lead: { text: row.slot, width: 78, title: `Slot ${row.slot}`, align: 'left' },
      title: waiting ? `${row.verb} — ${waiting}` : row.verb,
      /* NAMES THE HOST: the node slot is gated at `id`, so a phone row would otherwise lack it. */
      titleText: `${row.node} ${row.slot} ${row.verb}${waiting ? ` — ${waiting}` : ''}`,
      ariaLabel: `${row.verb} ${row.slot} on ${row.node} — ${stateWord}${waiting ? `, ${waiting}` : ''}`
        + (reasonText ? `. ${reasonText}` : ''),
      data: { 'intent-id': row.id, set },
      slots: [
        {
          key: 'refusals',
          width: 52,
          gate: 'meta',
          align: 'right',
          mono: true,
          testId: 'intent-refusals',
          /* THE COUNT ONLY (the cause is the row's note). The ack rewrites `outcome` to `parked` once the
             budget is spent, erasing what the row refused with. */
          title: row.refusals > 0
            ? `Refused ${row.refusals} time${row.refusals === 1 ? '' : 's'}`
            : 'Never refused',
          node: row.refusals > 0 ? `${row.refusals}×` : null,
        },
        {
          key: 'node',
          width: 96,
          gate: 'id',
          mono: true,
          testId: 'intent-node',
          title: `Open ${row.node}`,
          /* ALWAYS A LINK: shown by name, addressed by uuid (`/nodes/{id}`). */
          node: (
            <Link
              component={RouterLink}
              to={`/nodes/${encodeURIComponent(row.node_id)}`}
              underline="hover"
              data-testid="intent-node-link"
            >
              {row.node}
            </Link>
          ),
        },
        {
          key: 'age',
          width: 52,
          mono: true,
          dim: true,
          testId: 'intent-age',
          title: `Queued ${humanDuration(row.age_seconds)} ago${waiting ? ` — ${waiting}` : ''}`,
          node: humanDuration(row.age_seconds),
        },
      ],
      /* THE NODE SLOT'S SECOND HOME, which is what permits `gate: 'id'` (`ui/feed/types.ts`): below 768px a
         phone reader of a PARKED intent must still reach its host. Never conditional. `to`, not `href` (an
         `href` item opens a new tab). */
      actions: [{
        key: 'node',
        icon: '↗',
        label: `Open ${row.node}`,
        /* Names destination and row (as `components/WorktreePool.tsx`), not a generic "Open node". */
        ariaLabel: `Open node ${row.node}, host of slot ${row.slot}`,
        to: `/nodes/${encodeURIComponent(row.node_id)}`,
      }],
      /* WHY THIS ROW IS NOT MOVING, on its hover. `error` once refused (a spending retry budget is the alarm); a
         bare `reason` with no refusal is bookkeeping and reads `muted`. */
      notes: reasonText ? [{ key: 'reason', tone: cause !== null ? 'error' : 'muted', text: reasonText }] : undefined,
    }
  })


  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
      {error && (
        <Banner severity="error" testId={`${copy.rowTestId}s-error`}>
          {`${error} — showing the last reading.`}
        </Banner>
      )}

      {/* `mt: 1`: the outlined labels rise above the box; at the column's 4px gap they touched the count chips
          line above ("By verb:", "Outcomes:"). */}
      <Box sx={[FILTER_STRIP_SX, { mt: 1 }]}>
        <FilterBar>
          <FilterRow>
            <FilterSelect
              label="Verb"
              ariaLabel={`Filter ${set} intents by verb`}
              value={verb}
              onChange={next => applyFilter(() => setVerb(next))}
              allLabel="All verbs"
              options={verbOptions}
              minWidth={150}
            />
            {set === 'pending' ? (
              <FilterSelect
                label="State"
                ariaLabel="Filter pending intents by state"
                value={state}
                onChange={next => applyFilter(() => setState(next))}
                allLabel="Any state"
                options={STATE_OPTIONS}
                minWidth={150}
              />
            ) : (
              <FilterSelect
                label="Outcome"
                ariaLabel="Filter retired intents by outcome"
                value={outcome}
                onChange={next => applyFilter(() => setOutcome(next))}
                allLabel="Any outcome"
                options={outcomeOptions}
                minWidth={150}
              />
            )}
            <FilterSelect
              label="Refusals"
              ariaLabel={`Filter ${set} intents by refusals`}
              value={refusing}
              onChange={next => applyFilter(() => setRefusing(next))}
              allLabel="Any"
              options={REFUSING_OPTIONS}
              minWidth={180}
            />
          </FilterRow>
        </FilterBar>
      </Box>

      {/* `page > totalPages`: the clamp has queued a correction — hold the skeleton, not a false "no match".
          TWO DIFFERENT ANSWERS (via `total_unfiltered`): "nothing is queued" vs "nothing matches your filter".
          `copy.ariaLabel` / `copy.testId` keep "Pending intents" / "Recently retired intents" for suites and AT. */}
      <ListStates
        loading={loading || page > totalPages}
        skeleton={{ rows: 3 }}
        empty={rows.length === 0}
        noMatch={filtered && (data?.total_unfiltered ?? 0) > 0 && <NoMatchesState />}
        emptyTitle={copy.emptyTitle}
        feed={{ ariaLabel: copy.ariaLabel, testId: copy.testId, sections: [{ key: set, rows: intentRows }] }}
      />

      <PageNav
        testId={`${copy.rowTestId}s-pagination`}
        page={page}
        setPage={setPage}
        totalPages={totalPages}
        label={`Page ${page} of ${totalPages} — ${total} row${total === 1 ? '' : 's'}`}
      />
    </Box>
  )
}

/** A labelled strip of `key count` chips (`data-status="<prefix>-<key>"`), or "none"; unlisted in `tones` is neutral. */
function CountChips({ testId, label, prefix, counts, tones }: {
  testId: string; label: string; prefix: string; counts: [string, number][]; tones?: Record<string, Tone>
}) {
  return (
    <Box data-testid={testId} sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 0.5 }}>
      <Typography variant="caption" component="span" sx={{ color: 'text.secondary' }}>{label}</Typography>
      {counts.length === 0 ? (
        <Typography variant="caption" component="span" sx={{ color: 'text.secondary' }}>none</Typography>
      ) : (
        counts.map(([key, count]) => (
          <StatusChip key={key} label={`${key} ${count}`} tone={tones?.[key] ?? 'neutral'} data-status={`${prefix}-${key}`} />
        ))
      )}
    </Box>
  )
}

export default function SlotIntentsTab({ enabled = true }: {
  /* Whether the queue is wanted yet: `TabPanel` keeps this mounted behind other tabs. LATCHED BY THE CALLER
     (false→true only), so a sibling-tab trip keeps filters and pages. */
  enabled?: boolean
}) {
  /* THE NODE LENS LIVES IN THE URL, READ-THROUGH (the only filter that does): it is the shareable finding.
     No mirrored state to drift, and Back un-narrows. */
  const [searchParams, setFilters] = useUrlFilters()
  const node = searchParams.get('node') ?? ''

  /* Summary and node vocabulary from the tables' own pages — a third request could disagree with them.
     `null` until the first page lands (the stat strip's skeleton). */
  const [summary, setSummary] = useState<IntentQueueCounts | null>(null)

  /* THE NODE OPTIONS COME STRAIGHT OFF THE PAYLOAD: `nodes` is computed WITHOUT the node scope (a dropdown
     must not eat its own options), unlike `verbs`/`outcomes`; `IntentPageOut.nodes` (routes/nodes/intents.py) is the
     single home. HELD PER SET AND UNIONED: each list names nodes with rows in that set. */
  const [nodesBySet, setNodesBySet] = useState<Record<IntentSet, FleetIntentPage['node_options']>>({ pending: [], retired: [] })

  const onPage = useCallback((page: FleetIntentPage) => {
  /* THE SUMMARY IS THE PENDING PAGE'S, deterministically: both carry the same `counts`
     (`IntentPageOut.counts`, not scoped by `set`), and "whichever landed last" would flip if that changed. */
    if (page.set === 'pending') setSummary(page.counts)
    const options = page.node_options ?? []
    setNodesBySet(prev => (prev[page.set] === options ? prev : { ...prev, [page.set]: options }))
  }, [])

  /* ONE SUBSCRIPTION FOR THE WHOLE PANEL (`acquireStream` refcounts one `EventSource` per path), so tables and
     summary move on the SAME event. `node` is what the CP publishes when occupancy or an intent outcome moves. */
  const [refreshKey, setRefreshKey] = useState(0)
  useLiveRefetch(useEventStream('/api/stream', ['node']), () => setRefreshKey(k => k + 1), null)

  /* The ACTIVE value is unioned in: a pasted link's node may have no rows. Valued by uuid, labelled by name. */
  const nodeOptions = useMemo(() => {
    const byId = new Map([...nodesBySet.pending, ...nodesBySet.retired].map(o => [o.id, o.name]))
    const options = [...byId].map(([value, label]) => ({ value, label })).sort((a, b) => a.label.localeCompare(b.label))
    return withUnmatched(options, node)
  }, [nodesBySet, node])

  if (!enabled) return null

  const parked = summary?.recent_outcomes.parked ?? 0
  const refusing = summary?.refusing ?? 0
  const verbs = Object.entries(summary?.by_verb ?? {})
  const outcomes = Object.entries(summary?.recent_outcomes ?? {})

  return (
    /* `spacing={2}`: at 1 the FilterBar's outlined labels touched the banner above (Sessions' fix). */
    <Stack spacing={2} sx={{ pt: 1 }}>
      {/* Two numbers get their own banner for scannability: `refusing` (the park budget being spent now) and
          `parked` (already spent, a human gate open). Both also sit in the strip. */}
      {parked > 0 && (
        <Banner severity="error" testId="intents-parked-banner">
          {`${parked} intent${parked === 1 ? ' has' : 's have'} been PARKED — refused until the retry budget ran out, which retires the row for a human. Nothing will unstick these on its own; each row names its Node slot — in its ⋯ menu on a narrow screen.`}
        </Banner>
      )}
      {refusing > 0 && (
        <Banner severity="warning" testId="intents-refusing-banner">
          {`${refusing} pending intent${refusing === 1 ? '' : 's'} ${refusing === 1 ? 'has' : 'have'} refused at least once. Each refusal spends the park budget; when it runs out a human gate opens.`}
        </Banner>
      )}

      {/* THE NODE LENS SITS ABOVE BOTH SETS, ALONE: everything below is scoped by it. */}
      <Box sx={FILTER_STRIP_SX}>
        <FilterBar>
          <FilterRow>
            <FilterSelect
              label="Node"
              ariaLabel="Filter intents by node"
              value={node}
              onChange={next => setFilters({ node: next })}
              allLabel="All nodes"
              options={nodeOptions}
              minWidth={180}
            />
          </FilterRow>
        </FilterBar>
      </Box>

      {node && (
        <Typography
          data-testid="intents-scope-note"
          variant="caption"
          component="p"
          sx={[PROSE_SX, { color: 'text.secondary' }]}
        >
          {`Scoped to ${nodeOptions.find(o => o.value === node)?.label ?? node}.`}
        </Typography>
      )}

      {/* `maxWidth` so the five numbers do not scatter across a wide page. */}
      <StatGrid sx={{ maxWidth: 720 }}>
        <Stat label="Pending" value={String(summary?.pending ?? 0)} testId="intents-stat-pending" />
        <Stat label="Due now" value={String(summary?.due ?? 0)} testId="intents-stat-due" />
        <Stat label="Withheld" value={String(summary?.withheld ?? 0)} testId="intents-stat-withheld" />
        <Stat label="Refusing" value={String(refusing)} testId="intents-stat-refusing" />
        <Stat
          label="Oldest pending"
          value={summary == null || summary.oldest_pending_age_seconds === null
            ? '—'
            : humanDuration(summary.oldest_pending_age_seconds)}
          testId="intents-stat-oldest"
        />
      </StatGrid>

      {/* ── Pending ────────────────────────────────────────────────────────────────────────────────
          Two headings at the same level: the two sets are halves of one object, not main + appendix. */}
      <Box sx={INTENT_SET_SX}>
        <Typography variant="subtitle2" component="h3" sx={{ fontWeight: 600 }}>
          Pending
        </Typography>

        {/* `by_verb` counts the PENDING set only (retired rows can outnumber it); the heading above already says
            `Pending`. */}
        <CountChips testId="intents-by-verb" label="By verb:" prefix="verb" counts={verbs} />

        <IntentTable set="pending" node={node} refreshKey={refreshKey} onPage={onPage} />
      </Box>

      {/* ── Recent history ─────────────────────────────────────────────────────────────────────────
          The note is REQUIRED: `purge_unexecutable_intents` deletes retired rows past `retention_seconds`, so
          an empty history must never read as "nothing ever failed". */}
      <Box sx={INTENT_SET_DIVIDED_SX}>
        <Typography variant="subtitle2" component="h3" sx={{ fontWeight: 600 }}>
          Recently retired
        </Typography>
        {/* WHAT THE WINDOW BOUNDS: only FAILURES age out — `purge_unexecutable_intents` never matches `done`,
            `accepted` or `unsupported_verb`, so successes are kept indefinitely. `IntentQueue.retention_seconds`
            is the single home for the detail. Renders only once the summary has landed (no guessed bound). */}
        {summary !== null && (
          <Typography
            data-testid="intents-retention-note"
            variant="caption"
            component="p"
            sx={[PROSE_SX, { color: 'text.secondary' }]}
          >
            {`Failures are purged after ${humanDuration(summary.retention_seconds)}. Completed rows are kept.`}
          </Typography>
        )}

        <CountChips testId="intents-outcomes" label="Outcomes:" prefix="outcome" counts={outcomes} tones={OUTCOME_TONE} />

        <IntentTable set="retired" node={node} refreshKey={refreshKey} onPage={onPage} />
      </Box>
    </Stack>
  )
}
