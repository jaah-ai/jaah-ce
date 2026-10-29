import { useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import Paper from '@mui/material/Paper'
import type { Theme } from '@mui/material/styles'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import useMediaQuery from '@mui/material/useMediaQuery'
import { useAuth } from '../../auth'
import { useApiRead } from '../../hooks/useApiRead'
import { useBusyIds } from '../../hooks/useBusyIds'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { NotOnPlan, offPlan } from '../../lib/planSwitches'
import Banner from '../../ui/Banner'
import EmptyState from '../../ui/EmptyState'
import { matchesAny } from '../../ui/FilterRow'
import { fieldTapTargetSx, PHONE_QUERY } from '../../ui/listStyles'
import LoadingState from '../../ui/LoadingState'
import SegmentedControl, { type SegmentedOption } from '../../ui/SegmentedControl'
import { StatusChip } from '../../ui/StatusChip'
import ToolbarMenu, { filterRadioPage } from '../../ui/ToolbarMenu'
import { AgentsStrip } from './agentsTabs'
import { DISTRIBUTION_LABEL, installRegistryAgent, listRegistry, refusalSentences, type RegistryEntry } from './api'
import { type Notice, NoticeBanner } from './SnapshotDialog'

/* ── DIRECTORY > AGENTS > REGISTRY: THE ACP REGISTRY ───────────────────────────────────────────────────────
   `GET /api/agent-registry` — every entry of the public ACP registry (fetched server-side, cached), each with
   whether this org can install it and, once installed, the version it pinned. Browsing takes `agents:view`;
   Install (`POST /api/agent-registry/{id}/install`) is the org admin's (`principal.is_org_admin`), so the
   button shows on the org admin seat only and anyone else is told why. The server re-reads the registry and
   picks the distribution itself; a refused entry (a key only an environment variable can carry, no Linux build)
   shows the server's sentence where the button would be. An installed agent is an ordinary row on the Agents
   tab, pinned: uninstall is its Delete there. While the provider grid has no `acp:` row on (`acp_registry`) the tab and its route are
   not offered (`agentsTabs.tsx`, `App.tsx`); the door's 403 under an open session sends the page back to Agents, which says why (`agentsTabs.tsx::RegistryOffPlanNote`). Cards wrap their names
   (AC-9's readability rule), one column on a phone; a refused entry sorts after the rest (stable otherwise). The
   filter row (name search, distribution, All | Installed | Installable — the last: not installed, not refused)
   narrows client-side; on a phone the two segmented controls fold into one Filters menu beside the search, so the
   row stays one line (the Customers strip's pattern), and it is one height throughout (the tap floor on a phone, the small
   field's 40px above it). Install pins the version; the package itself installs on the node when a task first
   runs it, which the installed card and the success notice both say (the install itself takes under a second).
   An installing card says so itself (spinner, `aria-busy`); the others stay usable. Each card's icon arrives
   inline as a `data:` URI (`app/acp_registry_icons.py`: the CSP admits `data:` images, not the registry's
   CDN), else a neutral initial. */

/** The list door's plan refusal, held as the answer (the switch can turn off under an open session). */
const OFF_PLAN = 'off-plan' as const

type Distribution = NonNullable<RegistryEntry['distribution']>


const DISTRIBUTION_OPTIONS: readonly SegmentedOption<Distribution | 'all'>[] = [
  { value: 'all', label: 'All types' },
  ...(Object.keys(DISTRIBUTION_LABEL) as Distribution[]).map(value => ({
    value, label: DISTRIBUTION_LABEL[value], testId: `registry-distribution-${value}`,
  })),
]

type Status = 'all' | 'installed' | 'installable'

const STATUS_OPTIONS: readonly SegmentedOption<Status>[] = [
  { value: 'all', label: 'All statuses', testId: 'registry-status-all' },
  { value: 'installed', label: 'Installed', testId: 'registry-status-installed' },
  { value: 'installable', label: 'Installable', testId: 'registry-status-installable' },
]

/** What installed means, on the card and in the install notice. */
const INSTALLED_NOTE = 'Pinned. Installs on the node when a task runs.'

/** The filter row's one height: the tap floor on a phone, the small search field's 40px above it. */
const FILTER_HEIGHT_SX = (theme: Theme) => ({ minHeight: { xs: theme.custom?.tapTargetPx ?? 44, sm: 40 } })
const SEGMENT_SX = [{ px: 1, py: 0.25, fontSize: '0.75rem' }, FILTER_HEIGHT_SX]

/** A segmented filter's options as a phone menu page's: its "all" is the page's own off choice (`filterRadioPage`). */
const narrowed = (options: readonly SegmentedOption<string>[]) =>
  options.filter(o => o.value !== 'all').map(o => ({ value: o.value, label: String(o.label) }))
const unAll = (value: string) => (value === 'all' ? '' : value)

const isInstalled = (entry: RegistryEntry) => entry.installed_agent_id !== null
/** Refused and not installed: listed, but after everything this org can use or install. */
const isRefused = (entry: RegistryEntry) => !entry.installable && !isInstalled(entry)

export default Registry

function Registry() {
  const { admin } = useAuth()
  const canInstall = isOrgAdminSeat(admin)
  const { data, loading, error, refresh } = useApiRead(
    ['agent-registry'],
    () => listRegistry().catch((err: unknown) => {
      // The door refuses an org without the term, even under an open session.
      if (offPlan(err)) return OFF_PLAN
      throw err
    }),
    'Could not load the ACP registry',
  )
  const [busy, withBusy] = useBusyIds()
  const [notice, setNotice] = useState<Notice | null>(null)
  const [query, setQuery] = useState('')
  const [distribution, setDistribution] = useState<Distribution | 'all'>('all')
  const [status, setStatus] = useState<Status>('all')
  const phone = useMediaQuery(PHONE_QUERY)

  const install = (entry: RegistryEntry) => withBusy(entry.id, async () => {
    setNotice(null)
    try {
      await installRegistryAgent(entry.id)
      setNotice({ severity: 'success', lines: [`Installed ${entry.name}. ${INSTALLED_NOTE}`] })
    } catch (err) {
      setNotice({ severity: 'error', lines: refusalSentences(err, `Could not install ${entry.name}`) })
    }
  }).finally(refresh)

  if (data === OFF_PLAN) return <NotOnPlan name="acp_registry" />

  const entries = data ?? []
  const shown = entries
    .filter(entry =>
      matchesAny(query, entry.name)
      && (distribution === 'all' || entry.distribution === distribution)
      && (status !== 'installed' || isInstalled(entry))
      && (status !== 'installable' || (entry.installable && !isInstalled(entry))))
    .sort((a, b) => Number(isRefused(a)) - Number(isRefused(b)))
  return (
    <Box>
      <AgentsStrip tab="registry" />
      {error && <Banner severity="error">{error}</Banner>}
      {!canInstall && (
        <Typography variant="body2" sx={{ color: 'text.secondary', pt: 1 }} data-testid="registry-install-note">
          Only admins can install registry agents.
        </Typography>
      )}
      <NoticeBanner notice={notice} onDismiss={() => setNotice(null)} testId="registry-notice" />
      {loading && data === null ? (
        <LoadingState rows={3} />
      ) : entries.length === 0 ? (
        !error && <EmptyState title="No registry agents" />
      ) : (
        <>
          <Box data-testid="registry-filters" sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1, pt: 1.5 }}>
            <TextField
              type="search"
              size="small"
              value={query}
              onChange={event => setQuery(event.target.value)}
              placeholder="Search by name"
              slotProps={{ htmlInput: { 'aria-label': 'Search registry agents', 'data-testid': 'registry-search' } }}
              sx={[fieldTapTargetSx, { flex: '1 1 12rem', maxWidth: { sm: '20rem' } }]}
            />
            {phone ? (
              <ToolbarMenu
                name="Filter registry agents"
                heading="Filters"
                hint="Filters"
                badgeCount={Number(distribution !== 'all') + Number(status !== 'all')}
                items={[
                  filterRadioPage('registry-filter-distribution', 'distribution', 'Distribution', 'All',
                    narrowed(DISTRIBUTION_OPTIONS), unAll(distribution), v => setDistribution((v || 'all') as Distribution | 'all')),
                  filterRadioPage('registry-filter-status', 'status', 'Status', 'All',
                    narrowed(STATUS_OPTIONS), unAll(status), v => setStatus((v || 'all') as Status)),
                ]}
                onClear={() => { setDistribution('all'); setStatus('all') }}
              />
            ) : (<>
            <SegmentedControl
              value={distribution}
              options={DISTRIBUTION_OPTIONS}
              onChange={setDistribution}
              ariaLabel="Distribution"
              testId="registry-distribution"
              buttonSx={SEGMENT_SX}
            />
            <SegmentedControl
              value={status}
              options={STATUS_OPTIONS}
              onChange={setStatus}
              ariaLabel="Status"
              testId="registry-status"
              buttonSx={SEGMENT_SX}
            />
            </>)}
          </Box>
          {shown.length === 0 ? (
            <Typography variant="body2" sx={{ color: 'text.secondary', pt: 1.5 }} data-testid="registry-no-match">
              No matching agents.
            </Typography>
          ) : (
            <Box
              data-testid="registry-cards"
              sx={{ display: 'grid', gap: 1.5, pt: 1.5, gridTemplateColumns: 'repeat(auto-fill, minmax(min(100%, 18rem), 1fr))' }}
            >
              {shown.map(entry => (
                <EntryCard
                  key={entry.id}
                  entry={entry}
                  busy={busy.has(entry.id)}
                  canInstall={canInstall}
                  onInstall={() => void install(entry)}
                />
              ))}
            </Box>
          )}
        </>
      )}
    </Box>
  )
}

function EntryCard({ entry, busy, canInstall, onInstall }: {
  entry: RegistryEntry
  busy: boolean
  canInstall: boolean
  onInstall: () => void
}) {
  const installed = isInstalled(entry)
  const facts = [
    entry.version ? `v${entry.version.replace(/^v/, '')}` : null,
    entry.distribution ? DISTRIBUTION_LABEL[entry.distribution] : null,
    entry.authors.length > 0 ? `by ${entry.authors.join(', ')}` : null,
  ].filter(Boolean).join(' · ')
  return (
    <Paper
      variant="outlined"
      data-testid="registry-card"
      data-entry={entry.id}
      aria-busy={busy || undefined}
      sx={{ p: 1.5, display: 'flex', flexDirection: 'column', gap: 0.75, minWidth: 0 }}
    >
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1 }}>
        <EntryIcon entry={entry} />
        {/* The whole name, wrapped: never clipped to a few characters. */}
        <Typography
          variant="subtitle2"
          component="h3"
          title={entry.name}
          sx={{ flex: 1, minWidth: 0, overflowWrap: 'anywhere' }}
        >
          {entry.name}
        </Typography>
        {installed && (
          <StatusChip
            tone="success"
            glyph="✓"
            label={`Installed · pinned v${(entry.installed_version ?? entry.version).replace(/^v/, '')}`}
            labelTestId="registry-installed"
          />
        )}
      </Box>
      {installed && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }} data-testid="registry-installed-note">
          {INSTALLED_NOTE}
        </Typography>
      )}
      {facts && <Typography variant="caption" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>{facts}</Typography>}
      {entry.description && <Typography variant="body2">{entry.description}</Typography>}
      {entry.license && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>{entry.license} license</Typography>
      )}
      {!installed && !entry.installable && entry.refusal && (
        <Typography variant="caption" sx={{ color: 'warning.main' }} data-testid="registry-refusal">
          {entry.refusal}
        </Typography>
      )}
      {canInstall && entry.installable && !installed && (
        <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mt: 'auto', pt: 0.5 }}>
          <Button
            size="small"
            variant="contained"
            disabled={busy}
            onClick={onInstall}
            aria-label={`${busy ? 'Installing' : 'Install'} ${entry.name}`}
            startIcon={busy ? <CircularProgress size={14} color="inherit" aria-hidden data-testid="registry-installing" /> : undefined}
          >
            {busy ? 'Installing…' : 'Install'}
          </Button>
        </Box>
      )}
    </Paper>
  )
}

const ICON_PX = 32

/** The entry's icon, or its initial on a neutral tile. Decorative (`alt=""`): the name is beside it. An `<img>` of an
 *  SVG `data:` URI never runs the SVG's script — images are rendered in secure static mode — so inlining is safe. */
function EntryIcon({ entry }: { entry: RegistryEntry }) {
  const box = { width: ICON_PX, height: ICON_PX, flexShrink: 0, borderRadius: 1 } as const
  if (entry.icon) {
    // A light tile in either theme: many registry icons are one black shape, invisible on a dark card.
    return (
      <Box
        component="img"
        src={entry.icon}
        alt=""
        data-testid="registry-icon"
        sx={{ ...box, objectFit: 'contain', p: '3px', boxSizing: 'border-box', bgcolor: '#fff', border: 1, borderColor: 'divider' }}
      />
    )
  }
  return (
    <Box
      aria-hidden
      data-testid="registry-icon-placeholder"
      sx={{ ...box, display: 'grid', placeItems: 'center', bgcolor: 'action.selected', color: 'text.secondary', typography: 'subtitle2' }}
    >
      {entry.name.trim().charAt(0).toUpperCase()}
    </Box>
  )
}
