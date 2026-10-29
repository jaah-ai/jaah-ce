import { Fragment, type JSX, type ReactNode, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Divider from '@mui/material/Divider'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Switch from '@mui/material/Switch'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import Typography from '@mui/material/Typography'
import useMediaQuery from '@mui/material/useMediaQuery'
import { alpha, type Theme } from '@mui/material/styles'
import { apiDetail, get, put } from '../../../api'
import { useAgentProviders } from '../../../hooks/useAgentProviders'
import { isEnabled } from '../../../lib/editionFeatures'
import { capabilitiesOf } from '../../../lib/providerCatalogue'
import type { AccountAuthKind, ProviderChoice } from '../../../types'
import Banner from '../../../ui/Banner'
import DataTable from '../../../ui/DataTable'
import EmptyState from '../../../ui/EmptyState'
import LoadingState from '../../../ui/LoadingState'
import { PHONE_QUERY } from '../../../ui/listStyles'

/* Settings > Providers — a summary of every catalogue row, then one section per row: its capability groups, gaps
 * and runtime modes, and the per provider/auth-kind runtime switch (operator only). Every fact is the listing's
 * (`GET /api/accounts/providers`, `ProviderCapabilities`); a group the server does not serve reads "—", a
 * capability it serves as off reads "<name>: none".
 * The switches are `GET/PUT /api/settings/acp-runtime` (`routes/settings.py`, resolution in `app/acp_runtime.py`):
 * every pair served, each at the server's effective value (ON by default, OFF for a catalogue `acp_switch_off` kind
 * until the operator turns it on); a `legacy_only` pair has no switch, only "Terminal only"
 * and its reason, and an `acp_only` pair likewise "ACP only". Each reason is said once: on the Runtime line, where
 * the Gaps list names that gap without repeating it, and the Views row drops the catalogue's. A gap two or more
 * providers serve word for word is said once, in the Shared gaps block, and each card names it with a link there. */

/** One pair of `GET /api/settings/acp-runtime` (`routes/settings.py::AcpRuntimeSwitch`). */
export type AcpRuntimeSwitch = {
  provider: string
  label: string
  auth_kind: string
  /** `"<provider>:<auth_kind>"`, the key `PUT` takes. */
  key: string
  on: boolean
  legacy_only: boolean
  acp_only?: boolean
  reason: string
}

type AcpRuntimeOut = { switches: AcpRuntimeSwitch[] }

export const ACP_RUNTIME_PATH = '/api/settings/acp-runtime'
const ACP_RUNTIME_QUERY_KEY = ['acp-runtime']

const AUTH_LABEL: Record<string, string> = { oauth: 'Subscription', api_key: 'API key', jaah_account: 'Jaah account' }

const ABSENT = '—'
const TERMINAL_ONLY = 'Terminal only'
const ACP_ONLY = 'ACP only'
/** What a session runs on, by its pair's runtime: the switch's two states, and the console view each offers. */
const RUNTIME_WORD = { acp: 'ACP host', terminal: 'Terminal' } as const
const VIEW_WORD = { acp: 'Agent activity', terminal: 'TUI' } as const
const QUESTION_WORD = { elicitation: 'elicitation', mcp_tool: 'MCP tool', command: 'command' } as const

const pairKey = (provider: string, kind: string) => `${provider}:${kind}`
const anchorOf = (provider: string) => `provider-${provider}`
const gapsAnchorOf = (provider: string) => `provider-${provider}-gaps-fact`
const SHARED_ANCHOR = 'providers-shared-gaps'

type Runtime = keyof typeof RUNTIME_WORD

/** One launch lane of a provider: an auth kind, or a served legacy-only lane that is none (a Jaah account). */
type Lane = {
  kind: string
  label: string
  served?: AcpRuntimeSwitch
  /** "Terminal only" / "ACP only" for a lane with no switch, else null. */
  only: string | null
  /** Why `only`; "" for a switched lane. */
  reason: string
  /** What a session on it runs now; null when the switches did not load. */
  runtime: Runtime | null
}

/** A gap as listed: `consequence` null when the Runtime line already says it, and then `lanes` names the lanes and
 *  their fixed runtime ("Subscription, API key: terminal only") at every width; "" when the
 *  capability itself already names them. `id` is the served gap's key and consequence, the same on every provider
 *  that serves it word for word. */
type Gap = { id: string; key: string; capability: string; consequence: string | null; lanes?: string; harness?: boolean }

/** A gap served word for word by two or more providers, and the providers that serve it. */
type SharedGap = { id: string; capability: string; consequence: string; providers: string[] }

const gapId = (gap: { key: string; consequence: string }) => `${gap.key}\n${gap.consequence}`

/** Either source saying "legacy only" wins: the switch route and the catalogue agree once both land. */
function legacyOnly(provider: ProviderChoice, kind: string, switches: Map<string, AcpRuntimeSwitch>): boolean {
  return switches.get(pairKey(provider.value, kind))?.legacy_only === true
    || capabilitiesOf(provider).runtime?.[kind as AccountAuthKind] === 'legacy_only'
}

/** Either source saying "ACP only" wins, as for `legacyOnly`. */
function acpOnly(provider: ProviderChoice, kind: string, switches: Map<string, AcpRuntimeSwitch>): boolean {
  return switches.get(pairKey(provider.value, kind))?.acp_only === true
    || capabilitiesOf(provider).runtime?.[kind as AccountAuthKind] === 'acp_only'
}

function lanesOf(row: ProviderChoice, switches: Map<string, AcpRuntimeSwitch>): Lane[] {
  // A served row that is no auth kind of this provider is a launch lane of its own (a Jaah account).
  const extra = [...switches.values()]
    .filter(sw => sw.provider === row.value && !(row.auth_kinds as string[]).includes(sw.auth_kind))
    .map(sw => sw.auth_kind)
  return [...row.auth_kinds, ...extra].map(kind => {
    const served = switches.get(pairKey(row.value, kind))
    const only = legacyOnly(row, kind, switches) ? TERMINAL_ONLY : acpOnly(row, kind, switches) ? ACP_ONLY : null
    const runtime: Runtime | null = only === TERMINAL_ONLY ? 'terminal' : only === ACP_ONLY ? 'acp'
      : served ? (served.on ? 'acp' : 'terminal') : null
    // A served row's reason stands even when "": the catalogue's console reason is only for a lane the route omits.
    const reason = !only ? '' : served ? served.reason : capabilitiesOf(row).console?.reason || ''
    return { kind, label: AUTH_LABEL[kind] ?? kind, served, only, reason, runtime }
  })
}

/** The catalogue's gaps, a gap the Runtime line states named but not repeated, plus a no-switch lane no gap
 *  covers (Claude's subscription), so the Gaps list alone still shows every lane that cannot switch. */
function gapsOf(row: ProviderChoice, lanes: Lane[]): Gap[] | undefined {
  const listed = capabilitiesOf(row).gaps ?? row.degradations
  // The community edition has no MicroVM nodes, so a gap only they carry is not one there.
  const served = !isEnabled('microvm') ? listed?.filter(gap => !gap.key.startsWith('microvm-') && !/microvm/i.test(gap.capability)) : listed
  const gaps: Gap[] = (served ?? []).map(gap => {
    const stating = lanes.filter(lane => lane.reason && lane.reason === gap.consequence)
    if (stating.length === 0) return { ...gap, id: gapId(gap) }
    const only = [...new Set(stating.map(lane => (lane.only ?? '').toLowerCase()))].join(' / ')
    return { ...gap, id: gapId(gap), consequence: null, lanes: `${stating.map(lane => lane.label).join(', ')}: ${only}` }
  })
  const covered = new Set((served ?? []).map(gap => gap.consequence))
  const lanesOnly = lanes
    .filter(lane => lane.only && !covered.has(lane.reason))
    .map(lane => ({ id: `lane-${lane.kind}`, key: `lane-${lane.kind}`, capability: `${lane.label}: ${(lane.only ?? '').toLowerCase()}`, consequence: null, lanes: '' }))
  return served === undefined && lanesOnly.length === 0 ? undefined : [...gaps, ...lanesOnly]
}

/** The gaps two or more providers serve word for word, in first-served order. A gap whose consequence the
 *  Runtime line states stays on its card, where "see Runtime" points. */
function sharedGapsOf(laned: { row: ProviderChoice; gaps: Gap[] | undefined }[]): SharedGap[] {
  const byId = new Map<string, SharedGap>()
  for (const { row, gaps } of laned) {
    for (const gap of gaps ?? []) {
      if (gap.consequence === null) continue
      const seen = byId.get(gap.id)
      if (seen) { if (!seen.providers.includes(row.label)) seen.providers.push(row.label) }
      else byId.set(gap.id, { id: gap.id, capability: gap.capability, consequence: gap.consequence, providers: [row.label] })
    }
  }
  return [...byId.values()].filter(gap => gap.providers.length > 1)
}

/** What a provider's sessions run on now, as the summary words it: "ACP host", "Terminal", both, or "—". */
function runtimeWordOf(lanes: Lane[]): string {
  const runtimes = (Object.keys(RUNTIME_WORD) as Runtime[]).filter(r => lanes.some(lane => lane.runtime === r))
  return runtimes.length ? runtimes.map(r => RUNTIME_WORD[r]).join(' · ') : ABSENT
}

/** The views the console offers each lane as the switches stand: one word when every lane agrees, else grouped by
 *  view ("Agent activity: API key, Jaah account · TUI: Subscription"), lanes with no view muted as "none: …". Each
 *  group keeps its view and its lanes together, so a label never wraps away from its value. */
function viewsOf(row: ProviderChoice, lanes: Lane[]): ReactNode {
  const caps = capabilitiesOf(row).console
  if (!caps) return ABSENT
  const viewOf = (runtime: Runtime) => (runtime === 'acp' ? caps.chat_view : caps.terminal_view) ? VIEW_WORD[runtime] : null
  const known = lanes.filter(lane => lane.runtime).map(lane => ({ lane, view: viewOf(lane.runtime!) }))
  if (known.length === 0) return ABSENT
  const groups = new Map<string | null, string[]>()
  for (const { lane, view } of known) groups.set(view, [...(groups.get(view) ?? []), lane.label])
  if (groups.size === 1) return known[0].view ?? <Off>none</Off>
  // In the switch's order, ACP host's view first; the view-less lanes last.
  const rank = (view: string | null) => (view === VIEW_WORD.acp ? 0 : view === VIEW_WORD.terminal ? 1 : 2)
  const sorted = [...groups].sort(([a], [b]) => rank(a) - rank(b))
  return sorted.map(([view, labels], i) => (
    <Box component="span" key={view ?? ''} sx={{ display: 'inline-block' }}>
      {view === null ? <Off>none: {labels.join(', ')}</Off> : `${view}: ${labels.join(', ')}`}
      {i < sorted.length - 1 && <>&nbsp;·&nbsp;</>}
    </Box>
  ))
}

function questionsOf(row: ProviderChoice): string | null | undefined {
  const requests = capabilitiesOf(row).requests
  if (!requests) return undefined
  return requests.user_input ? QUESTION_WORD[requests.user_input] : null
}

export default function ProvidersSection(): JSX.Element {
  const { rows, settled } = useAgentProviders()
  const queryClient = useQueryClient()
  const runtime = useQuery({
    queryKey: ACP_RUNTIME_QUERY_KEY,
    queryFn: () => get<AcpRuntimeOut>(ACP_RUNTIME_PATH),
    refetchOnWindowFocus: false,
  })
  const switches = new Map((runtime.data?.switches ?? []).map(sw => [sw.key, sw]))
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')
  // The Shared gaps fold, held here so a card's "N shared gaps" jump can unfold it: a folded block may hide the gap.
  const [sharedAll, setSharedAll] = useState(false)

  async function flip(pair: string, on: boolean) {
    setBusy(pair)
    setError('')
    try {
      const data = await put<AcpRuntimeOut>(ACP_RUNTIME_PATH, { switches: { [pair]: on } })
      queryClient.setQueryData(ACP_RUNTIME_QUERY_KEY, data)
    } catch (err) {
      setError(`Not saved: ${apiDetail(err, 'the server refused the switch.')}`)
    } finally {
      setBusy(null)
    }
  }

  if (!settled) return <LoadingState />
  if (rows.length === 0) return <EmptyState title="Providers did not load." />

  const laned = rows.map(row => {
    const lanes = lanesOf(row, switches)
    return { row, lanes, gaps: gapsOf(row, lanes) }
  })
  const shared = sharedGapsOf(laned)
  const sharedIds = new Set(shared.map(gap => gap.id))
  return (
    <Stack spacing={2} data-testid="providers-section">
      {error && <Banner severity="error">{error}</Banner>}
      {runtime.isError && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }} data-testid="providers-switch-unloaded">
          Runtime switches did not load.
        </Typography>
      )}
      <Summary laned={laned} />
      {shared.length > 0 && (
        <>
          <Divider />
          <SharedGaps gaps={shared} all={sharedAll} setAll={setSharedAll} />
        </>
      )}
      {laned.map(({ row, lanes, gaps }) => (
        <Stack key={row.value} spacing={2}>
          <Divider />
          <ProviderBlock
            row={row}
            lanes={lanes}
            gaps={gaps}
            sharedIds={sharedIds}
            onSharedJump={() => setSharedAll(true)}
            busy={busy}
            onFlip={(pair, on) => { void flip(pair, on) }}
          />
        </Stack>
      ))}
    </Stack>
  )
}

/** How long a jump's landing stays lit. */
const JUMP_LIT_MS = 1500
/** Each lit landing's unlight timer: a second jump to it restarts the clock rather than ending it early. */
const litTimers = new WeakMap<HTMLElement, number>()

/* A jump lights its landing for a moment, for a mouse or a tap as for a key: the focus ring alone is
 * keyboard-only, so a clicked jump scrolled the page with nothing to say where it landed. */
function jumpTo(anchor: string) {
  const target = document.getElementById(anchor)
  if (!target) return
  const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  target.scrollIntoView({ block: 'start', behavior: still ? 'auto' : 'smooth' })
  target.focus({ preventScroll: true })
  target.setAttribute('data-jumped', 'true')
  window.clearTimeout(litTimers.get(target))
  litTimers.set(target, window.setTimeout(() => target.removeAttribute('data-jumped'), JUMP_LIT_MS))
}

function JumpLink({ anchor, label, onJump, children }: {
  anchor: string
  label?: string
  /** Runs before the jump: what unfolds the landing. */
  onJump?: () => void
  children: ReactNode
}): JSX.Element {
  return (
    <Link component="button" type="button" variant="body2" aria-label={label} onClick={() => { onJump?.(); jumpTo(anchor) }}
      sx={{ textAlign: 'left', verticalAlign: 'baseline' }}>
      {children}
    </Link>
  )
}

/** Where a jump lands: focusable without joining the tab order, outlined only for the keyboard. */
const JUMP_TARGET_SX = {
  scrollMarginTop: 8,
  borderRadius: 1,
  transition: 'background-color 600ms',
  '&:focus-visible': { outline: 2, outlineColor: 'primary.main', outlineOffset: 4 },
  '&:focus:not(:focus-visible)': { outline: 'none' },
  // Settings' search-jump tint (`settings/KnobSection`), so a landing reads the same anywhere on the page.
  '&[data-jumped]': { bgcolor: (theme: Theme) => alpha(theme.palette.warning.main, 0.18) },
} as const

/** " · "-joined words that break only between items, never inside one ("rate-limit reset"): a whole cell kept on one
 *  line widened the table past the card at 1280px and cut the Gaps column off. */
function Words({ text }: { text: string }): JSX.Element {
  const items = text.split(' · ')
  return (
    <>
      {/* The space between items sits OUTSIDE each nowrap span: inside one it is no break opportunity. */}
      {items.map((item, i) => (
        <Fragment key={item}>
          <Box component="span" sx={{ whiteSpace: 'nowrap' }}>{item}{i < items.length - 1 && ' ·'}</Box>
          {i < items.length - 1 && ' '}
        </Fragment>
      ))}
    </>
  )
}

/** Muted: a capability the server says is off, unlike "—", which it does not report. */
function Off({ children }: { children: ReactNode }): JSX.Element {
  return <Box component="span" sx={{ color: 'text.secondary' }}>{children}</Box>
}

/** The provider × runtime/questions/usage/views/gaps table; on a phone, where six columns do not fit, a jump list
 *  that keeps each provider's runtime. */
function Summary({ laned }: { laned: { row: ProviderChoice; lanes: Lane[]; gaps: Gap[] | undefined }[] }): JSX.Element {
  const phone = useMediaQuery(PHONE_QUERY)
  if (phone) {
    return (
      <Box component="nav" aria-label="Providers" data-testid="providers-summary"
        sx={{ display: 'grid', gridTemplateColumns: 'minmax(0, max-content) minmax(0, 1fr) max-content', columnGap: 2, rowGap: 1 }}>
        {laned.map(({ row, lanes, gaps }) => (
          <Box key={row.value} data-testid={`summary-${row.value}`} sx={{ display: 'contents' }}>
            <JumpLink anchor={anchorOf(row.value)}>{row.label}</JumpLink>
            <Typography variant="body2" component="span" sx={{ color: 'text.secondary' }}>{runtimeWordOf(lanes)}</Typography>
            {/* The table's Gaps column, kept: the count, and the way to them. */}
            <Typography variant="body2" component="span" data-testid={`summary-${row.value}-gaps`} sx={{ textAlign: 'right' }}>
              <GapCount row={row} gaps={gaps} phone />
            </Typography>
          </Box>
        ))}
      </Box>
    )
  }
  return (
    <Box data-testid="providers-summary">
      <DataTable ariaLabel="Providers summary" nested>
        <TableHead>
          <TableRow>
            {['Provider', 'Runtime', 'Questions', 'Usage', 'Views', 'Gaps'].map(h => <TableCell key={h}>{h}</TableCell>)}
          </TableRow>
        </TableHead>
        <TableBody>
          {laned.map(({ row, lanes, gaps }) => {
            const questions = questionsOf(row)
            const usage = capabilitiesOf(row).usage
            const spent = usage && [usage.tokens && 'tokens', usage.cost && 'cost', usage.rate_limit_reset && 'rate-limit reset']
              .filter((word): word is string => Boolean(word))
            return (
              <TableRow key={row.value} data-testid={`summary-${row.value}`}>
                <TableCell component="th" scope="row" sx={{ whiteSpace: 'nowrap' }}>
                  <JumpLink anchor={anchorOf(row.value)}>{row.label}</JumpLink>
                </TableCell>
                <TableCell><Words text={runtimeWordOf(lanes)} /></TableCell>
                <TableCell>{questions === undefined ? ABSENT : questions ?? <Off>none</Off>}</TableCell>
                <TableCell>{usage === undefined ? ABSENT : spent?.length ? <Words text={spent.join(' · ')} /> : <Off>none</Off>}</TableCell>
                <TableCell>{viewsOf(row, lanes)}</TableCell>
                <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}><GapCount row={row} gaps={gaps} /></TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </DataTable>
    </Box>
  )
}

/** A provider's gap count, a jump to its Gaps; "none" muted, "—" when the server serves none. On a phone in words
 *  ("2 gaps"): there it has no column header to name it. */
function GapCount({ row, gaps, phone = false }: { row: ProviderChoice; gaps: Gap[] | undefined; phone?: boolean }): JSX.Element {
  if (gaps === undefined) return <>{ABSENT}</>
  const words = `${gaps.length} ${gaps.length === 1 ? 'gap' : 'gaps'}`
  if (gaps.length === 0) return <Off>{phone ? 'no gaps' : 'none'}</Off>
  return <JumpLink anchor={gapsAnchorOf(row.value)} label={`${row.label}: ${words}`}>{phone ? words : gaps.length}</JumpLink>
}

/** Past this many, the shared gaps fold behind "Show all": a long block pushed the provider cards a screen down. */
const SHARED_SHOWN = 3

/** Each gap several providers serve word for word, said once with the providers it applies to. */
function SharedGaps({ gaps, all, setAll }: { gaps: SharedGap[]; all: boolean; setAll: (all: boolean) => void }): JSX.Element {
  // Folding one would hide no more than the button takes.
  const folds = gaps.length > SHARED_SHOWN + 1
  const shown = folds && !all ? gaps.slice(0, SHARED_SHOWN) : gaps
  return (
    <Box component="section" id={SHARED_ANCHOR} tabIndex={-1} aria-labelledby={`${SHARED_ANCHOR}-name`}
      data-testid={SHARED_ANCHOR} sx={JUMP_TARGET_SX}>
      <Typography id={`${SHARED_ANCHOR}-name`} variant="subtitle1" component="h3" sx={{ fontWeight: 600, mb: 1 }}>Shared gaps</Typography>
      <Box component="ul" sx={{ m: 0, pl: 2.25, typography: 'body2', '& li + li': { mt: 0.75 } }}>
        {shown.map(gap => (
          <li key={gap.id}>
            <Box component="span" sx={{ fontWeight: 600 }}>{gap.capability}</Box>
            {' — '}
            {gap.consequence}
            <Box component="span" sx={{ display: 'block', color: 'text.secondary' }}>{gap.providers.join(' · ')}</Box>
          </li>
        ))}
      </Box>
      {folds && (
        <Button size="small" onClick={() => setAll(!all)} aria-expanded={all} data-testid="providers-shared-gaps-toggle" sx={{ mt: 0.5, ml: -0.5 }}>
          {all ? 'Show fewer' : `Show all ${gaps.length}`}
        </Button>
      )}
    </Box>
  )
}

/** Names that are on, then the off ones muted as "<name>: none"; one muted "none" when every one is off; "—" when
 *  the server serves no such group. */
function Presence({ items }: { items: [string, boolean][] | undefined }): JSX.Element {
  if (!items) return <>{ABSENT}</>
  const on = items.filter(([, has]) => has).map(([name]) => name)
  if (on.length === 0) return <Off>none</Off>
  const off = items.filter(([, has]) => !has).map(([name]) => `${name}: none`)
  return (
    <>
      {on.join(' · ')}
      {off.length > 0 && <Off>{on.length > 0 && ' · '}{off.join(' · ')}</Off>}
    </>
  )
}

function ProviderBlock({ row, lanes, gaps, sharedIds, onSharedJump, busy, onFlip }: {
  row: ProviderChoice
  lanes: Lane[]
  gaps: Gap[] | undefined
  sharedIds: Set<string>
  onSharedJump: () => void
  busy: string | null
  onFlip: (pair: string, on: boolean) => void
}): JSX.Element {
  const caps = capabilitiesOf(row)
  const { sessions, turns, requests, usage } = caps
  const consoleCaps = caps.console
  const questions = questionsOf(row)
  // The catalogue's console reason is about a lane on ACP; a Runtime line or a gap that already gives a reason, or no
  // lane on ACP, leaves it nothing to add.
  const consoleReason = !lanes.some(lane => lane.reason) && lanes.some(lane => lane.runtime === 'acp')
    && !gaps?.some(gap => gap.consequence === consoleCaps?.reason) ? consoleCaps?.reason : undefined

  return (
    <Box
      component="section"
      id={anchorOf(row.value)}
      tabIndex={-1}
      aria-labelledby={`provider-${row.value}-name`}
      data-testid={`provider-${row.value}`}
      sx={{ minWidth: 0, ...JUMP_TARGET_SX }}
    >
      <Typography id={`provider-${row.value}-name`} variant="subtitle1" component="h3" sx={{ fontWeight: 600, mb: 1 }}>{row.label}</Typography>
      <Box
        component="dl"
        sx={{
          m: 0,
          display: 'grid',
          gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: '7.5rem minmax(0, 1fr)' },
          columnGap: 2,
          rowGap: { xs: 0.25, sm: 0.75 },
          '& dt': { color: 'text.secondary', typography: 'body2', mt: { xs: 0.75, sm: 0 } },
          '& dd': { m: 0, typography: 'body2', overflowWrap: 'anywhere', minWidth: 0 },
        }}
      >
        <Fact term="Runtime"><RuntimeLines provider={row} lanes={lanes} busy={busy} onFlip={onFlip} /></Fact>
        <Fact term="Modes"><Modes row={row} /></Fact>
        <Fact term="Sessions">
          <Presence items={sessions && [
            ['assigned ID', sessions.assign_id], ['listing', sessions.list], ['closing', sessions.close],
            ['extra dirs', sessions.additional_dirs], [sessions.resume === 'load' ? 'loading' : 'resuming', sessions.resume !== 'none'],
          ]} />
        </Fact>
        <Fact term="Turns"><Presence items={turns && [['steering', turns.steer], ['interrupting', turns.interrupt]]} /></Fact>
        <Fact term="Requests">
          <Presence items={requests && [
            ['approvals', requests.approval],
            [questions ? `questions (${questions})` : 'questions', Boolean(questions)],
          ]} />
        </Fact>
        <Fact term="Usage">
          <Presence items={usage && [['tokens', usage.tokens], ['cost', usage.cost], ['rate-limit reset', usage.rate_limit_reset]]} />
        </Fact>
        <Fact term="Views">
          <span data-testid={`provider-${row.value}-views`}>{viewsOf(row, lanes)}</span>
          {consoleReason && <Box component="span" sx={{ display: 'block', color: 'text.secondary' }}>{consoleReason}</Box>}
        </Fact>
        <Fact term="Gaps" anchor={gapsAnchorOf(row.value)}><Gaps provider={row.value} gaps={gaps} sharedIds={sharedIds} onSharedJump={onSharedJump} /></Fact>
      </Box>
    </Box>
  )
}

/** A term and its value; `anchor` makes the value a jump target. */
function Fact({ term, anchor, children }: { term: string; anchor?: string; children: ReactNode }): JSX.Element {
  return (
    <>
      <dt>{term}</dt>
      {anchor ? <Box component="dd" id={anchor} tabIndex={-1} sx={JUMP_TARGET_SX}>{children}</Box> : <dd>{children}</dd>}
    </>
  )
}

/** A small text toggle that says what it reveals. */
function Reveal({ open, onClick, children, controls }: { open: boolean; onClick: () => void; children: ReactNode; controls: string }): JSX.Element {
  return (
    <Button
      size="small"
      variant="text"
      aria-expanded={open}
      aria-controls={controls}
      onClick={onClick}
      sx={{ p: 0, minWidth: 0, verticalAlign: 'baseline', typography: 'body2', fontWeight: 600, textTransform: 'none' }}
    >
      {children}
    </Button>
  )
}

/** Natively enforced modes inline; the rest behind "+N not enforced". */
function Modes({ row }: { row: ProviderChoice }): JSX.Element {
  const [open, setOpen] = useState(false)
  const modes = capabilitiesOf(row).runtime_modes
  if (!modes) return <>{ABSENT}</>
  if (modes.supported.length === 0) return <Off>none</Off>
  const native = new Set(modes.native)
  const enforced = modes.supported.filter(mode => native.has(mode))
  const loose = modes.supported.filter(mode => !native.has(mode))
  const id = `provider-${row.value}-loose-modes`
  return (
    <Box data-testid={`provider-${row.value}-modes`}>
      {enforced.join(' · ')}
      {loose.length > 0 && (
        <>
          {enforced.length > 0 && ' · '}
          <Reveal open={open} onClick={() => setOpen(!open)} controls={id}>+{loose.length} not enforced</Reveal>
          <Box id={id} hidden={!open} sx={{ color: 'warning.dark' }}>
            {loose.join(' · ')} — {row.label} does not enforce these itself: a run in one is not stopped from doing what
            the mode forbids.
          </Box>
        </>
      )}
    </Box>
  )
}

/** The provider's own gaps, then the fleet-harness ones (guards, hooks, subagents — `Degradation.harness`), counted
 * apart so what differs between providers stands out; a gap shared word for word with another provider only named,
 * with a link to the Shared gaps block that says it, unfolded. */
function Gaps({ provider, gaps, sharedIds, onSharedJump }: {
  provider: string
  gaps: Gap[] | undefined
  sharedIds: Set<string>
  onSharedJump: () => void
}): JSX.Element {
  if (!gaps) return <>{ABSENT}</>
  if (gaps.length === 0) return <Off>none</Off>
  // A gap the Runtime line states stays here, pointing at Runtime, even when other providers share it.
  const isShared = (gap: Gap) => gap.consequence !== null && sharedIds.has(gap.id)
  const shared = gaps.filter(isShared)
  const own = gaps.filter(gap => !gap.harness && !isShared(gap))
  const harness = gaps.filter(gap => gap.harness && !isShared(gap))
  return (
    <Stack spacing={0.5}>
      {own.length > 0 && <GapGroup id={`provider-${provider}-gap`} noun="gap" gaps={own} />}
      {harness.length > 0 && <GapGroup id={`provider-${provider}-harness-gap`} noun="harness gap" gaps={harness} />}
      {shared.length > 0 && (
        <Box data-testid={`provider-${provider}-shared-gaps`}>
          <JumpLink anchor={SHARED_ANCHOR} onJump={onSharedJump}>
            <Box component="span" sx={{ fontWeight: 600 }}>{shared.length} shared {shared.length === 1 ? 'gap' : 'gaps'}</Box>
          </JumpLink>
          : {shared.map(gap => gap.capability).join(' · ')}
        </Box>
      )}
    </Stack>
  )
}

/** "N gaps" with the capability names; expanded, each with its consequence. */
function GapGroup({ id, noun, gaps }: { id: string; noun: string; gaps: Gap[] }): JSX.Element {
  const [open, setOpen] = useState(false)
  return (
    <Box data-testid={`${id}s`}>
      <Reveal open={open} onClick={() => setOpen(!open)} controls={`${id}-list`}>
        {gaps.length} {gaps.length === 1 ? noun : `${noun}s`}
      </Reveal>
      {!open && <Box component="span" data-testid={`${id}-names`}>: {gaps.map(gap => gap.capability).join(' · ')}</Box>}
      <Box component="ul" id={`${id}-list`} hidden={!open} sx={{ m: 0, mt: 0.5, pl: 2.25 }}>
        {gaps.map(gap => (
          <li key={gap.key}>
            <Box component="span" sx={{ fontWeight: 600 }}>{gap.capability}</Box>
            <GapConsequence gap={gap} />
          </li>
        ))}
      </Box>
    </Box>
  )
}

/** Why a gap matters. One the Runtime line states names the lanes and their runtime inline, the same words at every
 *  width (a phone's Runtime line is screens up), or nothing when the capability already names them. */
function GapConsequence({ gap }: { gap: Gap }): JSX.Element | null {
  if (gap.consequence !== null) return <>{' — '}{gap.consequence}</>
  return gap.lanes ? <>{' — '}<Off>{gap.lanes}.</Off></> : null
}

/** One line per lane; one line for all when every lane has the same fixed runtime and reason. */
function RuntimeLines({ provider, lanes, busy, onFlip }: {
  provider: ProviderChoice
  lanes: Lane[]
  busy: string | null
  onFlip: (pair: string, on: boolean) => void
}): JSX.Element {
  const first = lanes[0]
  if (!first) return <>{ABSENT}</>
  if (lanes.length > 1 && first.only && lanes.every(lane => lane.only === first.only && lane.reason === first.reason)) {
    return <FixedLine testId={`runtime-${provider.value}`} label={lanes.map(lane => lane.label).join(', ')} lane={first} />
  }
  return (
    <Stack spacing={0.5}>
      {lanes.map(lane => {
        const pair = pairKey(provider.value, lane.kind)
        if (lane.only) return <FixedLine key={lane.kind} testId={`runtime-${pair}`} label={lane.label} lane={lane} />
        if (!lane.served) return <Box key={lane.kind} data-testid={`runtime-${pair}`}>{lane.label}: {ABSENT}</Box>
        return (
          <Box key={lane.kind} data-testid={`runtime-${pair}`} sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', columnGap: 1 }}>
            <span>{lane.label}</span>
            <Switch
              size="small"
              checked={lane.served.on}
              disabled={busy !== null}
              onChange={e => onFlip(pair, e.target.checked)}
              slotProps={{ input: { 'aria-label': `${provider.label} ${lane.label} ACP runtime` } }}
            />
            <Box component="span" sx={{ color: 'text.secondary' }}>
              {busy === pair ? 'Saving…' : RUNTIME_WORD[lane.served.on ? 'acp' : 'terminal']}
            </Box>
          </Box>
        )
      })}
    </Stack>
  )
}

function FixedLine({ testId, label, lane }: { testId: string; label: string; lane: Lane }): JSX.Element {
  return (
    <Box data-testid={testId}>
      {label}: <Box component="span" sx={{ fontWeight: 600 }}>{lane.only}</Box>
      {lane.reason && <Box component="span" sx={{ color: 'text.secondary' }}> — {lane.reason}</Box>}
    </Box>
  )
}
