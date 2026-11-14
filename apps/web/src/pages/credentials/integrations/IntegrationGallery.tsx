import { useMemo, useState, type JSX } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Link as RouterLink, useLocation } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import Typography from '@mui/material/Typography'
import Breadcrumb from '../../../components/Breadcrumb'
import SetupCelebration from '../../../components/onboarding/SetupCelebration'
import { useWideDetailPane } from '../../../components/ListDetail'
import { useApiRead } from '../../../hooks/useApiRead'
import { useSubmit } from '../../../hooks/useSubmit'
import { useUrlFilters } from '../../../hooks/useUrlFilters'
import { safeReturnPath } from '../../../lib/listDetail'
import { safeHref } from '../../../lib/safeHref'
import { matchesAny } from '../../../ui/FilterRow'
import CardGrid from '../../../ui/gallery/CardGrid'
import GalleryCard, { GALLERY_THUMB_PX } from '../../../ui/gallery/GalleryCard'
import { VendorLogo } from '../../../ui/credentialMarks'
import { DetailPage } from '../../../ui/detail/DetailPage'
import LoadFailedState from '../../../ui/LoadFailedState'
import LoadingState from '../../../ui/LoadingState'
import NoMatchesState from '../../../ui/NoMatchesState'
import SearchBox from '../../../ui/SearchBox'
import { StatusChip } from '../../../ui/StatusChip'
import { tapTargetSx } from '../../../ui/listStyles'
import { useIsOrgAdmin } from '../../../hooks/useIsOrgAdmin'
import { connectorUrl, newConnectorUrl } from '../custom/api'
import {
  authChips, CONNECTION_STATE, connectableWithoutApp, INTEGRATIONS_KEY, integrationPath, isBeta, listIntegrations,
  voteIntegration, type Integration,
} from './api'
import Tip from '../../../ui/Tip'

/* THE INTEGRATIONS GALLERY — `/credentials/new` with no `?type` or `?integration` (the style guide's Gallery
 * archetype, as `pages/templates/TemplatesPage.tsx`). Every catalog vendor the server lists (`GET /api/integrations`),
 * filtered here: a search over name, category and summary (`?q=`), and one category chip (`?category=`), both in the
 * URL. A card opens the vendor's Connect screen (`?integration=<slug>`); "Docs" its reader page. A Beta vendor (no
 * Jaah app yet, never said so) leads with Vote instead — one vote per person, pressed again to withdraw — and keeps a
 * plain Connect only where a key or the person's own OAuth app still connects it. Any other secret
 * type is one press away ("Other secret"), on the form's own type picker. No sidebar item: Secrets is the door. */
/** The two "Add custom" cards (`vendors/custom_connector.toml`, `custom_mcp.toml`): each defines an org's own
 *  connector of that kind, so only an Org Owner/Admin sees them; a member connects on each connector's own card. */
const ADD_CUSTOM: Record<string, 'rest' | 'mcp'> = { custom_connector: 'rest', custom_mcp: 'mcp' }

/** Where a card's primary verb goes, and its words: an org's custom connector opens its Connections tab; an "Add
 *  custom" card defines a new one; every other vendor opens its Connect screen. */
function cardTarget(item: Integration, returnTo: string | null): { to: string; label: string; aria: string } {
  if (item.custom && item.custom_connector_id) {
    return { to: connectorUrl(item.custom_connector_id, 'connections'), label: item.connected ? 'Add another' : 'Connect', aria: `Connect ${item.label}` }
  }
  const kind = ADD_CUSTOM[item.slug]
  if (kind) return { to: newConnectorUrl(kind), label: 'Add custom', aria: `Add a ${item.label.toLowerCase()}` }
  return { to: integrationPath(item.slug, returnTo), label: item.connected ? 'Add another' : 'Connect', aria: `Connect ${item.label}` }
}

/** The plain secret form's type when "Other secret" opens it: the registry's catch-all. */
const OTHER_SECRET_PATH = '/credentials/new?type=generic_login'

/** A vendor mark is square, filling the row's thumbnail. */
const CARD_MEDIA_RATIO = '1 / 1'

export default function IntegrationGallery(): JSX.Element {
  // In the list/detail pane the rows need the width for a one-line summary.
  useWideDetailPane()
  const [params, setParam] = useUrlFilters()
  const { search } = useLocation()
  const returnTo = safeReturnPath(search, window.location.origin, 'return')
  const admin = useIsOrgAdmin()
  const q = params.get('q') ?? ''
  const { data, loading, error, reload } = useApiRead(INTEGRATIONS_KEY, listIntegrations, 'Failed to load integrations')
  /* Add custom (the catalog's `custom_connector`/`custom_mcp` cards, no connector id) is an Org Owner's or Admin's;
     an org connector's own card (it carries `custom_connector_id`) shows to everyone, whatever its slug. */
  const all = useMemo(
    () => (data ?? []).filter(item => admin || Boolean(item.custom_connector_id) || !(item.slug in ADD_CUSTOM)),
    [data, admin],
  )

  /* Category chips in label order, each named once, from the vendors listed (never a client-side table). */
  const categories = useMemo(() => {
    const seen = new Map<string, string>()
    for (const item of all) if (!seen.has(item.category)) seen.set(item.category, item.category_label)
    return [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1]))
  }, [all])
  const categoryParam = params.get('category')
  const category = categories.some(([key]) => key === categoryParam) ? categoryParam : null

  const shown = all.filter(item => (category === null || item.category === category)
    && matchesAny(q, `${item.label} ${item.category_label} ${item.summary}`))

  // Only the filters clear: `?return=` (the Connect screen's way back) and any other key stay.
  const clearAll = () => setParam({ q: '', category: '' })

  let body: JSX.Element
  if (loading) body = <LoadingState label="Loading integrations…" />
  else if (error) body = <LoadFailedState title="Integrations unavailable" error={error} onRetry={() => void reload()} />
  else if (shown.length === 0) {
    body = <NoMatchesState title="No integrations match" clearLabel="Clear" onClear={clearAll} testId="integrations-clear-search" />
  } else {
    body = (
      <CardGrid label="Integrations" testId="integrations-grid" rows>
        {shown.map(item => <IntegrationCard key={item.custom_connector_id ?? item.slug} item={item} returnTo={returnTo} />)}
      </CardGrid>
    )
  }

  return (
    <DetailPage
      testId="integrations-page"
      breadcrumb={<Breadcrumb items={[{ label: 'Secrets', to: '/credentials' }, { label: 'New secret' }]} />}
      actions={(
        <Button
          component={RouterLink}
          to={returnTo ? `${OTHER_SECRET_PATH}&return=${encodeURIComponent(returnTo)}` : OTHER_SECRET_PATH}
          variant="outlined"
          color="inherit"
          data-testid="integrations-other"
          sx={tapTargetSx}
        >
          Other secret
        </Button>
      )}
      sx={{ maxWidth: 'none', minWidth: 0 }}
    >
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
        <SearchBox
          value={q}
          onChange={next => setParam({ q: next })}
          delayMs={150}
          ariaLabel="Search integrations"
          placeholder="Search integrations"
          title="Name, category and summary"
          maxLength={200}
          testId="integrations-search"
          sx={{ maxWidth: { sm: 420 } }}
        />
        {categories.length > 1 && (
          /* One scrolling row on a phone (fourteen chips wrap to six rows there); wrapped wider. */
          <Box
            role="group"
            aria-label="Category"
            sx={{
              display: 'flex', flexWrap: { xs: 'nowrap', sm: 'wrap' }, gap: 1, alignItems: 'center',
              overflowX: { xs: 'auto', sm: 'visible' }, pb: { xs: 0.5, sm: 0 }, '& > *': { flexShrink: 0 },
            }}
          >
            {categories.map(([key, label]) => (
              <Chip
                key={key}
                label={label}
                clickable
                color={category === key ? 'primary' : 'default'}
                variant={category === key ? 'filled' : 'outlined'}
                aria-pressed={category === key}
                onClick={() => setParam({ category: category === key ? '' : key })}
                data-testid={`integrations-chip-${key}`}
                sx={tapTargetSx}
              />
            ))}
            {(category !== null || q.trim() !== '') && (
              <Button size="small" onClick={clearAll} data-testid="integrations-clear" sx={tapTargetSx}>Clear</Button>
            )}
          </Box>
        )}
      </Box>
      {body}
      </Box>
    </DetailPage>
  )
}

function IntegrationCard({ item, returnTo }: { item: Integration; returnTo: string | null }): JSX.Element {
  const docs = safeHref(item.docs_url)
  const state = item.connected ? CONNECTION_STATE[item.connected.worst_state] : null
  const testId = `integration-card-${item.slug}`
  const target = cardTarget(item, returnTo)
  const beta = isBeta(item) && !item.custom
  const connect = (
    <Button
      component={RouterLink}
      to={target.to}
      variant={beta ? 'text' : item.connected ? 'outlined' : 'contained'}
      color={beta ? 'inherit' : 'primary'}
      size="small"
      data-testid={`${testId}-connect`}
      aria-label={target.aria}
    >
      {target.label}
    </Button>
  )
  return (
    <GalleryCard
      testId={testId}
      name={item.label}
      summary={item.summary}
      image={null}
      mediaRatio={CARD_MEDIA_RATIO}
      placeholder={(
        <Box sx={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'text.primary' }}>
          <VendorLogo url={item.logo_url} label={item.label} size={GALLERY_THUMB_PX} tile />
        </Box>
      )}
      meta={(
        <Box component="span" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5 }}>
          {/* First, so a narrow meta cell clips the category, never the flag. */}
          {beta && (
            <StatusChip label="Beta" tone="info" testId={`${testId}-beta`} />
          )}
          <span>{item.category_label}</span>
          {authChips(item).map(chip => (
            <StatusChip key={chip} label={chip} tone="neutral" testId={`${testId}-auth-${chip.toLowerCase()}`} />
          ))}
        </Box>
      )}
      actions={(
        <>
          {beta ? <VoteButton item={item} testId={testId} /> : connect}
          {beta && connectableWithoutApp(item) && connect}
          {docs && (
            <Button
              component="a"
              href={docs}
              target="_blank"
              rel="noopener noreferrer"
              size="small"
              color="inherit"
              data-testid={`${testId}-docs`}
              aria-label={`${item.label} docs`}
            >
              Docs
            </Button>
          )}
          {state && (
            <StatusChip
              label={item.connected && item.connected.count > 1 ? `${state.label} · ${item.connected.count}` : state.label}
              tone={state.tone}
              testId={`${testId}-state`}
            />
          )}
        </>
      )}
    />
  )
}

/** A Beta card's Vote: one vote per person, thanked with a short celebration; pressed again it withdraws the vote. */
function VoteButton({ item, testId }: { item: Integration; testId: string }): JSX.Element {
  // The cached gallery row is the one truth: a card that re-mounts (a filter dropped it and brought it back) reads it.
  const queryClient = useQueryClient()
  const voted = item.voted === true
  const [thanks, setThanks] = useState(false)
  const { busy, formError, run } = useSubmit()
  const toggle = () => void run('Vote not saved', async () => {
    const next = !voted
    await voteIntegration(item.slug, next)
    queryClient.setQueryData<Integration[]>(INTEGRATIONS_KEY, rows => rows?.map(row => (
      row.slug === item.slug && !row.custom ? { ...row, voted: next } : row
    )))
    if (next) setThanks(true)
  })
  return (
    <>
      <Tip title={voted ? 'Withdraw vote' : undefined}><Button
        onClick={toggle}
        disabled={busy}
        variant={voted ? 'outlined' : 'contained'}
        color="secondary"
        size="small"
        aria-pressed={voted}
        aria-label={voted ? `Withdraw your vote for ${item.label}` : `Vote for ${item.label}`}
        data-testid={`${testId}-vote`}
      >
        {voted ? 'Voted' : 'Vote'}
      </Button></Tip>
      {formError && (
        <Typography variant="caption" role="alert" sx={{ color: 'error.main' }} data-testid={`${testId}-vote-error`}>
          {formError}
        </Typography>
      )}
      {thanks && (
        <SetupCelebration
          onDone={() => setThanks(false)}
          title="Thanks for voting!"
          message={`Your vote for ${item.label} is counted.`}
          testId="integration-vote-thanks"
        />
      )}
    </>
  )
}
