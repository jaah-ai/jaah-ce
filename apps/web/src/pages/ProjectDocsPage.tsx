import { useCallback, useEffect, useMemo } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import List from '@mui/material/List'
import ListItemButton from '@mui/material/ListItemButton'
import ListSubheader from '@mui/material/ListSubheader'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { get } from '../api'
import Breadcrumb from '../components/Breadcrumb'
import RichTextRenderer from '../components/richtext/RichTextRenderer'
import { useApiRead } from '../hooks/useApiRead'
import { useProjects } from '../hooks/useRosters'
import Banner from '../ui/Banner'
import EmptyState from '../ui/EmptyState'
import { ELLIPSIS_SX, tapTargetSx } from '../ui/listStyles'
import LoadingState from '../ui/LoadingState'
import { OrgAside } from '../ui/OrgIndicator'
import PageHeader from '../ui/PageHeader'
import SectionCard from '../ui/SectionCard'
import { docLabel, groupByDir, linkTarget, stripHtmlComments } from '../lib/docsTree'

/**
 * A project's markdown docs, read live from its repo. Not a build: a repo opts in by committing an
 * `mkdocs.yml`, of which the API reads only `site_name` and `docs_dir`; MkDocs never runs.
 * Rendered through `RichTextRenderer` (read-only TipTap schema) so repo content cannot inject markup.
 */

interface DocsIndex {
  enabled: boolean
  reason_code: 'no_mkdocs' | 'not_visible' | null
  project_name: string
  site_name: string | null
  paths: string[]
  ref: string | null
  blob_base: string | null
  reason: string | null
}

export default function ProjectDocsPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const selected = params.get('path') ?? ''

  const { data: index, loading, error: indexError } = useApiRead(
    ['project-docs-index', id],
    () => get<DocsIndex>(`/api/projects/${id}/docs`),
    'Could not load this project’s docs.',
  )

  // Open the first document so the page does not look broken on an empty pane.
  useEffect(() => {
    if (!selected && index?.enabled && index.paths.length > 0) {
      setParams({ path: index.paths[0] }, { replace: true })
    }
  }, [index, selected, setParams])

  const { data: doc, loading: loadingDoc, error: docError } = useApiRead(
    ['project-docs-file', id, selected],
    () => get<{ content: string }>(
      `/api/projects/${id}/docs/file?path=${encodeURIComponent(selected.split('#')[0])}`,
    ),
    'Could not load that document.',
    { enabled: Boolean(selected) },
  )
  /* Memoised: `stripHtmlComments` walks every line and the page re-renders on each query settle. */
  const content = useMemo(() => (doc ? stripHtmlComments(doc.content) : ''), [doc])

  /* One banner, index-first: a failed index leaves nothing to select, so the doc error would be the
   * symptom. `indexError || docError` does not cross-clear a stale doc error when `:id` changes with no
   * `?path` — unreachable (nothing links to this route), so not guarded. */
  const error = indexError || docError

  /* The org comes from the `['projects']` roster (the docs endpoint returns no org), usually served from
   * cache. No `enabled` gate and no error surface: an unanswered roster leaves the indicator blank, and a
   * failed org lookup must not take a readable doc off screen. */
  const projectsQuery = useProjects()
  const project = projectsQuery.data?.find(p => String(p.id) === id)

  const pathSet = useMemo(() => new Set(index?.paths ?? []), [index])
  const groups = useMemo(() => groupByDir(index?.paths ?? []), [index])
  const blobBase = index?.blob_base ?? ''

  // In-tree markdown links navigate the SPA; the rule lives (and is tested) in `lib/docsTree`.
  const onDocClick = useCallback(
    (event: React.MouseEvent<HTMLDivElement>) => {
      const anchor = (event.target as HTMLElement).closest('a')
      const href = anchor?.getAttribute('href')
      if (!href || !selected) return
      const dest = linkTarget(selected.split('#')[0], href, pathSet, blobBase)
      if (dest.kind === 'doc') {
        event.preventDefault()
        setParams({ path: dest.target })
      } else if (anchor) {
        anchor.setAttribute('href', dest.target)
        anchor.setAttribute('target', '_blank')
        anchor.setAttribute('rel', 'noopener noreferrer')
      }
    },
    [selected, pathSet, blobBase, setParams],
  )

  if (loading) return <LoadingState label="Loading docs…" />

  return (
    <Stack spacing={2}>
      <Breadcrumb
        items={[
          { label: 'Projects', to: '/projects' },
          { label: index?.project_name ?? 'Project', to: `/projects/${id}` },
          { label: 'Docs' },
        ]}
      />
      <PageHeader
        title={index?.site_name || `${index?.project_name ?? 'Project'} docs`}
        /* The owning tenant beside the title (`titleAside`, not `actions` — see `ui/PageHeader`): `site_name`
         * comes from the repo, and two tenants may name their docs alike. Labelled "Org" because the heading is
         * a site name, not the project's. `ui/OrgIndicator.tsx`'s `OrgAside` owns the markup. */
        titleAside={<OrgAside label="Org" org={project?.org ?? null} chipTestId="project-docs-org" />}
        subtitle={index?.ref ? `Read live from ${index.ref}` : undefined}
      />
      {error ? <Banner severity="error">{error}</Banner> : null}

      {index && !index.enabled ? (
        <EmptyState
          /* Titled from `reason_code`: no mkdocs.yml (not opted in) and a repo unreadable with THIS operator's
           * token are opposite advice. */
          title={
            index.reason_code === 'not_visible'
              ? 'These docs are not visible to your GitHub account'
              : 'This repo has not opted into docs browsing'
          }
          action={<Button onClick={() => navigate(`/projects/${id}`)}>Back to project</Button>}
        />
      ) : null}

      {index?.enabled && index.paths.length === 0 ? (
        <EmptyState title="No documents yet" />
      ) : null}

      {index?.enabled && index.paths.length > 0 ? (
        <Box
          sx={{
            display: 'flex',
            flexDirection: { xs: 'column', md: 'row' },
            alignItems: 'flex-start',
            gap: 2,
          }}
        >
          <Box sx={{ width: { xs: '100%', md: 300 }, flexShrink: 0 }}>
            <SectionCard dense>
              <List dense sx={{ maxHeight: { xs: '38vh', md: '70vh' }, overflowY: 'auto' }}>
                {groups.map((group) => (
                  <li key={group.dir || '(root)'}>
                    <ul style={{ padding: 0 }}>
                      <ListSubheader sx={{ ...ELLIPSIS_SX, lineHeight: 2 }} title={group.dir}>
                        {group.dir || '/'}
                      </ListSubheader>
                      {group.paths.map((path) => (
                        <ListItemButton
                          key={path}
                          selected={selected.split('#')[0] === path}
                          onClick={() => setParams({ path })}
                          sx={{ ...tapTargetSx, ...ELLIPSIS_SX }}
                          title={path}
                        >
                          <Typography variant="body2" sx={ELLIPSIS_SX}>
                            {docLabel(path)}
                          </Typography>
                        </ListItemButton>
                      ))}
                    </ul>
                  </li>
                ))}
              </List>
            </SectionCard>
          </Box>

          <Box sx={{ flexGrow: 1, minWidth: 0, width: '100%' }}>
            <SectionCard>
              {loadingDoc ? (
                <LoadingState label="Loading document…" />
              ) : (
                <Box onClick={onDocClick} sx={{ minWidth: 0, overflowX: 'auto' }}>
                  <RichTextRenderer content={content} linkify={false} />
                </Box>
              )}
            </SectionCard>
          </Box>
        </Box>
      ) : null}
    </Stack>
  )
}
