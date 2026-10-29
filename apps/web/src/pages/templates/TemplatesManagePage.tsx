import type { JSX } from 'react'
import { useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import { apiDetail } from '../../api'
import { useSettled } from '../../hooks/useSettled'
import { deleteItem, editItem } from '../../components/tasks/rowActions'
import Banner from '../../ui/Banner'
import FilterBar from '../../ui/FilterBar'
import { FilterRow, FilterSearch } from '../../ui/FilterRow'
import NewButton from '../../ui/NewButton'
import NoMatchesState from '../../ui/NoMatchesState'
import PageHeader from '../../ui/PageHeader'
import { TEMPLATES_LIST_CONTAINER_SX } from '../../ui/listStyles'
import ListStates from '../../ui/feed/ListStates'
import type { FeedColumn, FeedRowSpec } from '../../ui/feed/types'
import { MANAGE_KEY, type TemplateCard, listManagedTemplates, purposeLabel } from './api'
import { useTemplateDelete, useTemplateEditing } from './access'
import { TEMPLATE_PATHS } from '../../routeGroups'

/* THE TEMPLATE MANAGE LIST — `/templates/manage`: every template the viewer edits, drafts included
 * (`GET /api/templates/manage`) — the operator's global library and, for an org template editor, the org's own.
 * One `ui/feed/FeedList` with a column row: name, purpose group, origin, status and starts in the last 90 days. A
 * row opens its editor (`/templates/:id`) beside the list in the split pane (`lib/listDetail.ts`), never a modal.
 * The search is the server's (`?q=`, ranked full text), debounced. ＋ New opens `/templates/new`. */

const SEARCH_DEBOUNCE_MS = 250

const COLUMNS: { title: FeedColumn; slots: FeedColumn[] } = {
  title: { key: 'name', label: 'Name' },
  slots: [
    { key: 'purpose', label: 'Purpose' },
    { key: 'origin', label: 'Origin' },
    { key: 'status', label: 'Status' },
    { key: 'starts', label: 'Starts', ariaLabel: 'Starts in the last 90 days' },
  ],
}

const ORIGIN_LABEL = { global: 'Global', org: 'Org' } as const

export default function TemplatesManagePage(): JSX.Element {
  const [q, setQ] = useState('')
  const query = useSettled(q, SEARCH_DEBOUNCE_MS)
  const editing = useTemplateEditing()
  const [askDelete, deleteDialog] = useTemplateDelete()
  const [busyId, setBusyId] = useState<string | null>(null)
  const list = useQuery({
    queryKey: [...MANAGE_KEY, query.trim()],
    queryFn: () => listManagedTemplates(query),
    placeholderData: keepPreviousData,
  })
  const rows = list.data ?? []

  async function onDelete(t: TemplateCard) {
    setBusyId(t.id)
    try {
      await askDelete(t)
    } finally {
      setBusyId(null)
    }
  }

  const rowOf = (t: TemplateCard): FeedRowSpec => ({
    key: t.id,
    indicator: t.published
      ? { tone: 'success', glyph: '●', label: 'Published', data: { status: 'published' } }
      : { tone: 'neutral', glyph: '○', label: 'Draft', data: { status: 'draft' } },
    title: t.name,
    titleText: t.name,
    // A name is at most 80 characters: two lines hold it whole at every width.
    titleLines: 2,
    href: `/templates/${t.id}`,
    busy: busyId === t.id,
    markers: t.featured ? [{ key: 'featured', glyph: '★', label: 'Featured', hug: true }] : undefined,
    slots: [
      { key: 'purpose', width: 128, gate: 'templatesMeta', align: 'left', dim: true, testId: 'template-purpose', node: purposeLabel(t.purpose) },
      { key: 'origin', width: 48, gate: 'templatesMeta', align: 'left', dim: true, testId: 'template-origin', node: ORIGIN_LABEL[t.origin] },
      { key: 'status', width: 64, align: 'left', testId: 'template-status', node: t.published ? 'Published' : 'Draft' },
      {
        key: 'starts', width: 48, align: 'right', testId: 'template-starts', title: `${t.starts_90d} starts in the last 90 days`,
        node: String(t.starts_90d),
      },
    ],
    notes: t.summary ? [{ key: 'summary', text: t.summary, tone: 'muted' }] : undefined,
    actions: t.editable
      ? [editItem(t.name, { to: `/templates/${t.id}` }), deleteItem(t.name, () => void onDelete(t))]
      : undefined,
    data: { origin: t.origin },
  })

  const filtering = query.trim() !== ''
  return (
    // The query container `TEMPLATES_NO_META` reads: the list's own width, not the window's.
    <Box data-testid="templates-manage-page" sx={TEMPLATES_LIST_CONTAINER_SX}>
      <PageHeader
        title="Manage templates"
        actions={editing.any && <NewButton label="New template" to={TEMPLATE_PATHS.create} testId="template-new" />}
      />
      <Box data-testid="template-filters">
        <FilterBar>
          <FilterRow>
            <FilterSearch value={q} onChange={setQ} placeholder="Name, summary or purpose" ariaLabel="Search templates" />
          </FilterRow>
        </FilterBar>
      </Box>
      <Box sx={{ display: 'grid', gap: 1.5, minWidth: 0 }}>
        <ListStates
          loading={list.isPending}
          skeleton={{ rows: 6 }}
          failed={list.isError && <Banner severity="error">{apiDetail(list.error, 'Could not load templates.')}</Banner>}
          empty={rows.length === 0}
          noMatch={filtering && <NoMatchesState onClear={() => setQ('')} />}
          emptyTitle="No templates"
          emptyScoped
          feed={{
            ariaLabel: 'Templates',
            testId: 'template-list',
            sections: [{ key: 'templates', rows: rows.map(rowOf) }],
            columns: COLUMNS,
          }}
        />
      </Box>
      {deleteDialog}
    </Box>
  )
}
