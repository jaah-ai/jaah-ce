import { useState, type JSX } from 'react'
import { Link as RouterLink, useSearchParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import Typography from '@mui/material/Typography'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { apiDetail } from '../../api'
import CardGrid from '../../ui/gallery/CardGrid'
import LoadFailedState from '../../ui/LoadFailedState'
import LoadingState from '../../ui/LoadingState'
import NewButton from '../../ui/NewButton'
import NoMatchesState from '../../ui/NoMatchesState'
import PageHeader from '../../ui/PageHeader'
import SearchBox from '../../ui/SearchBox'
import { tapTargetSx } from '../../ui/listStyles'
import { PURPOSES, TEMPLATES_KEY, listTemplates, type TemplateCard } from './api'
import { useTemplateEditing } from './access'
import { gallerySections } from './sections'
import StartTemplateDialog from './StartTemplateDialog'
import TemplateCardView from './TemplateCardView'

/* THE TEMPLATE GALLERY — `/templates`, the style guide's Gallery archetype. Every org member
 * browses the published templates the server shows them (`GET /api/templates`: global, plus the org's own while its
 * Templates (org) switch is on). Unsearched it draws sections (`sections.ts::gallerySections`): Favorites, Most popular,
 * then one per purpose group, each with its count; a group chip narrows to that group. A search (`?q=`, committed
 * 250 ms after typing pauses) asks the server, which ranks the matches: one flat grid, best match first, no sections.
 * The term and the chip live in the URL, so a task's "From template" link lands on a search. Start opens the one-shot
 * Start dialog; Customize opens the seeded create form. A template editor (`access.tsx::useTemplateEditing`: the
 * operator, or `templates:edit` while the org's switch is on) gets Manage and New template; an org editor also gets
 * Duplicate on global cards. The template's own pages are the manage view's. The sidebar names the page, so the
 * header carries actions only. */
export const SEARCH_DELAY_MS = 250

export default function TemplatesPage(): JSX.Element {
  const [params, setParams] = useSearchParams()
  const q = params.get('q') ?? ''
  const purpose = PURPOSES.some(p => p.value === params.get('purpose')) ? params.get('purpose') : null
  const editing = useTemplateEditing()
  const [starting, setStarting] = useState<TemplateCard | null>(null)
  const searching = q.trim() !== ''

  const all = useQuery({ queryKey: TEMPLATES_KEY, queryFn: () => listTemplates(), enabled: !searching })
  const found = useQuery({
    queryKey: [...TEMPLATES_KEY, 'search', q, purpose],
    queryFn: () => listTemplates(q, purpose),
    enabled: searching,
    placeholderData: keepPreviousData,
  })
  const shown = searching ? found : all

  const setParam = (key: string, value: string | null) => setParams(prev => {
    const next = new URLSearchParams(prev)
    if (value === null || value === '') next.delete(key)
    else next.set(key, value)
    return next
  }, { replace: true })
  const clearAll = () => setParams(new URLSearchParams(), { replace: true })

  const card = (c: TemplateCard) => (
    <TemplateCardView key={c.id} card={c} onStart={setStarting} duplicable={editing.org} />
  )

  let body: JSX.Element
  if (shown.isPending) body = <LoadingState label="Loading templates…" />
  else if (shown.isError) {
    body = <LoadFailedState title="Templates unavailable" error={apiDetail(shown.error, '')} onRetry={() => void shown.refetch()} />
  } else if (searching) {
    body = found.data && found.data.length > 0
      ? <CardGrid label="Search results" testId="templates-results">{found.data.map(card)}</CardGrid>
      : <NoMatchesState title="No templates match" clearLabel="Clear" onClear={clearAll} testId="templates-clear-search" />
  } else {
    const sections = gallerySections(all.data ?? [], purpose)
    body = sections.length === 0
      ? <NoMatchesState title="No templates match" clearLabel="Clear" onClear={purpose ? clearAll : undefined} />
      : (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          {sections.map(section => (
            <Box component="section" key={section.key} aria-labelledby={`templates-${section.key}`} data-testid={`templates-section-${section.key}`}>
              <Typography id={`templates-${section.key}`} component="h2" variant="subtitle1" sx={{ fontWeight: 600, mb: 1 }}>
                {section.title}
                <Box component="span" sx={{ color: 'text.secondary', fontWeight: 400, ml: 1 }} data-testid={`templates-section-${section.key}-count`}>
                  {section.cards.length}
                </Box>
              </Typography>
              <CardGrid label={section.title}>{section.cards.map(card)}</CardGrid>
            </Box>
          ))}
        </Box>
      )
  }

  return (
    <Box data-testid="templates-page" sx={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
      <PageHeader
        actions={editing.any ? (
          <>
            <Button component={RouterLink} to="/templates/manage" variant="outlined" color="inherit" data-testid="templates-manage" sx={tapTargetSx}>
              Manage
            </Button>
            <NewButton label="New template" to="/templates/new" testId="templates-new" />
          </>
        ) : undefined}
      />
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
        <SearchBox
          value={q}
          onChange={next => setParam('q', next)}
          delayMs={SEARCH_DELAY_MS}
          ariaLabel="Search templates"
          placeholder="Search templates"
          title="Name, summary, description and group"
          maxLength={200}
          testId="templates-search"
          sx={{ maxWidth: { sm: 420 } }}
        />
        <Box role="group" aria-label="Purpose" sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center' }}>
          {PURPOSES.map(p => (
            <Chip
              key={p.value}
              label={p.label}
              clickable
              color={purpose === p.value ? 'primary' : 'default'}
              variant={purpose === p.value ? 'filled' : 'outlined'}
              aria-pressed={purpose === p.value}
              onClick={() => setParam('purpose', purpose === p.value ? null : p.value)}
              data-testid={`templates-chip-${p.value}`}
              sx={tapTargetSx}
            />
          ))}
          {(purpose !== null || searching) && (
            <Button size="small" onClick={clearAll} data-testid="templates-clear" sx={tapTargetSx}>Clear</Button>
          )}
        </Box>
      </Box>
      {body}
      {starting && <StartTemplateDialog card={starting} onClose={() => setStarting(null)} />}
    </Box>
  )
}
