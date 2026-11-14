import { useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { useAuth, useCan, useCanOnAnyProject } from '../../auth'
import { priceTagText } from '../../lib/billing'
import { countOf, humanize, percent } from '../../lib/humanize'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { useOrgScope } from '../../lib/orgScope'
import { danger, useConfirm } from '../../components/ConfirmDialog'
import { useApiRead } from '../../hooks/useApiRead'
import { useBusyIds } from '../../hooks/useBusyIds'
import { CompactVerbLabel, compactVerbProps } from '../../ui/CompactVerb'
import Banner from '../../ui/Banner'
import { DisabledReason } from '../../ui/DisabledReason'
import { VendorLogo } from '../../ui/credentialMarks'
import CardGrid from '../../ui/gallery/CardGrid'
import GalleryCard, { GALLERY_THUMB_PX } from '../../ui/gallery/GalleryCard'
import { tapTargetSx } from '../../ui/listStyles'
import LoadingState from '../../ui/LoadingState'
import { StatusChip } from '../../ui/StatusChip'
import PageHeader from '../../ui/PageHeader'
import { useAgentsVerbs } from './agentsTabs'
import {
  CONNECTORS_REPO_URL,
  listMarketplace,
  type MarketplaceEntry,
  type MarketplaceKind,
  publishConnector,
  type QuotaState,
  removeConnector,
  setConnectorEnabled,
} from './api'
import { type Notice, NoticeBanner, PublishConnectorDialog } from './agentFields'
import { runWithNotice } from './noticeAction'
import EmptyState from '../../ui/EmptyState'
import Tip from '../../ui/Tip'

/* ── CUSTOMIZE > AGENTS > CATALOG: THE AGENT MARKETPLACE ────────────────────────────────────────────────────
   `GET /api/agent-marketplace` (`apps/api/app/routes/agent_marketplace.py`): every agent this org can use, in four
   sections — Built-in, Your agents, Catalog, Registry — each row (`ui/gallery/GalleryCard`, the style guide's Gallery)
   naming the personas it takes, a cost band (built-ins only), how many live nodes can run it, its outcome-ledger
   profile and whether the agent router may pick it (in words, the router's own sentence). Every number is Jaah's
   own: the page renders only the entry's whitelisted fields,
   never a manifest, so nothing an agent's publisher reports about itself can show.
   Open to every `agents:view` holder whatever the plan (`App.tsx`). Enable/Disable on a catalog connector is the
   org's (`routes/agents.py`, `agents:edit` org-wide), so the org admin seat gates the control; while the org's grid
   cell for it is off, Enable stays disabled with the reason (`enable_refusal`). Publish and Remove are the
   operator's. */

const SECTIONS: readonly { kind: MarketplaceKind; title: string }[] = [
  { kind: 'builtin', title: 'Built-in' },
  { kind: 'org_agent', title: 'Your agents' },
  { kind: 'catalog', title: 'Catalog' },
  { kind: 'registry', title: 'Registry' },
]

const QUOTA_WORDS: Readonly<Record<QuotaState, string | null>> = {
  ok: 'accounts ready',
  tiered: 'API-key spillover only',
  blocked: 'every account at its quota',
  no_account: 'no account',
  not_applicable: null,
}

function nodesText(entry: MarketplaceEntry): string {
  const n = entry.availability.capable_nodes_online
  const nodes = n === 0 ? 'No live node can run it' : `${countOf(n, 'live node')} can run it`
  const quota = QUOTA_WORDS[entry.availability.quota_state]
  return quota ? `${nodes} · ${quota}` : nodes
}

const figure = (value: number) => (Number.isInteger(value) ? String(value) : value.toFixed(2))
/** The ledger's survival is a rate, its cost per success dollars; any other figure is shown as it comes. */
const METRIC_FORMAT: Record<string, (value: number) => string> = {
  survival: percent,
  cost_per_success: value => `$${value.toFixed(2)}`,
}

function ledgerText(entry: MarketplaceEntry): string {
  const { state, n, metrics } = entry.ledger
  if (state === 'no_ledger') return 'No ledger data yet'
  if (state === 'insufficient') return `Insufficient data · ${countOf(n, 'outcome')}`
  const shown = (metrics ?? []).map(m => {
    const format = METRIC_FORMAT[m.key] ?? figure
    const interval = m.low != null && m.high != null ? ` (${format(m.low)}–${format(m.high)})` : ''
    return `${humanize(m.key)} ${format(m.value)}${interval}`
  })
  return [...shown, `${n} outcomes`].join(' · ')
}

const entryKey = (entry: MarketplaceEntry) => `${entry.kind}:${entry.id ?? entry.provider}`

export default function CatalogPage() {
  const { admin } = useAuth()
  const can = useCan()
  const canOnAny = useCanOnAnyProject()
  const isOperator = admin?.is_operator === true
  const scope = useOrgScope()
  // The marketplace is the caller's own org's; an operator's Orgs filter naming another org does not change it.
  const filterIgnored = isOperator && scope !== null && !(scope.length === 1 && scope[0].id === admin?.org?.id)
  const { canPublish } = useAgentsVerbs()
  /* On or off for the WHOLE org takes `agents:edit` org-wide (`authz.require_org_key`); `useCan` also answers for a
   * project-scoped grant, so the org admin seat is the presentation gate. The server stays the boundary. */
  const canToggle = isOrgAdminSeat(admin)
  // An agent editor on some project, but not an org admin: say why there is no toggle.
  const editsElsewhere = !canToggle && (can('agents:edit') || canOnAny('agents:edit'))
  const { data, loading, error, refresh } = useApiRead(['agent-marketplace'], () => listMarketplace(), 'Could not load the catalog')
  const [busy, withBusy] = useBusyIds()
  const [notice, setNotice] = useState<Notice | null>(null)
  const [publishing, setPublishing] = useState(false)
  const [confirm, confirmDialog] = useConfirm()

  const run = (entry: MarketplaceEntry, done: string, call: () => Promise<unknown>) => withBusy(entryKey(entry), () =>
    runWithNotice(setNotice, call, done, `Could not change ${entry.display_name}`),
  ).finally(refresh)

  async function withdraw(entry: MarketplaceEntry) {
    const ok = await confirm(danger('Remove', {
      title: `Remove ${entry.display_name}?`,
      message: 'It leaves the catalog. Orgs that enabled it see it marked removed, and their tasks using it fail to start.',
    }))
    if (ok) await run(entry, `Removed ${entry.display_name} from the catalog.`, () => removeConnector(entry.id!))
  }

  const entries = data ?? []
  const hasCatalog = entries.some(e => e.kind === 'catalog')
  return (
    <Box>
      {/* Titleless: Publish portals onto the Agents strip (`components/TabActionsSlot`), wordless on a phone. */}
      <PageHeader
        actions={canPublish && (
          <Tip title="Publish"><Button variant="contained" color="primary" {...compactVerbProps('Publish', tapTargetSx)} onClick={() => setPublishing(true)}>
            <CompactVerbLabel label="Publish" />
          </Button></Tip>
        )}
      />
      {error && <Banner severity="error">{error}</Banner>}
      {filterIgnored && (
        <Typography variant="body2" sx={{ color: 'text.secondary', pt: 1 }} data-testid="catalog-scope-note">
          Showing your own org. The Orgs filter does not apply here.
        </Typography>
      )}
      {editsElsewhere && hasCatalog && (
        <Typography variant="body2" sx={{ color: 'text.secondary', pt: 1 }} data-testid="catalog-toggle-note">
          Only admins can enable catalog agents.
        </Typography>
      )}
      <NoticeBanner notice={notice} onDismiss={() => setNotice(null)} testId="catalog-notice" />
      {loading && data === null ? (
        <LoadingState rows={3} />
      ) : SECTIONS.map(({ kind, title }) => {
        const shown = entries.filter(e => e.kind === kind)
        // An empty section is dropped, except Catalog, whose empty state points at how to add a connector.
        if (shown.length === 0 && kind !== 'catalog') return null
        return (
          <Box component="section" key={kind} data-testid={`marketplace-section-${kind}`} sx={{ pt: 2 }}>
            <Typography variant="subtitle1" component="h2" sx={{ pb: 1 }}>
              {title} <Box component="span" sx={{ color: 'text.secondary' }}>{shown.length}</Box>
            </Typography>
            {shown.length === 0 ? (
              <EmptyState
                inline
                title="No connectors yet."
                action={
                  <Tip title="Build one with the Jaah agent SDK; the repo's CONTRIBUTING guide has the steps."><Button
                    component="a" href={CONNECTORS_REPO_URL} target="_blank" rel="noopener noreferrer" size="small"
                    data-testid="catalog-contribute"
                  >
                    Contribute one
                  </Button></Tip>
                }
              />
            ) : (
              <CardGrid label={title} testId={`marketplace-grid-${kind}`} rows>
                {shown.map(entry => (
                  <EntryCard
                    key={entryKey(entry)}
                    entry={entry}
                    busy={busy.has(entryKey(entry))}
                    canToggle={canToggle && kind === 'catalog'}
                    canRemove={isOperator && kind === 'catalog'}
                    onToggle={enabled => void run(
                      entry,
                      `${enabled ? 'Enabled' : 'Disabled'} ${entry.display_name}.`,
                      () => setConnectorEnabled(entry.id!, enabled),
                    )}
                    onRemove={() => void withdraw(entry)}
                  />
                ))}
              </CardGrid>
            )}
          </Box>
        )
      })}
      {publishing && (
        <PublishConnectorDialog
          initial={{ repo_url: CONNECTORS_REPO_URL, subpath: '', ref: '' }}
          onClose={() => setPublishing(false)}
          onSave={async body => {
            const connector = await publishConnector(body)
            setNotice({ severity: 'success', lines: [`Published ${connector.display_name}.`] })
            await refresh()
          }}
        />
      )}
      {confirmDialog}
    </Box>
  )
}

function EntryCard({ entry, busy, canToggle, canRemove, onToggle, onRemove }: {
  entry: MarketplaceEntry
  busy: boolean
  canToggle: boolean
  canRemove: boolean
  onToggle: (enabled: boolean) => void
  onRemove: () => void
}) {
  const { removed, unsupported } = entry
  const enabled = entry.availability.enabled
  // An SDK v1 agent cannot run, so it has nothing to turn on: no toggle, the reason on its mark.
  const toggles = canToggle && !unsupported && (enabled || !removed)
  const band = entry.cost_band
  const refusal = entry.enable_refusal ?? undefined
  const personaList = entry.personas.map(p => p.label).join(', ') || 'No personas'
  /* The row's line is the short form that fits its column: personas (a built-in takes every task persona: one phrase
   * rather than thirteen names), cost band, live nodes. Its hover adds the price range, quota, ledger and router. */
  const facts = (full: boolean) => [
    full || entry.kind !== 'builtin' ? personaList : 'Every task persona',
    !band ? 'No price' : !full ? band.band
      : `${band.band} ${priceTagText(Math.round(band.low_usd * 100), 'usd')}–${priceTagText(Math.round(band.high_usd * 100), 'usd')} per million output tokens`,
    ...(full
      ? [nodesText(entry), ledgerText(entry), entry.router.reason]
      : [entry.availability.capable_nodes_online ? countOf(entry.availability.capable_nodes_online, 'live node') : 'No live node']),
  ].join(' · ')
  return (
    <GalleryCard
      testId="catalog-card"
      attrs={{ 'data-kind': entry.kind, 'data-entry': entry.id ?? entry.provider ?? undefined, 'aria-busy': busy || undefined }}
      name={entry.display_name}
      summary={entry.description}
      image={null}
      mediaRatio="1 / 1"
      placeholder={<VendorLogo url={null} label={entry.display_name} size={GALLERY_THUMB_PX} />}
      meta={<Tip title={facts(true)}><span tabIndex={0} data-testid="marketplace-facts">{facts(false)}</span></Tip>}
      aside={removed
        ? <StatusChip tone="error" label="Removed from catalog" glyph="✕" title="Removed from catalog. Tasks using it fail to start. Pick another agent for them." labelTestId="catalog-removed" />
        : unsupported
          ? <StatusChip tone="neutral" label="Unsupported" title={unsupported} testId="catalog-unsupported" />
          : enabled
            ? <StatusChip tone="success" label="Enabled" glyph="✓" labelTestId="catalog-enabled" />
            : <StatusChip tone="neutral" label={entry.kind === 'builtin' ? 'Not on plan' : 'Off'} labelTestId="catalog-off" />}
      actions={(toggles || (canRemove && !removed)) && (
        <>
          {toggles && (
            <DisabledReason reason={enabled ? undefined : refusal}>
              <Button
                size="small"
                variant={enabled ? 'outlined' : 'contained'}
                disabled={busy || (!enabled && refusal !== undefined)}
                onClick={() => onToggle(!enabled)}
                aria-label={`${enabled ? 'Disable' : 'Enable'} ${entry.display_name}`}
              >
                {enabled ? 'Disable' : 'Enable'}
              </Button>
            </DisabledReason>
          )}
          {canRemove && !removed && (
            <Button size="small" color="error" disabled={busy} onClick={onRemove} aria-label={`Remove ${entry.display_name}`}>
              Remove
            </Button>
          )}
        </>
      )}
    />
  )
}
