import { type JSX, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link as RouterLink, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import NewButton from '../../ui/NewButton'
import { alpha } from '@mui/material/styles'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { apiDetail, del, get } from '../../api'
import { useSubmit } from '../../hooks/useSubmit'
import { isOpenPath } from '../../lib/listDetail'
import { useConfirm } from '../ConfirmDialog'
import { useOpenDetailPath } from '../ListDetail'
import { deleteItem, RowActionsOverflow } from '../tasks/rowActions'
import Banner from '../../ui/Banner'
import { BreakAfter } from '../../ui/BreakAfter'
import { CopyButton } from '../../ui/CopyButton'
import { FONT_WEIGHT, SIZE } from '../../theme'
import { DataTable } from '../../ui/DataTable'
import EmptyState from '../../ui/EmptyState'
import HelpLabel from '../../ui/HelpLabel'
import { monoSx, ROW_ACTION_SX } from '../../ui/listStyles'
import LoadingState from '../../ui/LoadingState'
import { nextSort, rankRows, SortableHead, type SortState } from '../../ui/SortableHead'
import StatusChip, { type Tone } from '../../ui/StatusChip'
import { TONE_COLOR } from '../../ui/statusTone'
import Tip from '../../ui/Tip'
import { VISUALLY_HIDDEN } from '../../ui/visuallyHidden'
import {
  envFileLines,
  envListHref,
  envPreviewKey,
  envVarHref,
  envVarsKey,
  DELETE_FALLBACK,
  deleteEnvVarConfirm,
  mayWriteEnvVar,
  portValueLabel,
  scopeLabel,
  sourceLabel,
  STATUS_OK,
  unresolvedReason,
  type ProjectEnvVar,
} from './projectEnvVars'
import { CodeBlock } from '../../ui/CodeBlock'

/* The project edit page's ENVIRONMENT tab over `project_env_vars` — the catalogue Jaah writes into a
 * slot's `.env` when claimed. A row is a LITERAL (non-secret, cleartext) or a reference to a stored
 * CREDENTIAL's field (whose value never reaches this screen). Any `projects:view` holder may set their OWN
 * override, affecting only their slots.
 *
 * It renders the server's verdicts (`status`, `status_detail`, `winning`, `warning`) and re-derives none:
 * `app/project_env.py` holds the ONE copy of R4's precedence; `./projectEnvVars.ts` words statuses.
 *
 * The catalogue is a `ui/DataTable`, one row per variable: Name (sortable), Value / Source, Scope, and the row's ⋯.
 * The mark before the name says whether the row is the value the viewer's slot gets; why it does not resolve, what
 * writes the name instead, whose override it is and its description are lines under the name. The name wraps
 * (never under 12 characters wide) rather than clips; on a narrow screen the table scrolls inside its frame.
 *
 * Secret floor: nothing on the wire can carry a resolved value (the serializer sends credential NAME,
 * TYPE LABEL and FIELD only; the preview never resolves), so there is nothing to redact.
 *
 * A row's NAME links to the variable's own page (`pages/projects/ProjectEnvVarPage.tsx`, beside this list), for
 * every row: one the viewer may not write opens read-only there. Add opens the same page in create mode. The ⋯ keeps
 * only Delete, the one verb used from the list.
 *
 * Renders inside the project form's `<form>`: every button is `type="button"` (else Delete would save the
 * project), and the confirm is a portalled `FormDialog`, which stops its own submit before it bubbles.
 *
 * The preview is the `.env` text this tab contributes (`envFileLines`): literals in clear, credentials
 * masked, an unresolved winner commented out in error tone. It sits behind a toggle, under the table.
 */

const NOT_APPLIED = 'Not the value your slot gets.'

type NoteTone = 'muted' | 'error' | 'warning'
type Note = { key: string; tone: NoteTone; text: string }
type Mark = { tone: Tone; glyph: string; label: string; applies: boolean }

type SortKey = 'name'

/* The name column never narrower than a name column's floor (~19 mono characters beside the mark, over the 12 a
   phone must show), so a phone shows a readable stem, never a letter; the table scrolls inside its frame instead. */
const NAME_CELL_SX = { minWidth: SIZE.fieldMd } as const
const NAME_SX = [monoSx, { overflowWrap: 'anywhere', wordBreak: 'normal' }] as const
/* A long literal wraps inside a capped column rather than widening the table past the frame. */
const VALUE_CELL_SX = { minWidth: SIZE.fieldMd, width: '38%' } as const
const VALUE_SX = { maxWidth: '36ch', overflowWrap: 'anywhere' } as const
const NOTE_COLOR: Record<NoteTone, string> = { muted: 'text.secondary', error: 'error.main', warning: 'warning.main' }

/** What a row says beyond its cells, in order: why it does not resolve, that it is not applied, what writes the
 *  name instead (the server's sentence), whose override it is, its description. */
function envVarNotes(row: ProjectEnvVar, reason: string | null, viewerOwnsRow: boolean, ownerLabel: string | null): Note[] {
  const notes: Note[] = []
  if (reason !== null) notes.push({ key: 'reason', tone: 'error', text: reason })
  if (row.scope === 'project' && !row.winning && reason !== null) notes.push({ key: 'shadowed', tone: 'muted', text: NOT_APPLIED })
  if (row.warning !== null) notes.push({ key: 'warning', tone: 'warning', text: row.warning })
  if (row.scope === 'user' && !viewerOwnsRow && ownerLabel !== null) notes.push({ key: 'owner', tone: 'muted', text: `Set by ${ownerLabel}` })
  if (row.description !== '') notes.push({ key: 'description', tone: 'muted', text: row.description })
  return notes
}

/** The ✓ only on a row that wins AND resolves (a winner supplying nothing is the row whose warning matters most).
 *  Reports `winning` and names no cause: a precedence inference is the server's. */
function envVarMark(row: ProjectEnvVar, reason: string | null, viewerId: string | null): Mark {
  if (row.winning && row.status === STATUS_OK) return { tone: 'success', glyph: '✓', label: 'The value your slot gets.', applies: true }
  if (reason !== null) return { tone: 'error', glyph: '!', label: 'Not resolved', applies: false }
  const shadowed = row.scope === 'project' && !row.winning
  return { tone: 'neutral', glyph: '–', label: shadowed ? NOT_APPLIED : scopeLabel(row, viewerId), applies: false }
}

/** One catalogue row. A CREDENTIAL reference names credential and field in words, never a value. */
function EnvVarRow({
  row, projectId, viewerId, ownerLabel, onDelete, busy,
}: {
  row: ProjectEnvVar
  projectId: number
  viewerId: string | null
  ownerLabel: string | null
  onDelete: (() => void) | undefined
  busy: boolean
}): JSX.Element {
  const viewerOwnsRow = viewerId !== null && row.user_id === viewerId
  const reason = unresolvedReason(row, viewerOwnsRow)
  const mark = envVarMark(row, reason, viewerId)
  const notes = envVarNotes(row, reason, viewerOwnsRow, ownerLabel)
  const fromCredential = row.source === 'credential'
  /* A tombstone: the delete nulls the reference; the field survives so a replacement is clear. */
  const credentialName = row.credential?.name ?? '(deleted)'
  return (
    <TableRow data-testid="project-env-row" data-name={row.name} hover>
      <TableCell sx={NAME_CELL_SX}>
        <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, minWidth: 0 }}>
          <Tip title={mark.label}>
            <Box
              component="span"
              role="img"
              aria-label={mark.label}
              tabIndex={0}
              data-testid="project-env-mark"
              data-tone={mark.tone}
              data-applies={mark.applies}
              sx={{ color: TONE_COLOR[mark.tone], fontWeight: FONT_WEIGHT.semibold, flexShrink: 0, width: '1ch', textAlign: 'center' }}
            >
              {mark.glyph}
            </Box>
          </Tip>
          <Box sx={{ minWidth: 0 }}>
            <Link component={RouterLink} to={envVarHref(projectId, row.id)} data-testid="project-env-name" sx={NAME_SX}>
              <BreakAfter text={row.name} sep="_" />
            </Link>
            {notes.length > 0 && (
              <Box data-env-notes="">
                {notes.map(note => (
                  <Typography key={note.key} variant="caption" component="p" data-note={note.key}
                    sx={{ color: NOTE_COLOR[note.tone], overflowWrap: 'anywhere' }}>
                    {note.text}
                  </Typography>
                ))}
              </Box>
            )}
          </Box>
        </Box>
      </TableCell>
      <TableCell sx={VALUE_CELL_SX}>
        <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 0.5, ...VALUE_SX }}>
          <Box data-testid="project-env-source">
            <StatusChip
              tone={fromCredential ? 'success' : 'neutral'}
              glyph={fromCredential ? '●' : row.source === 'literal' ? '○' : undefined}
              label={sourceLabel(row.source)}
              data-status={row.source}
            />
          </Box>
          {fromCredential ? (
            <Box component="span" data-testid="project-env-value">
              {'credential '}
              <span data-testid="project-env-credential-name">{credentialName}</span>
              {', field '}
              <Box component="span" data-testid="project-env-credential-field" sx={monoSx}>{row.credential?.field}</Box>
            </Box>
          ) : row.source === 'literal' ? (
            <Box component="span" data-testid="project-env-value" sx={monoSx}>{row.literal}</Box>
          ) : row.source === 'port' ? (
            /* No number here: the node picks it on each lease. The chip above already says Port. */
            <Box component="span" data-testid="project-env-value">{portValueLabel(row.port_preferred)}</Box>
          ) : null}
        </Box>
      </TableCell>
      <TableCell>
        <Box data-testid="project-env-scope">
          <StatusChip tone={row.scope === 'project' ? 'info' : 'neutral'} label={scopeLabel(row, viewerId)} data-status={row.scope} />
        </Box>
      </TableCell>
      <TableCell align="right" padding="none" sx={{ width: SIZE.tapTarget }}>
        {/* Named for the row (not a bare `Delete`). A row the viewer may not write has no ⋯. */}
        {onDelete && (
          <RowActionsOverflow name={row.name} busy={busy} items={[deleteItem(`environment variable ${row.name}`, onDelete)]} />
        )}
      </TableCell>
    </TableRow>
  )
}

/** THE CATALOGUE: one row per variable, sortable by Name (first click A→Z, then Z→A, then the server's order). */
function EnvVarTable({
  rows, loading, projectId, viewerId, overrideOwners, canEdit, busy, onDelete,
}: {
  rows: ProjectEnvVar[]
  loading: boolean
  projectId: number
  viewerId: string | null
  overrideOwners: Record<string, string>
  canEdit: boolean
  busy: boolean
  onDelete: (row: ProjectEnvVar) => void
}): JSX.Element {
  const [sort, setSort] = useState<SortState<SortKey> | null>(null)
  if (loading) return <LoadingState label="Loading variables…" rows={2} />
  if (rows.length === 0) return <EmptyState inline title="No environment variables" />
  const shown = sort ? rankRows(rows, r => r.name, sort.dir) : rows
  return (
    <DataTable ariaLabel="Environment variables" testId="project-environment-list">
      <TableHead>
        <TableRow>
          <SortableHead label="Name" sortKey="name" sort={sort} align="left" first="asc"
            onSort={key => setSort(s => nextSort(s, key, 'asc'))} testId="project-env-sort-name" />
          <TableCell>Value / Source</TableCell>
          <TableCell>Scope</TableCell>
          <TableCell padding="none"><Box component="span" sx={VISUALLY_HIDDEN}>Actions</Box></TableCell>
        </TableRow>
      </TableHead>
      <TableBody>
        {shown.map(row => (
          <EnvVarRow
            key={row.id}
            row={row}
            projectId={projectId}
            viewerId={viewerId}
            ownerLabel={row.user_id === null ? null : overrideOwners[row.user_id] ?? null}
            onDelete={mayWriteEnvVar(row, canEdit, viewerId) ? () => onDelete(row) : undefined}
            busy={busy}
          />
        ))}
      </TableBody>
    </DataTable>
  )
}

/** THE PREVIEW: the winners as the `.env` lines this tab contributes (`envFileLines`), with a Copy that
 *  takes the same masked text. Read-only: no verb on any line. */
function EnvFilePreview({ rows, viewerId }: { rows: ProjectEnvVar[]; viewerId: string | null }) {
  const lines = envFileLines(rows, viewerId)
  return (
    <Box sx={{ position: 'relative', minWidth: 0 }}>
      <Box sx={{ display: 'flex', justifyContent: 'flex-end', mb: 0.5 }}>
        <CopyButton
          testId="project-env-preview-copy"
          label="Copy"
          ariaLabel="Copy the .env preview"
          text={() => lines.map(l => l.text).join('\n') + '\n'}
        />
      </Box>
      <CodeBlock
        role="figure"
        testId="project-env-preview-file"
        aria-label="What your slot gets, as .env lines"
        size="medium"
        bordered
        sx={{ p: 1.5 }}
      >
        {lines.map((line, i) => (
          <Box
            component="span"
            key={i}
            data-testid="project-env-preview-line"
            data-name={line.name}
            data-tone={line.tone}
            sx={theme => ({
              display: 'block',
              ...(line.tone === 'error' && { bgcolor: alpha(theme.palette.error.main, 0.1) }),
              color: line.tone === 'error'
                ? 'error.main'
                : line.tone === 'warning'
                  ? 'warning.main'
                  : 'text.primary',
            })}
          >
            {line.text}
          </Box>
        ))}
      </CodeBlock>
    </Box>
  )
}

export function ProjectEnvironmentTab({
  projectId,
  canEdit,
  viewerId,
  overrideOwners = {},
}: {
  /** The project whose catalogue this tab manages. Numeric: `project_env_vars.project_id` → `projects.id`. */
  projectId: number
  /** `projects:edit` on THIS project, gating only the project tier: without it defaults render read-only
   *  (no Edit/Delete offered) while own overrides stay writable (R3/D10; the API decorator is
   *  `projects:view` on all five verbs with the tier check inside). */
  canEdit: boolean
  /** The viewer's uuid (`null` while resolving): decides whose override is "yours" (D20 relaxed
   *  VISIBILITY, not authority). */
  viewerId: string | null
  /** `user_id` → a display label, for naming another user's override owner. */
  overrideOwners?: Record<string, string>
}): JSX.Element {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const openPath = useOpenDetailPath()
  const [confirm, confirmDialog] = useConfirm()

  const varsQuery = useQuery({
    queryKey: envVarsKey(projectId),
    queryFn: () => get<ProjectEnvVar[]>(`/api/projects/${projectId}/env-vars`),
  })

  const { busy, run } = useSubmit()
  const [actionError, setActionError] = useState<string | null>(null)
  const [previewOpen, setPreviewOpen] = useState(false)

  const previewQuery = useQuery({
    queryKey: envPreviewKey(projectId),
    queryFn: () => get<ProjectEnvVar[]>(`/api/projects/${projectId}/env-vars/preview`),
    // Fetched only while the section is open.
    enabled: previewOpen,
  })

  const rows = varsQuery.data ?? []
  /* Data wins over `isError`: react-query persists this key, so a reload while the API is down has both. */
  const listState = varsQuery.data ? 'ready' : varsQuery.isError ? 'error' : 'loading'

  async function handleDelete(row: ProjectEnvVar): Promise<void> {
    if (busy) return
    setActionError(null)
    const ok = await confirm(deleteEnvVarConfirm(row.name))
    if (!ok) return
    await run(DELETE_FALLBACK, async () => {
      try {
        await del(`/api/projects/${projectId}/env-vars/${row.id}`)
        await queryClient.invalidateQueries({ queryKey: envVarsKey(projectId) })
        await queryClient.invalidateQueries({ queryKey: envPreviewKey(projectId) })
        // Its page open beside the list would read "not found": close it.
        if (isOpenPath(openPath, envVarHref(projectId, row.id))) navigate(envListHref(projectId), { replace: true })
      } catch (err) {
        setActionError(apiDetail(err, DELETE_FALLBACK))
      }
    })
  }

  return (
    <Stack data-testid="project-environment" spacing={1} sx={{ pt: 1.5, minWidth: 0 }}>
      <Box>
        <Typography id="project-environment-heading" variant="subtitle2" component="h3">
          <HelpLabel label="Environment" help="Written into a slot's .env when it is claimed." />
        </Typography>
      </Box>

      {actionError && (
        <Banner severity="error" testId="project-environment-error" onDismiss={() => setActionError(null)}>
          {actionError}
        </Banner>
      )}

      {listState === 'error' ? (
        <Banner severity="error" testId="project-environment-load-error">
          {apiDetail(varsQuery.error, 'Could not load environment variables.')}
        </Banner>
      ) : (
        <EnvVarTable
          rows={rows}
          loading={listState === 'loading'}
          projectId={projectId}
          viewerId={viewerId}
          overrideOwners={overrideOwners}
          canEdit={canEdit}
          busy={busy}
          onDelete={row => void handleDelete(row)}
        />
      )}

      <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
        <NewButton label="Add variable" testId="project-env-add" disabled={busy}
          onClick={() => navigate(envVarHref(projectId, 'new'))} />
      </Box>

      {/* ── WHAT A SLOT ACTUALLY GETS ──────────────────────────────────────────────────────────────
          A second read: the WINNERS only, for the viewer, behind a toggle; `winning` is the server's. */}
      <Box sx={{ pt: 0.5 }}>
        <Button
          type="button"
          data-testid="project-env-preview-toggle"
          variant="outlined"
          color="inherit"
          aria-expanded={previewOpen}
          onClick={() => setPreviewOpen(open => !open)}
          sx={ROW_ACTION_SX}
        >
          {previewOpen ? 'Hide preview' : 'Show preview'}
        </Button>
      </Box>

      {previewOpen && (
        <Box data-testid="project-env-preview" sx={{ minWidth: 0 }}>
          <Typography variant="subtitle2" component="h4" id="project-env-preview-heading">
            What your slot gets
          </Typography>
          {previewQuery.isPending && <LoadingState label="Resolving…" rows={2} />}
          {previewQuery.isError && (
            <Banner severity="error" testId="project-env-preview-error">
              {apiDetail(previewQuery.error, 'Could not resolve the environment.')}
            </Banner>
          )}
          {previewQuery.data && previewQuery.data.length === 0 && (
            <EmptyState title="Nothing to write" />
          )}
          {previewQuery.data && previewQuery.data.length > 0 && (
            <EnvFilePreview rows={previewQuery.data} viewerId={viewerId} />
          )}
        </Box>
      )}

      {/* Portalled to `document.body`; `FormDialog` stops its submit before it reaches the enclosing `<form>`. */}
      {confirmDialog}
    </Stack>
  )
}

export default ProjectEnvironmentTab
