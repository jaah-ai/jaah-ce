import { useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { alpha } from '@mui/material/styles'
import type { Theme } from '@mui/material/styles'
import { get } from '../../api'
import type { PipelineTask, TaskBodyVersion } from '../../types'
import { useApiRead } from '../../hooks/useApiRead'
import { absoluteTime } from '../../lib/relativeTime'
import { takeTaskPageSeed } from '../../lib/taskPageSeed'
import { useLandedTaskPart } from '../../hooks/useLandedTaskPart'
import { Banner } from '../../ui/Banner'
import FeedList from '../../ui/feed/FeedList'
import TextDiffDialog from '../../ui/TextDiffDialog'
import type { FeedRowSpec } from '../../ui/feed/types'
import { CodeBlock } from '../../ui/CodeBlock'

// The Versions subtab: a READ-ONLY audit surface over a Task's `body` history. No restore route exists,
// hence no `readOnly` prop. This is the console's ONLY reader of `GET /api/tasks/{id}/body-versions`,
// so every field of a row is shown rather than summarised.

// How each snapshot was written. Live writers: `manual` (`update_task`), `agent_filed_edit`
// (`edit_agent_filed_draft`), `runner_break_refresh` (apps/api/app/runner_breaks.py),
// `system_dedup_refresh` (apps/api/app/routes/agent/task_reads.py). `restore` and `chat` are legacy tokens with
// rows still in the table.
const SOURCE_LABELS: Record<string, string> = {
  manual: 'manual edit',
  restore: 'restored',
  chat: 'chat edit',
  runner_break_refresh: 'runner break refresh',
  system_dedup_refresh: 'system dedup refresh',
}

// Unknown tokens humanise (`snake_case` → words) so a new writer need not register here.
function formatSource(source: string): string {
  const known = SOURCE_LABELS[source]
  if (known) return known
  const spaced = source.replace(/[_-]+/g, ' ').trim()
  return spaced || 'unknown'
}

// A snapshot can be arbitrarily long, so it gets its own bounded scrollport. `ml` is the feed's rail
// inset: the block sits under its row's title.
function detailPreSx(theme: Theme) {
  return {
    ml: '30px',
    mb: 0.5,
    padding: '1rem 1.25rem 1rem 1.625rem',
    borderRadius: 0,
    color: 'text.secondary',
    borderTop: '1px solid',
    borderTopColor: 'divider',
    borderLeft: '3px solid',
    borderLeftColor: alpha(theme.palette.primary.main, 0.3),
    lineHeight: 1.65,
    // Nested in a narrow row-detail the padding stacks with the panel gutters; trimmed at <=480px.
    [theme.breakpoints.down(481)]: { ml: '22px', padding: '0.625rem 0.75rem' },
  }
}

/* Recency mark: filled for newest, hollow otherwise — shape, not colour. */
const NEWEST_GLYPH = '●'
const EARLIER_GLYPH = '○'

export default function TaskVersions({ task }: { task: PipelineTask }) {
  const { data: versions, error, loading } = useApiRead(
    // Keyed on `updated_at` too: saving the description writes a snapshot server-side, and the host's
    // fresher task is the only signal that it exists.
    ['task-versions', task.id, task.updated_at],
    async () => {
      const data = takeTaskPageSeed<TaskBodyVersion[]>(task.id, 'body_versions')
        ?? await get<TaskBodyVersion[]>(`/api/tasks/${task.id}/body-versions`)
      return Array.isArray(data) ? data : []
    },
    'Failed to load version history',
  )
  useLandedTaskPart(task.id, 'body_versions', loading || error || !versions ? undefined : versions)
  /* Explicit open/shut choices by id; default is newest open, the rest shut.
   * Every snapshot is in the DOM at first paint, shut or not (this is the only body-history reader, so
   * find-in-page must reach it): `detailKeepMounted` keeps it mounted and findable, where `hidden` would
   * be `display: none` (see `ui/feed/FeedList.tsx`). */
  const [open, setOpen] = useState<Record<number, boolean>>({})
  /* The snapshot whose Compare dialog is open, by id: a refetch prepends rows, so an index would drift. */
  const [comparingId, setComparingId] = useState<number | null>(null)

  const comparing = versions && comparingId !== null ? versions.findIndex(v => v.id === comparingId) : -1

  if (error) return <Banner severity="error">{error}</Banner>
  if (!versions) return null

  const rows: FeedRowSpec[] = versions.map((v, i): FeedRowSpec => {
    const newest = i === 0
    const source = formatSource(v.source)
    const when = absoluteTime(v.created_at)
    const expanded = open[v.id] ?? newest
    return {
      key: String(v.id),
      /* Not a `testid`: the row's testid is the feed's `feed-row`, which every row oracle finds
       * (`ui/feed/types.ts`). */
      data: { version: v.id },
      indicator: {
        tone: newest ? 'info' : 'neutral',
        glyph: newest ? NEWEST_GLYPH : EARLIER_GLYPH,
        label: newest ? 'most recent snapshot' : 'earlier snapshot',
      },
      disclosure: {
        expanded,
        label: 'the snapshot',
        onToggle: () => setOpen(prev => ({ ...prev, [v.id]: !expanded })),
      },
      /* The instant is the row's identity. `dateTime`/`title` carry the raw ISO: an audit surface must answer
       * "exactly when", which a localised string rounds away. */
      title: (
        <Box component="time" dateTime={v.created_at} title={v.created_at}>
          {when}
        </Box>
      ),
      titleText: when,
      /* The disc is decorative once this is set (`ui/feed/types.ts`). */
      ariaLabel: `${newest ? 'Most recent' : 'Earlier'} snapshot, ${when}, ${source}`,
      slots: [
        {
          key: 'compare',
          width: 84,
          align: 'right',
          node: (
            <Button
              size="small"
              data-testid={`task-version-compare-${v.id}`}
              aria-label={`Compare the ${when} edit`}
              onClick={() => setComparingId(v.id)}
              sx={{ minWidth: 0 }}
            >
              Compare
            </Button>
          ),
        },
        {
          key: 'source',
          width: 132,
          /* Ungated: the only fact saying HOW the snapshot came to exist. */
          align: 'right',
          dim: true,
          testId: `task-version-source-${v.id}`,
          node: source,
        },
      ],
      detailKeepMounted: true,
      detail: (
        <CodeBlock maxHeight={150} sx={detailPreSx}>{v.body}</CodeBlock>
      ),
    }
  })

  return (
    <Box data-testid="task-versions" sx={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
      {/* The count, not a title (the tab already says Versions); suppressed at zero. */}
      {versions.length > 0 && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          {versions.length} previous {versions.length === 1 ? 'version' : 'versions'}
        </Typography>
      )}

      {/* "No versions" is a real answer, so the list says so rather than rendering an empty box. */}
      <FeedList
        ariaLabel="Description versions"
        testId="task-versions-list"
        emptyMessage="No previous versions."
        sections={rows.length > 0 ? [{ key: 'versions', rows }] : []}
      />

      {/* A snapshot is the description an edit REPLACED (`_snapshot_body_version`), so it compares with the
          next newer snapshot — or, for the newest, with the current description. */}
      {comparing >= 0 && (
        <TextDiffDialog
          title={`Description changes · ${absoluteTime(versions[comparing].created_at)}`}
          before={versions[comparing].body}
          after={comparing === 0 ? task.body ?? '' : versions[comparing - 1].body}
          onClose={() => setComparingId(null)}
        />
      )}
    </Box>
  )
}
