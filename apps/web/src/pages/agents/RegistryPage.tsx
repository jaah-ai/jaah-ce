import { useState } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { useAuth } from '../../auth'
import { useApiRead } from '../../hooks/useApiRead'
import { useBusyIds } from '../../hooks/useBusyIds'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { NotOnPlan, offPlan } from '../../lib/planSwitches'
import Banner from '../../ui/Banner'
import BusyButton from '../../ui/BusyButton'
import { VendorLogo } from '../../ui/credentialMarks'
import EmptyState from '../../ui/EmptyState'
import { FilterRow, FilterSearch, matchesAny } from '../../ui/FilterRow'
import CardGrid from '../../ui/gallery/CardGrid'
import GalleryCard, { GALLERY_THUMB_PX } from '../../ui/gallery/GalleryCard'
import { usePhone } from '../../hooks/usePhone'
import LoadingState from '../../ui/LoadingState'
import NoMatchesState from '../../ui/NoMatchesState'
import { SelectFilters, type SelectNarrowing } from '../../ui/SelectFilters'
import { StatusChip } from '../../ui/StatusChip'
import Tip from '../../ui/Tip'
import { DISTRIBUTION_LABEL, installRegistryAgent, listRegistry, type RegistryEntry } from './api'
import { type Notice, NoticeBanner } from './agentFields'
import { runWithNotice } from './noticeAction'

/* ── CUSTOMIZE > AGENTS > REGISTRY: THE ACP REGISTRY ───────────────────────────────────────────────────────
   `GET /api/agent-registry` — every entry of the public ACP registry (fetched server-side, cached), each with
   whether this org can install it and, once installed, the version it pinned. Browsing takes `agents:view`;
   Install (`POST /api/agent-registry/{id}/install`) is the org admin's (`principal.is_org_admin`), so the
   button shows on the org admin seat only and anyone else is told why. The server re-reads the registry and
   picks the distribution itself; a refused entry (a key only an environment variable can carry, no Linux build)
   shows the server's sentence where the button would be. An installed agent is an ordinary row on the Agents
   tab, pinned: uninstall is its Delete there. While the provider grid has no `acp:` row on (`acp_registry`) the tab and its route are
   not offered (`agentsTabs.tsx`, `App.tsx`); the door's 403 under an open session sends the page back to Agents, which says why (`agentsTabs.tsx::RegistryOffPlanNote`). The
   style guide's Gallery: one `ui/gallery/GalleryCard` row per entry in `ui/gallery/CardGrid` (name and description one
   line each, the full text on hover); a refused entry sorts after the rest (stable otherwise). The
   filter row (name search, distribution, Installed | Installable — the last: not installed, not refused)
   narrows client-side: `ui/FilterRow`'s search beside `ui/SelectFilters`, which folds the two selects into one Filters
   menu on a phone. Install pins the version; the package itself installs on the node when a task first
   runs it, which the installed pill's hover and the success notice both say (the install itself takes under a second).
   An installing card says so itself (`ui/BusyButton`, `aria-busy`); the others stay usable. Each card's icon arrives
   inline as a `data:` URI (`app/acp_registry_icons.py`: the CSP admits `data:` images, not the registry's
   CDN), drawn on `ui/credentialMarks::VendorLogo`'s light tile (many registry icons are one black shape), else its
   monogram. */

/** The list door's plan refusal, held as the answer (the switch can turn off under an open session). */
const OFF_PLAN = 'off-plan' as const

type Distribution = NonNullable<RegistryEntry['distribution']>

type Filter = 'distribution' | 'status'

/** The two narrowings; '' is no filter. */
const NARROWINGS: SelectNarrowing<Filter>[] = [
  {
    key: 'distribution', label: 'Distribution', allLabel: 'All types', testId: 'registry-filter-distribution',
    options: (Object.keys(DISTRIBUTION_LABEL) as Distribution[]).map(value => ({ value, label: DISTRIBUTION_LABEL[value] })),
  },
  {
    key: 'status', label: 'Status', allLabel: 'All statuses', testId: 'registry-filter-status',
    options: [{ value: 'installed', label: 'Installed' }, { value: 'installable', label: 'Installable' }],
  },
]

const NO_FILTERS: Record<Filter, string> = { distribution: '', status: '' }

/** What installed means: the installed pill's hover, and the install notice. */
const INSTALLED_NOTE = 'Pinned. Installs on the node when a task runs.'

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
  const [filters, setFilters] = useState(NO_FILTERS)
  const phone = usePhone()

  const install = (entry: RegistryEntry) => withBusy(entry.id, () =>
    runWithNotice(setNotice, () => installRegistryAgent(entry.id), `Installed ${entry.name}. ${INSTALLED_NOTE}`, `Could not install ${entry.name}`),
  ).finally(refresh)

  if (data === OFF_PLAN) return <NotOnPlan name="acp_registry" />

  const entries = data ?? []
  const { distribution, status } = filters
  const shown = entries
    .filter(entry =>
      matchesAny(query, entry.name)
      && (!distribution || entry.distribution === distribution)
      && (status !== 'installed' || isInstalled(entry))
      && (status !== 'installable' || (entry.installable && !isInstalled(entry))))
    .sort((a, b) => Number(isRefused(a)) - Number(isRefused(b)))
  const clearAll = () => { setQuery(''); setFilters(NO_FILTERS) }
  return (
    <Box>
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
          <Box data-testid="registry-filters" sx={{ pt: 1.5 }}>
            <FilterRow>
              <FilterSearch value={query} onChange={setQuery} ariaLabel="Search registry agents" placeholder="Name" />
              <SelectFilters
                narrowings={NARROWINGS}
                values={filters}
                onChange={(key, value) => setFilters(f => ({ ...f, [key]: value }))}
                onClear={keys => setFilters(f => ({ ...f, ...Object.fromEntries(keys.map(k => [k, ''])) }))}
                phone={phone}
                menuName="Filter registry agents"
              />
            </FilterRow>
          </Box>
          {shown.length === 0 ? (
            <NoMatchesState title="No matching agents" onClear={clearAll} testId="registry-clear-filters" />
          ) : (
            <Box sx={{ pt: 1.5 }}>
              <CardGrid label="Registry agents" testId="registry-cards" rows>
                {shown.map(entry => (
                  <EntryCard
                    key={entry.id}
                    entry={entry}
                    busy={busy.has(entry.id)}
                    canInstall={canInstall}
                    onInstall={() => void install(entry)}
                  />
                ))}
              </CardGrid>
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
    entry.license,
  ].filter(Boolean).join(' · ')
  return (
    <GalleryCard
      testId="registry-card"
      attrs={{ 'data-entry': entry.id, 'aria-busy': busy || undefined }}
      name={entry.name}
      summary={entry.description ?? ''}
      image={null}
      mediaRatio="1 / 1"
      placeholder={(
        <Box sx={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'text.primary' }}>
          <VendorLogo url={entry.icon} label={entry.name} size={GALLERY_THUMB_PX} tile />
        </Box>
      )}
      meta={(facts || (!installed && !entry.installable && entry.refusal)) && (
        <Box component="span" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5 }}>
          {facts && <span>{facts}</span>}
          {!installed && !entry.installable && entry.refusal && (
            <Tip title={entry.refusal}><Box component="span" sx={{ color: 'warning.main' }} data-testid="registry-refusal">
              {entry.refusal}
            </Box></Tip>
          )}
        </Box>
      )}
      aside={installed && (
        <StatusChip
          tone="success"
          glyph="✓"
          label={`Installed · pinned v${(entry.installed_version ?? entry.version).replace(/^v/, '')}`}
          title={INSTALLED_NOTE}
          labelTestId="registry-installed"
        />
      )}
      actions={canInstall && entry.installable && !installed && (
        <BusyButton size="small" variant="contained" busy={busy} onClick={onInstall} aria-label={`${busy ? 'Installing' : 'Install'} ${entry.name}`}>
          {busy ? 'Installing…' : 'Install'}
        </BusyButton>
      )}
    />
  )
}
