import { useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MuiLink from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { get } from '../api'
import { useApiRead } from '../hooks/useApiRead'
import { useIsOrgAdmin } from '../hooks/useIsOrgAdmin'
import { useEventStream } from '../hooks/useEventStream'
import { useLiveRefetch } from '../hooks/useLiveRefetch'
import { absoluteTime, fmtTimestamp, humanAge, humanDuration, relativeAge } from '../lib/relativeTime'
import type { NodeConfigDrift, NodeHealth as NodeHealthPayload, NodeHealthClock, NodeHealthFinding, NodeHealthUpdate, NodeHosting, NodeInfra } from '../types'
import Banner from '../ui/Banner'
import { TaskLink } from '../ui/DetailFields'
import EmptyState from '../ui/EmptyState'
import FeedList from '../ui/feed/FeedList'
import type { FeedRowSpec } from '../ui/feed/types'
import InfoTip from '../ui/InfoTip'
import LoadingState from '../ui/LoadingState'
import { NONE, NONE_SX } from '../ui/NoneMark'
import RelativeTime from '../ui/RelativeTime'
import SectionCard from '../ui/SectionCard'
import StatusChip, { type Tone } from '../ui/StatusChip'
import { PHONE } from '../ui/listStyles'
import { DRAINING_REASON } from './NodeEnableToggle'

/* ══ THE NODE'S HEALTH TAB ═════════════════════════════════════════════════════════════════════════
 *
 * Internal queues, liveness and staleness for one node, all from ONE read
 * (`GET /api/nodes/{id}/health`). The slot-intent queue lives fleet-wide in Audit > Nodes >
 * Slot Intents; what remains here is per-node.
 *
 * Order: the diagnosis (`verdicts`, with remedies) first, then the evidence (clocks, config drift, updates).
 *
 * NO TIME ARITHMETIC AND NO JUDGEMENT here: `stale`, `state`, `healthy` and verdicts come off the wire
 * (`types.ts`'s NODE HEALTH block says why — a skewed browser clock would mis-judge). Only `humanAge`
 * (`lib/relativeTime.ts`) words the server's seconds. The one exception is a finding's "last logged" age (`Findings`): the server sends
 * a stamp, not seconds, so `lib/relativeTime.ts` words it — the judgement (`healthy`) is still the server's.
 *
 * No permission gate: the route's `@feature("system", "view")` (apps/api/app/routes/nodes/health.py) is the key
 * already guarding `/nodes/:nodeId` in `App.tsx`.
 */

// ── The verdict vocabulary, in words ──────────────────────────────────────────────────────────────

/* What each verdict means and what to do. Transcribed from `apps/api/app/node_liveness.py` (docstring and
 * `verdicts_for`), never re-derived. Every alarm there has a documented remedy; a verdict without one only
 * says there is a problem.
 * Tone: `live` is the only success, `disabled` the only neutral (the operator's own statement); others are
 * error (not doing its job) or warning (doing it while contradicting the roster).
 * `short` is the ONE laid-out clause that decides whether the verdict is the reader's problem;
 * `remedy` lives behind the ⓘ (show the fact, never a definition of the verdict word). `live` and `disabled`
 * lay out no `short`: the summary banner already says it (`QUIET_VERDICTS`). Keep `short` well inside a
 * `body2` column (~56 chars now).
 * Two readers: every `nodes:view` holder reaches this tab, only a superadmin administers the
 * host. `short` is scope-neutral (copy a tenant reads names no host-side action or operator machinery;
 * forbidden words in `apps/api/app/tenancy/copy.py`'s `OPERATOR_CONSOLE_VOCABULARY`). `remedy` is the host
 * administrator's action; `ORG_REMEDIES` has everyone else's sentence where the remedy is a host action.
 * `remedyFor` picks. */
type VerdictNote = { tone: Tone; short: string; remedy: string }

export const VERDICT_NOTES: Record<string, VerdictNote> = {
  live: {
    tone: 'success',
    short: 'Reporting, observing, enrolled and current.',
    remedy: 'Nothing to do.',
  },
  disabled: {
    tone: 'neutral',
    short: 'Switched off — nothing is alarmed on it.',
    remedy: 'Re-enable it from the Overview tab if it should be carrying work.',
  },
  silent: {
    tone: 'error',
    /* "Connected", never "on the bus" (the bus is retired); every reader reads this. */
    short: 'Not connected — nothing else here can be trusted.',
    remedy: 'Bring jaahd back up on the host, or disable the node so the roster stops counting it.',
  },
  'not-observing': {
    tone: 'error',
    short: 'Connected, but its observation pass has stopped.',
    remedy: 'Check the daemon’s observation loop on the host — reachability is proven, so it is not the network.',
  },
  'stale-release': {
    tone: 'warning',
    short: 'Heartbeating on provably older code.',
    remedy: 'Check the daemon’s self-update on the host — a failing updater looks healthy for days.',
  },
  'config-drift': {
    tone: 'warning',
    short: 'Live config contradicts the roster’s declaration.',
    remedy: 'Reconcile the two — the Config drift card names every key that disagrees.',
  },
}

/* `silent` for a machine someone joins by running its install command that has NEVER reported (its presence
 * clock is null): nothing went down, it has not joined yet, so "bring jaahd back up" would send the reader
 * looking for a daemon that was never installed. Picked by `noteFor`; the server's verdict is
 * unchanged. A cloud launch or a co-located node never takes this note: no one runs a command for those. */
export const NEVER_JOINED_NOTE: VerdictNote = {
  tone: 'error',
  short: 'Never connected — the machine hasn’t joined yet.',
  remedy: 'Run the install command on the machine — Install, beside Last Seen on Overview, shows it. The install can take several minutes.',
}
const NEVER_JOINED_ORG_REMEDY = 'An admin finishes adding this machine.'

/** Whether a person joins this node by running its install command: a registered machine, or a host the control
 *  plane did not launch (`apps/api/app/hosts.py::_cp_launched`). */
export function joinsByCommand(node: { hosting?: NodeHosting | null; infra?: NodeInfra | null }): boolean {
  if (node.hosting === 'self_hosted') return true
  const infra = node.infra ?? {}
  return node.hosting === 'multi_node_host' && !infra.instance_id && infra.launched_by !== 'control-plane'
}

/** Whether the node has never reported in: its presence clock was never written. */
export function neverJoined(clocks: NodeHealthClock[]): boolean {
  const presence = clocks.find(c => c.key === 'last_seen')
  return presence !== undefined && presence.at === null
}

/** The note a verdict renders with; `silent` on a never-joined machine reads as not joined yet. */
export function noteFor(verdict: string, joined = true): VerdictNote {
  if (verdict === 'silent' && !joined) return NEVER_JOINED_NOTE
  return VERDICT_NOTES[verdict] ?? UNKNOWN_VERDICT
}

/** Verdicts whose `short` restates the summary banner: the row is the pill and its ⓘ alone. */
const QUIET_VERDICTS = new Set(['live', 'disabled'])

/** A verdict this build does not know: reported, never dropped, so an older console cannot under-report. */
export const UNKNOWN_VERDICT: VerdictNote = {
  tone: 'warning',
  short: 'This build does not know this verdict.',
  remedy: 'Read apps/api/app/node_liveness.py for what it asserts.',
}

/* A non-administrator's remedies where the host administrator's is a host action: ask one. `silent`
 * keeps "disable the node" (a `nodes:edit` holder's act). `config-drift` names no card (this reader has
 * none). Never "superadmin" (`app/tenancy/copy.py`). */
export const ORG_REMEDIES: Record<string, string> = {
  silent:
    'An admin manages this host. Disabling the node stops the roster counting it in the meantime.',
  'not-observing': 'An admin manages this host.',
  'stale-release': 'An admin manages this host.',
  'config-drift': 'An admin manages this host.',
}

/** The remedy this reader can act on. An unknown verdict has no host remedy to withhold. */
export function remedyFor(verdict: string, hostAdmin: boolean, joined = true): string {
  const note = noteFor(verdict, joined)
  if (hostAdmin) return note.remedy
  return note === NEVER_JOINED_NOTE ? NEVER_JOINED_ORG_REMEDY : ORG_REMEDIES[verdict] ?? note.remedy
}

// ── Layout ────────────────────────────────────────────────────────────────────────────────────────

/* The readable measure for any explanatory sentence left on this panel. Full card width was ~163 chars
 * per line at 1440px; past ~90 the eye loses its place. Most prose now sits behind `ui/InfoTip`,
 * so this is a CEILING for the verdict cell (inline-flex, today's copy ~62 chars never reaches it);
 * `e2e/node-detail-health.spec.ts` asserts "a verdict is a line" rather than this width. `ch` tracks the
 * type scale; narrower containers still win. */
const PROSE_SX = { maxWidth: '78ch' } as const

/* Grid items stretch by default, so shorter pills were drawn at the longest verdict's width. Applies to
 * both grids (the only chips in them); `ui/StatusChip` cannot fix it from its side. */
const GRID_PILL_SX = { '& .MuiChip-root': { justifySelf: 'start' } } as const

/* The two explained-item grids (verdicts and clocks). A flex row packed each row from its own start, so
 * pills landed at five different x positions (measured); a `max-content` track aligns them by construction.
 * `display: contents` on each item's wrapper keeps a DOM node carrying `health-verdict-<v>` /
 * `health-clock-<key>` for scoping (`within()`), while its children join the grid. Caveats:
 *   • The wrapper has NO BOX: Playwright's `toBeVisible()`, `.boundingBox()`, `.click()` etc. fail on it.
 *     Assert its TEXT or measure a child cell (as `e2e/node-detail-health.spec.ts` does).
 *   • A box-less role-less `<div>` groups nothing for screen readers (neither did the old one). */
const VERDICT_GRID_SX = {
  display: 'grid',
  gridTemplateColumns: 'max-content minmax(0, 1fr)',
  columnGap: 1.25,
  rowGap: 1,
  alignItems: 'start',
  ...GRID_PILL_SX,
  /* One column on a phone. The row gap goes TIGHT and the seam moves above every pill but the first:
     stacked, a verdict is two grid rows, and equal gaps would make it unclear which sentence belongs to
     which pill. On the pill, not under the prose: a `QUIET_VERDICTS` row has no prose, so a seam there
     left it touching the next pill while the other rows sat 6px apart. */
  [PHONE]: { gridTemplateColumns: 'minmax(0, 1fr)', rowGap: 0.25, '& > * + * > .MuiChip-root': { mt: 0.75 } },
} as const

/* THE CLOCKS CARD'S OWN WIDTH decides its layout, never the window's: a two-up card in a narrow page (the
 * node beside another pane at 1440px) clipped the age column ("1n", "ne") under a viewport rule. The four
 * columns need ~450px (the arithmetic under `HEALTH_TWO_UP`); below that, a paired card included, the label
 * takes its own line. */
const CLOCKS_CONTAINER = 'health-clocks'
const CLOCKS_CONTAINER_SX = { containerType: 'inline-size', containerName: CLOCKS_CONTAINER } as const
const CLOCKS_NARROW = `@container ${CLOCKS_CONTAINER} (max-width:449.95px)`

const CLOCK_GRID_SX = {
  display: 'grid',
  gridTemplateColumns: 'max-content max-content max-content minmax(0, 1fr)',
  columnGap: 1.25,
  /* One line per clock (the meaning is behind its ⓘ), so this is the whole seam. A tenant
     gets four clocks (no Config snapshot); the grid takes whatever arrives. */
  rowGap: 0.75,
  alignItems: 'center',
  ...GRID_PILL_SX,
  /* Three columns in a narrow card: the label takes its own line (`gridColumn` below) and the three short
     facts stay in columns beneath it. */
  /* As `VERDICT_GRID_SX`, the row gap goes TIGHT here and the seam moves under the facts (the age cell's
     `[CLOCKS_NARROW]` rule), so a label reads with its own facts. */
  [CLOCKS_NARROW]: { gridTemplateColumns: 'max-content max-content minmax(0, 1fr)', rowGap: 0.25 },
} as const

/** Each grid item's wrapper. See `VERDICT_GRID_SX` for why `display: contents` and not a box. */
const GRID_ITEM_SX = { display: 'contents' } as const

/** Text plus its ⓘ; inline-flex so the tip sits on the text's line and cannot set the row height. */
const TIPPED_CELL_SX = { display: 'inline-flex', alignItems: 'center', gap: 0.5, minWidth: 0 } as const

/* `ui/InfoTip`'s tap box is 44px below `sm` (20px above). Inline beside text, that box would set the line's
 * height; negative margins keep the touch target while the line sees the 20px glyph. */
const TAP_BOX_PULLBACK_SX = { '& > span': { my: { xs: '-12px', sm: 0 } } } as const

/** An ⓘ inline after a word: a 4px gap to the glyph at every width, its tap box pulled back below `sm`. */
const INLINE_TIP_SX = [{ ml: { xs: '-8px', sm: 0.5 }, mr: { xs: '-12px', sm: 0 } }, TAP_BOX_PULLBACK_SX] as const

/** A sentence split before its last word, so the word and a trailing ⓘ can share a `nowrap` span. */
const lastSpace = (text: string) => text.lastIndexOf(' ') + 1
const leadOf = (text: string) => text.slice(0, lastSpace(text))
const lastWordOf = (text: string) => text.slice(lastSpace(text))

/** One explanation as the tip (and `ui/InfoTip`'s sr-only copy) renders it. SPANS, never `<p>`:
 *  `e2e/node-detail-health.spec.ts` measures the `p` elements in a verdict row, and an off-screen copy
 *  would report a 1px "sentence". */
/* ══ THE PANEL'S OWN COLUMNS ════════════════════════════════════════════════════════════════════════
 * Two-up from a 900px PANEL (verdicts left, clocks right — the same order as stacked), one column below; the
 * full-width row stays below the pair. The panel's width, not the window's: the node opened beside the roster
 * at 1440px is a ~545px pane, where a viewport gate squeezed two cards side by side.
 * The gate sits under the smallest panel a 1240 window gives (so a 1280×800 laptop pairs, classic scrollbar
 * included), measured live:
 *   1240 viewport − 192 sidebar − 2 × 32 pane insets (`ListDetail`'s `px`; the launcher's clearance is
 *   bottom-only) = 984 of panel, 969 with a scrollbar → 900, 69px to spare (109 at 1280). 1024 gives 768, so it stacks.
 *   At the gate a card is (900 − 9 grid gap) / 2 − 18 (`SectionCard`'s padding) ≈ 427, under the clocks'
 *   four-column ~450 (145 label + 27.5 ⓘ + 45 pill + 70 age + 140 stamp + 22.5 gaps), so a paired clocks
 *   card takes its narrow form (`CLOCKS_NARROW`): label above, facts beneath.
 * `alignItems` stays `stretch` (checked live): `start` left a ragged bottom, which read worse than a
 * part-empty card. */
const HEALTH_PANEL_CONTAINER = 'node-health'
const HEALTH_PANEL_CONTAINER_SX = { containerType: 'inline-size', containerName: HEALTH_PANEL_CONTAINER } as const
const HEALTH_TWO_UP = `@container ${HEALTH_PANEL_CONTAINER} (min-width:900px)`

const PANEL_SX = {
  display: 'grid',
  gridTemplateColumns: 'minmax(0, 1fr)',
  gap: 1.5,
  [HEALTH_TWO_UP]: { gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' },
} as const

/** A full-row card (drift table, load-error banner); inert below the gate. */
const PANEL_FULL_ROW_SX = { [HEALTH_TWO_UP]: { gridColumn: '1 / -1' } } as const

// ── Sections ──────────────────────────────────────────────────────────────────────────────────────

/* (a) THE VERDICT SUMMARY: one pill-led row per verdict — the pill to scan, the sentence to act on.
 * `healthy` gets its own line, not inferred from the `live` pill: a DISABLED node is healthy too (server
 * computed, `get_node_health`), and so is a draining one — three good states, said differently.
 * Each verdict is named once: the pill in its row, or the banner when it is the ONLY issue (a pill
 * beside "1 issue: silent" said "silent" twice). A violated row is ONE line (`short`, its remedy in
 * brief); `remedy` is behind the ⓘ, so a reader decides from the sentences, not hovers. A
 * `QUIET_VERDICTS` row is the pill only — the banner already says it.
 * `hostAdmin` is a prop so this stays a pure rendering; it picks the remedy (`remedyFor`). */
function Verdicts({ health, hostAdmin, byCommand, note }: {
  health: NodeHealthPayload
  hostAdmin: boolean
  byCommand: boolean
  /** One closing line under the verdicts (`NO_REPORTS`), in the absent mark's faintness. */
  note?: string
}) {
  const joined = !(byCommand && neverJoined(health.clocks))
  const notes = health.verdicts.map(v => ({ verdict: v, note: noteFor(v, joined) }))
  // Each violated verdict and each finding is one issue; `live`/`disabled` beside a finding are not. Each is
  // NAMED on the banner (a finding by its label, a verdict by its word), so the card reads without the
  // Findings card above it.
  const issues = [...(health.findings ?? []).map(f => f.label), ...health.verdicts.filter(v => !QUIET_VERDICTS.has(v))]
  // The banner names a lone issue; with several, the pills say which sentence is whose.
  const namedByBanner = issues.length === 1 ? issues[0] : null
  return (
    <SectionCard
      title="Verdict"
      /* The verdict bound qualifies the whole card, so it sits beside the title (as a clock's ⓘ beside its
         label), not at the row's far edge. The two enumeration clocks use a different, operator-tunable
         bound (`types.ts`), so it is not beside a clock. */
      tip={
        /* Beside its title: below, the tooltip covered the summary banner. */
        <InfoTip
          testId="health-bound-tip"
          label="What silent and not-observing are measured against"
          title={`Silent / not-observing use a ${humanDuration(health.stale_bound_seconds)} bound. Each clock carries its own.`}
          placement="right"
        />
      }
    >
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
        {health.healthy ? (
          /* The drain arm sits INSIDE `healthy`: a drain is not a verdict, so a draining AND silent node
             still reads the error arm. It explains only an otherwise clean node (`live` beside an
             emptying slot table). `severity="info"`: operator-requested, neither alarm nor clean bill
             (`app/models/nodes.py::Node.draining` owns the meaning). The sentence is imported
             (`DRAINING_REASON` is its one home); the full stop is this card's. */
          health.draining ? (
            <Banner severity="info" testId="health-verdict-summary">{`${DRAINING_REASON}.`}</Banner>
          ) : (
            <Banner severity="success" testId="health-verdict-summary">
              {health.enabled ? 'Healthy' : 'Off'}
            </Banner>
          )
        ) : (
          <Banner severity="error" testId="health-verdict-summary">
            {`${issues.length === 1 ? '1 issue' : `${issues.length} issues`}${issues.length > 0 ? `: ${issues.join(', ')}` : ''}`}
          </Banner>
        )}

        {/* The testid holds the verdict LIST (pills and sentences) in payload order; query pills via
            `.MuiChip-root`, never "all the text in here". */}
        <Box data-testid="health-verdict-pills" sx={VERDICT_GRID_SX}>
          {notes.map(({ verdict, note }) => (
            <Box key={verdict} data-testid={`health-verdict-${verdict}`} sx={GRID_ITEM_SX}>
              {/* In the `max-content` column; `ui/StatusChip` never shrinks, so the column grows. */}
              {verdict !== namedByBanner && <StatusChip label={verdict} tone={note.tone} data-status={verdict} />}
              {/* The measure is a ceiling here (see `PROSE_SX`). */}
              {/* THE ⓘ RIDES THE SENTENCE'S LAST LINE: a flex sibling sat at the card's right edge once the
                  sentence wrapped (a phone). It is inline, bound to the last word by a `nowrap` span, so it
                  never wraps onto a line of its own (`INLINE_TIP_SX`). */}
              {!QUIET_VERDICTS.has(verdict) && (
                <Typography
                  variant="body2"
                  component="p"
                  /* Pill-less, the sentence takes the whole row rather than the pill's `max-content` track. */
                  sx={[PROSE_SX, { minWidth: 0 }, verdict === namedByBanner && { gridColumn: '1 / -1' }]}
                >
                  {leadOf(note.short)}
                  <Box component="span" sx={{ whiteSpace: 'nowrap' }}>
                    {lastWordOf(note.short)}
                    <Box component="span" sx={INLINE_TIP_SX}>
                      <InfoTip
                        testId={`health-verdicttip-${verdict}`}
                        label={`Remedy for “${verdict}”`}
                        title={`Remedy: ${remedyFor(verdict, hostAdmin, joined)}`}
                      />
                    </Box>
                  </Box>
                </Typography>
              )}
            </Box>
          ))}
        </Box>
        {note && (
          <Typography variant="body2" component="p" data-testid="health-no-reports" sx={[NONE_SX, { m: 0 }]}>
            {note}
          </Typography>
        )}
      </Box>
    </SectionCard>
  )
}

/* (a′) FINDINGS: faults the node logged that no clock shows — a NodeLink store not answering, a wedged step,
 * memory or CPU at critical, an OOM kill, a relaunch storm (its `task_ids` render as links). Rendered by kind-blind code (every word is the server's), so a new
 * kind needs no change here. They lead the panel, full width: each is the cause behind an unhealthy node, so
 * it is read before the clocks. The label, the logged line (wraps, never cut: it is the evidence), when it was
 * first and last logged, and what it costs. */
/* `recent` are the last 24h's findings no longer active: listed under "Earlier (24h)", toned down (no error
 * colour), so a storm that just settled is still traceable without reading as current. */
function Findings({ findings, recent }: { findings: NodeHealthFinding[]; recent: NodeHealthFinding[] }) {
  return (
    <SectionCard title="Findings">
      {findings.length > 0 && (
        <Box data-testid="health-findings" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
          {findings.map((f, i) => <Finding key={`${f.kind}:${i}`} f={f} />)}
        </Box>
      )}
      {recent.length > 0 && (
        <Box data-testid="health-findings-recent" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, mt: findings.length > 0 ? 2 : 0 }}>
          <Typography variant="caption" component="h4" sx={{ m: 0, color: 'text.secondary', fontWeight: 600 }}>
            Earlier (24h)
          </Typography>
          {recent.map((f, i) => <Finding key={`${f.kind}:${i}`} f={f} earlier />)}
        </Box>
      )}
    </SectionCard>
  )
}

function Finding({ f, earlier = false }: { f: NodeHealthFinding; earlier?: boolean }) {
  const tone = earlier ? 'text.secondary' : 'error.main'
  return (
    <Box
      data-testid={`health-finding-${f.kind}`}
      sx={{ display: 'flex', flexDirection: 'column', gap: 0.5, minWidth: 0, pl: 1.25, borderLeft: 3, borderColor: earlier ? 'divider' : 'error.main' }}
    >
      <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', justifyContent: 'space-between', columnGap: 1.5, rowGap: 0.25 }}>
        <Typography variant="body2" component="span" sx={{ fontWeight: 600, color: tone, minWidth: 0, overflowWrap: 'anywhere' }}>
          {f.label}
        </Typography>
        {/* Both ends of the run: a fault seen once reads apart from one logged for hours. `first_at` is
            null where the server never recorded it — omitted, never guessed. */}
        <Typography variant="caption" component="span" sx={{ color: 'text.secondary', whiteSpace: 'nowrap' }}>
          {f.first_at && (
            <>
              <Box component="time" dateTime={f.first_at} data-testid="health-finding-first" title={absoluteTime(f.first_at)}>
                First {relativeAge(f.first_at)}
              </Box>
              {' · '}
            </>
          )}
          <Box component="time" dateTime={f.last_at} data-testid="health-finding-last" title={absoluteTime(f.last_at)}>
            Last {relativeAge(f.last_at)}
          </Box>
        </Typography>
      </Box>
      <Typography
        variant="mono"
        component="p"
        data-testid="health-finding-detail"
        sx={{ m: 0, fontSize: '0.8125rem', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}
      >
        {f.detail}
      </Typography>
      {/* The tasks a finding names (a relaunch storm), each a link to its page and to its sessions in
          Audit > Sessions (`/sessions?task=<id>`), where each relaunch is a row. */}
      {(f.task_ids?.length ?? 0) > 0 && (
        <Box data-testid="health-finding-tasks" sx={{ display: 'flex', flexWrap: 'wrap', columnGap: 1.5, rowGap: 0.25 }}>
          {f.task_ids?.map(id => (
            <Box key={id} component="span" sx={{ display: 'inline-flex', alignItems: 'baseline', columnGap: 0.75 }}>
              <TaskLink taskId={id} label="Task" variant="body2" />
              <MuiLink
                component={RouterLink}
                to={`/sessions?task=${id}`}
                variant="body2"
                underline="hover"
                aria-label={`Sessions of task #${id}`}
                data-testid="health-finding-sessions"
              >
                sessions
              </MuiLink>
            </Box>
          ))}
        </Box>
      )}
      <Typography variant="body2" component="p" sx={[PROSE_SX, { m: 0, color: 'text.secondary' }]}>{f.meaning}</Typography>
    </Box>
  )
}

/* (b) THE REPORTING CLOCKS: a four-column grid, not a `DataTable`. Each clock's `meaning` is one ⓘ
 * beside its name. The grid puts every pill in one column (`CLOCK_GRID_SX`), so "which is
 * stale" is one glance. The ⓘ hangs off the LABEL (what the clock is, what staleness costs); the pill's
 * tooltip carries its bound (what stale was judged against).
 * Until any clock has ticked (a node that never connected) the card is not drawn: the panel's one
 * `NO_REPORTS` line says it. */
function Clocks({ clocks }: { clocks: NodeHealthClock[] }) {
  return (
    <SectionCard title="Reporting clocks">
      <Box sx={CLOCKS_CONTAINER_SX}>
        <Box sx={CLOCK_GRID_SX}>
          {clocks.map(clock => (
            <Box key={clock.key} data-testid={`health-clock-${clock.key}`} sx={GRID_ITEM_SX}>
              {/* The name keeps its own testid (the ⓘ adds a text node; the spec reads the label verbatim).
                  NOTE THE TESTID SPELLING — `health-clockname-`, not `health-clock-name-`: the spec's row
                  census selects `[data-testid^="health-clock-"]`. Same for `health-clocktip-` and
                  `health-verdicttip-`. */}
              <Box
                component="span"
                /* Its own line in a narrow card (see `CLOCK_GRID_SX`); above that, `max-content` never clips a label. */
                sx={[TIPPED_CELL_SX, { [CLOCKS_NARROW]: { gridColumn: '1 / -1' } }]}
              >
                <Typography
                  variant="body2"
                  component="span"
                  data-testid={`health-clockname-${clock.key}`}
                  sx={{ fontWeight: 600 }}
                >
                  {clock.label}
                </Typography>
                {/* The tap box pulled back to the glyph (`TAP_BOX_PULLBACK_SX`), or it sets the label row's height
                    and the label drifts away from its facts. */}
                <Box component="span" sx={[{ display: 'inline-flex', mx: { xs: '-12px', sm: 0 } }, TAP_BOX_PULLBACK_SX]}>
                  <InfoTip
                    testId={`health-clocktip-${clock.key}`}
                    label={`What ${clock.label} reports`}
                    title={clock.meaning}
                  />
                </Box>
              </Box>
              {/* The pill is the server's `stale`; the bound rides the tooltip. */}
              {/* A withheld bound (`null` for a tenant, an operator knob —
                  `types.ts::NodeHealthClock.bound_seconds`) is OMITTED, not "—", which would claim none exists. */}
              <StatusChip
                label={clock.stale ? 'STALE' : 'FRESH'}
                tone={clock.stale ? 'error' : 'success'}
                data-status={clock.stale ? 'stale' : 'fresh'}
                title={clock.bound_seconds === null
                  ? clock.stale ? 'STALE' : 'FRESH'
                  : `Bound: ${humanDuration(clock.bound_seconds)}`}
                /* The accessible name is the STATUS (else `ui/StatusChip` falls back to `title`, announcing
                   only the bound); the bound follows, since the tooltip is unreachable by touch. */
                ariaLabel={clock.bound_seconds === null
                  ? (clock.stale ? 'STALE' : 'FRESH')
                  : `${clock.stale ? 'STALE' : 'FRESH'} — bound ${humanDuration(clock.bound_seconds)}`}
              />
              <Typography variant="body2" component="span" sx={{ color: 'text.secondary', [CLOCKS_NARROW]: { mb: 0.75 } }}>
                {humanAge(clock.age_seconds)}
              </Typography>
              {/* The absolute stamp (WHEN, not only how long ago), in `fmtTimestamp`'s form so the tab reads one
                  date form with the Disk history below. `null` still emits an empty cell to keep the grid; no
                  em-dash, since `never` is already on the line. */}
              <Typography variant="caption" component="span" data-testid={`health-clockat-${clock.key}`} sx={{ color: 'text.secondary' }}>
                {clock.at ? fmtTimestamp(clock.at) : ''}
              </Typography>
            </Box>
          ))}
        </Box>
      </Box>
    </SectionCard>
  )
}

/* NULL IS AN AMBIGUITY, named not resolved (`ConfigDriftRow`): the node may not set the key, or the key
 * is outside the daemon's snapshot allowlist. The `?` glyph says it on screen; the sentence rides the
 * glyph's and the slot's tooltips and the accessible name, never a laid-out line. */
const UNREPORTED = 'not reported'
const UNREPORTED_NOTE =
  'not reported — unset on the node, or absent from the daemon’s snapshot allowlist'

/* The `declared → reported` pair's width: fits `on → off`, `1.1.400 → 1.1.387`; longer values ellipsise,
 * recoverable from `title` and the accessible name. */
const DRIFT_SLOT = 168

/* (c) CONFIG DRIFT, omitted when empty (`config-drift` is already a verdict then).
 * A feed list: declared and reported are ONE slot (never read apart; separate columns would
 * drop one at 900px). The caption states the direction. */
function ConfigDrift({ rows }: { rows: NodeConfigDrift[] }) {
  const driftRows: FeedRowSpec[] = rows.map((row): FeedRowSpec => {
    const unreported = row.got === null
    const got = row.got ?? UNREPORTED
    return {
      key: row.key,
      indicator: {
        /* Two states, two glyphs: `≠` a reported contradiction (act on that host); `?` the ambiguity above
           (find out first). */
        tone: unreported ? 'neutral' : 'warning',
        glyph: unreported ? '?' : '≠',
        label: unreported ? 'value not reported' : 'contradicts the declaration',
        title: unreported ? UNREPORTED_NOTE : undefined,
      },
      title: <Typography variant="mono" component="span">{row.key}</Typography>,
      titleText: row.key,
      ariaLabel: `${row.key} — declared ${row.want}, ${unreported ? UNREPORTED_NOTE : `reported ${got}`}`,
      slots: [
        {
          key: 'values',
          width: DRIFT_SLOT,
          mono: true,
          testId: `health-drift-${row.key}`,
          title: `Declared ${row.want} · ${unreported ? UNREPORTED_NOTE : `reported ${got}`}`,
          node: `${row.want} → ${got}`,
        },
      ],
    }
  })

  return (
    <SectionCard
      title="Config drift"
      /* No how-to-read tip: the `→` states direction. */
    >
      {/* The testid keeps its `-table` name (two suites use it; `FeedList`'s root is `role="table"`). */}
      <FeedList
        ariaLabel="Config drift"
        testId="health-drift-table"
        sections={[{ key: 'drift', rows: driftRows }]}
      />
    </SectionCard>
  )
}

/* (e) SELF-UPDATES: the node's update passes, newest first (the server keeps 20). The answer to "is the
 * updater working" that `stale-release` only infers. One wrapping line per pass — outcome pill, version, age —
 * so it reads at 360px without a table; a failed pass lays its cause out under it (the evidence, never behind
 * a hover a phone cannot do). The first `UPDATES_SHOWN` show; the rest behind one button. */
const UPDATES_SHOWN = 5

const UPDATE_TONE: Record<NodeHealthUpdate['outcome'], Tone> = { converged: 'success', failed: 'error' }

const UPDATE_ROW_SX = {
  display: 'flex',
  flexDirection: 'column',
  gap: 0.25,
  minWidth: 0,
  py: 0.75,
  '& + &': { borderTop: '1px solid', borderColor: 'divider' },
} as const

function Updates({ updates }: { updates: NodeHealthUpdate[] }) {
  const [all, setAll] = useState(false)
  const shown = all ? updates : updates.slice(0, UPDATES_SHOWN)
  const hidden = updates.length - shown.length
  /* None is the card's title row alone (a phone spent a whole card on one sentence). */
  if (updates.length === 0) {
    return (
      <SectionCard
        title="Updates"
        actions={
          <Typography variant="body2" data-testid="health-updates-empty" sx={NONE_SX}>
            {NONE}
          </Typography>
        }
      />
    )
  }
  return (
    <SectionCard title="Updates">
      <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 0.5 }}>
        <Box component="ul" aria-label="Updates" data-testid="health-updates" sx={{ m: 0, p: 0, listStyle: 'none', alignSelf: 'stretch', minWidth: 0 }}>
          {shown.map((u, i) => (
            <Box component="li" key={`${u.at}:${i}`} data-testid="health-update" data-outcome={u.outcome} sx={UPDATE_ROW_SX}>
              <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1.25, rowGap: 0.25, minWidth: 0 }}>
                <StatusChip
                  label={u.outcome}
                  tone={UPDATE_TONE[u.outcome] ?? 'neutral'}
                  glyph={u.outcome === 'failed' ? '✕' : '✓'}
                  data-status={u.outcome}
                  /* A converged pass's line rides the pill (the failed one is laid out below). */
                  title={u.outcome === 'converged' && u.detail ? u.detail : undefined}
                  ariaLabel={u.outcome}
                />
                <Typography
                  variant="mono"
                  component="span"
                  data-testid="health-update-version"
                  sx={{ fontSize: '0.8125rem', minWidth: 0, overflowWrap: 'anywhere', color: u.version ? 'text.primary' : 'text.disabled' }}
                >
                  {u.version ?? 'version unknown'}
                </Typography>
                <RelativeTime
                  iso={u.at}
                  variant="caption"
                  sx={{ color: 'text.secondary', whiteSpace: 'nowrap', ml: 'auto' }}
                />
              </Box>
              {u.outcome === 'failed' && u.detail && (
                <Typography
                  variant="mono"
                  component="p"
                  data-testid="health-update-detail"
                  sx={{ m: 0, fontSize: '0.8125rem', color: 'error.main', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}
                >
                  {u.detail}
                </Typography>
              )}
            </Box>
          ))}
        </Box>
        {hidden > 0 && (
          <Button size="small" data-testid="health-updates-more" onClick={() => setAll(true)}>
            {`Show ${hidden} more`}
          </Button>
        )}
      </Box>
    </SectionCard>
  )
}

// ── The panel ─────────────────────────────────────────────────────────────────────────────────────

const LOAD_ERROR = 'Failed to load this node’s health'

/* A node none of whose clocks ever ticked has reported nothing: clocks, drift and updates would be cards that
 * each say so (and the page drops Metrics and Disk on the same fact, `NodeDetailPage.tsx`). The Verdict card
 * stays, closed by this one line. */
const NO_REPORTS = 'No reports yet'

/** Whether no clock has ever ticked: the node has reported nothing at all. */
function nothingReported(clocks: NodeHealthClock[]): boolean {
  return clocks.length > 0 && clocks.every(c => c.at === null)
}

export default function NodeHealth({ nodeId, joinsByCommand: byCommand = false }: {
  nodeId: string
  /** The node is joined by a person running its install command (`joinsByCommand`): a never-seen one reads
   *  as not joined yet. */
  joinsByCommand?: boolean
}) {
  /* The last good reading stays under a stale banner when a refetch fails (`useApiRead` keeps `data`).
     The banner says this panel's own words, never the server's detail. Remounted per node by the page. */
  const { data: health, error: failed, refresh } = useApiRead(
    ['node-health', nodeId],
    () => get<NodeHealthPayload>(`/api/nodes/${encodeURIComponent(nodeId)}/health`),
    LOAD_ERROR,
  )
  const error = failed ? LOAD_ERROR : ''

  // Whether this reader administers the host (an org admin, `routes/nodes/node_wire.py::_administers_own_hosts`),
  // read once for `Verdicts`. It only picks the remedy.
  const hostAdmin = useIsOrgAdmin()

  /* The panel's one subscription: `node` events re-read this route so it never ages quietly. Filtered to
     `node`: all entities would re-read on every worktree lease in the fleet. */
  useLiveRefetch(useEventStream(), () => void refresh(), null, { entity: 'node' })

  if (health === null) {
    return (
      <SectionCard title="Health">
        {error ? <Banner severity="error" testId="health-load-error">{error}</Banner> : <LoadingState rows={4} />}
      </SectionCard>
    )
  }

  /* (d) An unregistered host: ONE explanation. With no `nodes` row there are no clocks or declaration, and
     empty sections would accuse a non-member of being broken. `registered` is the server's. */
  if (!health.registered) {
    return (
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
        {error && <Banner severity="error" testId="health-load-error">{error}</Banner>}
        <SectionCard title="Health">
          <EmptyState
            title="Not registered"
            action={
              <Button component={RouterLink} to="/nodes" size="small" variant="outlined">
                Register
              </Button>
            }
          />
        </SectionCard>
      </Box>
    )
  }

  const silent = nothingReported(health.clocks)
  return (
    <Box sx={HEALTH_PANEL_CONTAINER_SX}>
      <Box sx={PANEL_SX}>
        {/* The MESSAGE handle, not the root `error-banner` token: an unhealthy node already has one. */}
        {error && (
          <Box sx={PANEL_FULL_ROW_SX}>
            <Banner severity="error" testId="health-load-error">{`${error} — showing the last reading.`}</Banner>
          </Box>
        )}
        {((health.findings?.length ?? 0) > 0 || (health.recent_findings?.length ?? 0) > 0) && (
          <Box sx={PANEL_FULL_ROW_SX}>
            <Findings findings={health.findings ?? []} recent={health.recent_findings ?? []} />
          </Box>
        )}
        {silent ? (
          <Box sx={PANEL_FULL_ROW_SX}>
            <Verdicts health={health} hostAdmin={hostAdmin} byCommand={byCommand} note={NO_REPORTS} />
          </Box>
        ) : (
          <>
            <Verdicts health={health} hostAdmin={hostAdmin} byCommand={byCommand} />
            <Clocks clocks={health.clocks} />
            {health.config_drift.length > 0 && (
              <Box sx={PANEL_FULL_ROW_SX}>
                <ConfigDrift rows={health.config_drift} />
              </Box>
            )}
            {/* An older server sends no `updates`: the same empty card as none recorded. */}
            <Box sx={PANEL_FULL_ROW_SX}>
              <Updates updates={Array.isArray(health.updates) ? health.updates : []} />
            </Box>
          </>
        )}
      </Box>
    </Box>
  )
}
