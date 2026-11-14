import { useState, type JSX } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../api'
import { danger, useConfirm } from '../ConfirmDialog'
import NonBaaWarning from '../NonBaaWarning'
import { useApiRead } from '../../hooks/useApiRead'
import { useSubmit } from '../../hooks/useSubmit'
import {
  authLabel,
  linkProjectTracker,
  listAvailableTrackers,
  listProjectTrackerContainers,
  listProjectTrackers,
  makeProjectTrackerPrimary,
  NEW_TRACKER_PATH,
  projectTrackersKey,
  unlinkProjectTracker,
  type ProjectTrackerLink,
  type ProjectTrackers,
} from '../../pages/trackers/api'
import ContainerSelect from '../../pages/trackers/ContainerSelect'
import { connectionLabel } from '../trackers/trackerIssuesApi'
import { FONT_WEIGHT, RADIUS, grid } from '../../theme'
import Banner from '../../ui/Banner'
import { credentialMark } from '../../ui/credentialMarks'
import { DetailSection } from '../../ui/detail/DetailPage'
import EmptyState from '../../ui/EmptyState'
import { tapTargetSx } from '../../ui/listStyles'
import LoadFailedState from '../../ui/LoadFailedState'
import LoadingState from '../../ui/LoadingState'
import { StartsTasksChip } from '../../ui/MarkChip'
import SelectField from '../../ui/SelectField'
import { StatusChip } from '../../ui/StatusChip'

/* A PROJECT'S ISSUE TRACKERS (the project page's General › Advanced): the org's tracker connections this project links, and
 * per connection the tracker projects it links (`routes/project_trackers.py`). The same rows as each connection's
 * Mapping tab. Edited in place: Add link / Edit open one inline editor (connection, then its tracker projects), Save
 * writes that connection's links at once (`PUT`), Remove unlinks it after a confirm. A tracker project several Jaah
 * projects link has one primary — the one tracker-started tasks land in ("Starts tasks"); "Make primary" moves it
 * here and shows only where another project shares it. Read-only without `can_edit`; connecting a tracker stays an
 * Owner/Admin's (`can_connect`). Vendor-neutral: labels are the server's `tracker_label`, marks `credentialMark`. */

type Editor = { connectionId: string; picked: string[]; adding: boolean }

const ROW_SX = {
  display: 'flex', flexWrap: 'wrap', alignItems: 'flex-start', columnGap: 1.5, rowGap: 1, py: 1.5,
  borderTop: 1, borderColor: 'divider', '&:first-of-type': { borderTop: 0 }, minWidth: 0,
} as const

/** Connections that read alike (same tracker and name) gain their sign-in kind, so two on one site differ. */
function twinKeys(conns: readonly { tracker: string; name: string }[]): Set<string> {
  const seen = new Map<string, number>()
  for (const c of conns) seen.set(`${c.tracker}\u0000${c.name}`, (seen.get(`${c.tracker}\u0000${c.name}`) ?? 0) + 1)
  return new Set([...seen].filter(([, n]) => n > 1).map(([k]) => k))
}
const withSignIn = (c: { tracker: string; name: string; auth_kind: string }, twins: Set<string>): string =>
  twins.has(`${c.tracker}\u0000${c.name}`) ? `${c.name} · ${authLabel(c.auth_kind)}` : c.name

/** The section's explanation, for a caller that names it itself (`bare`). */
export const TRACKERS_HELP = "Issues this project's tasks start from and link to."

export default function ProjectTrackersSection({ projectId, bare = false }: {
  projectId: number
  /** No titled section: the caller names it (the project page's Advanced); Add link leads the rows. */
  bare?: boolean
}): JSX.Element {
  const client = useQueryClient()
  const key = projectTrackersKey(projectId)
  const read = useQuery({ queryKey: key, queryFn: () => listProjectTrackers(projectId), meta: { persist: false } })
  const body = read.data
  const canEdit = body?.can_edit === true
  const [editor, setEditor] = useState<Editor | null>(null)
  const { busy, formError, setFormError, run } = useSubmit()
  const [confirm, confirmDialog] = useConfirm()
  // Editors need the org's connections: to add one, and to tell "none in the org" from "none linked".
  const available = useApiRead(['project-trackers-available', projectId], () => listAvailableTrackers(projectId),
    'Could not load trackers', { enabled: canEdit })

  const landed = (next: ProjectTrackers) => client.setQueryData(key, next)
  const mapError = (err: unknown, fallback: string) => apiDetail(err, fallback).replace(/^container_ids:\s*/, '')

  const save = async () => {
    if (!editor) return
    const ids = [...new Set(editor.picked.map(id => id.trim()).filter(Boolean))]
    const done = await run('Could not save — try again', () => linkProjectTracker(projectId, editor.connectionId, ids), { mapError })
    if (done === null) return
    landed(done.value)
    setEditor(null)
  }

  const remove = async (link: ProjectTrackerLink) => {
    setFormError('')
    const yes = await confirm(danger('Remove', {
      title: 'Remove link?',
      message: 'New tasks stop starting from it. Tasks already linked to an issue keep their link.',
      identity: connectionLabel(link.connection),
      run: { action: () => unlinkProjectTracker(projectId, link.connection.id), failed: 'Could not remove — try again' },
    }))
    if (!yes) return
    if (editor?.connectionId === link.connection.id) setEditor(null)
    void client.invalidateQueries({ queryKey: key })
  }

  const makePrimary = async (connectionId: string, containerId: string) => {
    const done = await run('Could not change — try again',
      () => makeProjectTrackerPrimary(projectId, connectionId, containerId), { mapError })
    if (done !== null) landed(done.value)
  }

  const linkedIds = new Set((body?.links ?? []).map(l => l.connection.id))
  const addable = (available.data ?? []).filter(c => !linkedIds.has(c.id))
  // Twins across the org's connections and the linked ones (a reader without `can_edit` reads no available list).
  const twins = twinKeys([...(available.data ?? []), ...(body?.links ?? []).map(l => l.connection).filter(c => !available.data?.some(a => a.id === c.id))])
  const addButton = canEdit && editor === null && addable.length > 0 && (
    <Button type="button" variant="outlined" size="small" sx={tapTargetSx} data-testid="project-trackers-add"
      onClick={() => { setFormError(''); setEditor({ connectionId: addable[0].id, picked: [], adding: true }) }}>
      Add link
    </Button>
  )

  let content: JSX.Element
  if (read.isPending) content = <LoadingState rows={2} />
  else if (!body) {
    content = <LoadFailedState title="Issue trackers unavailable" error={apiDetail(read.error, 'Could not load')} onRetry={() => void read.refetch()} />
  } else if (body.links.length === 0 && canEdit && available.data !== null && available.data.length === 0) {
    content = (
      <Box data-testid="project-trackers-none">
        <Typography variant="body2">No tracker connected.</Typography>
        {body.can_connect
          ? <Link component={RouterLink} to={NEW_TRACKER_PATH} underline="hover" variant="body2" data-testid="project-trackers-connect">Add tracker</Link>
          : <Typography variant="body2" sx={{ color: 'text.secondary' }}>Ask an Owner or Admin to connect a tracker.</Typography>}
      </Box>
    )
  } else {
    content = (
      <>
        {body.links.length === 0 && editor === null && <EmptyState inline title="No tracker linked." />}
        {body.links.length > 0 && (
          <Box component="ul" data-testid="project-trackers" sx={{ listStyle: 'none', m: 0, p: 0 }}>
            {body.links.map(link => (
              <LinkRow key={link.connection.id} projectId={projectId} link={link} name={withSignIn(link.connection, twins)}
                canEdit={canEdit} busy={busy}
                editor={editor?.connectionId === link.connection.id && !editor.adding ? editor : null}
                setEditor={setEditor} onSave={() => void save()} onRemove={() => void remove(link)}
                onMakePrimary={containerId => void makePrimary(link.connection.id, containerId)}
                editing={editor !== null} />
            ))}
          </Box>
        )}
        {editor?.adding && (
          <Box data-testid="project-trackers-editor" sx={{ border: 1, borderColor: 'divider', borderRadius: RADIUS.sm, p: 1.5, mt: 1, display: 'flex', flexDirection: 'column', gap: 1.5 }}>
            <SelectField label="Connection" value={editor.connectionId} disabled={busy} testId="project-trackers-connection"
              sx={{ maxWidth: grid(64) }} fullWidth
              options={addable.map(c => ({ value: c.id, label: connectionLabel({ ...c, name: withSignIn(c, twins) }) }))}
              onChange={id => setEditor({ ...editor, connectionId: id, picked: [] })} />
            <ContainerPicker projectId={projectId} editor={editor} setEditor={setEditor} disabled={busy} />
            <EditorActions busy={busy} picked={editor.picked} saveLabel="Add" onSave={() => void save()} onCancel={() => setEditor(null)} />
          </Box>
        )}
      </>
    )
  }

  // One warning per tracker the project links; the server decides which are warned (`hipaa.SURFACES`).
  const trackers = [...new Set((body?.links ?? []).map(l => l.connection.tracker))]
  const inner = (
    <Box data-testid="project-trackers-section" sx={{ maxWidth: grid(120), minWidth: 0 }}>
      {(formError || available.error) && <Banner severity="error" testId="project-trackers-error">{formError || available.error}</Banner>}
      {content}
      {trackers.map(t => <NonBaaWarning key={t} surface={t} />)}
    </Box>
  )
  if (bare) {
    return (
      <Box sx={{ minWidth: 0 }}>
        {inner}
        {addButton && <Box sx={{ mt: 0.5 }}>{addButton}</Box>}
        {confirmDialog}
      </Box>
    )
  }
  return (
    <DetailSection title="Issue trackers" help={TRACKERS_HELP} divided actions={addButton || undefined}>
      {inner}
      {confirmDialog}
    </DetailSection>
  )
}

function LinkRow({ projectId, link, name, canEdit, busy, editor, editing, setEditor, onSave, onRemove, onMakePrimary }: {
  projectId: number
  link: ProjectTrackerLink
  /** The connection's name, with its sign-in kind when another connection reads alike. */
  name: string
  canEdit: boolean
  busy: boolean
  editor: Editor | null
  editing: boolean
  setEditor: (e: Editor | null) => void
  onSave: () => void
  onRemove: () => void
  onMakePrimary: (containerId: string) => void
}): JSX.Element {
  const { connection: conn } = link
  return (
    <Box component="li" sx={ROW_SX} data-testid={`project-tracker-${conn.id}`}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flex: `1 1 ${grid(30)}`, minWidth: 0 }}>
        <Box sx={{ display: 'flex', flexShrink: 0 }}>{credentialMark(conn.tracker, conn.tracker_label)}</Box>
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.semibold, overflowWrap: 'anywhere' }}>{conn.tracker_label}</Typography>
          <Typography variant="caption" component="div" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
            {name}
          </Typography>
          {conn.site && !conn.name.includes(conn.site) && (
            <Typography variant="caption" component="div" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>{conn.site}</Typography>
          )}
        </Box>
      </Box>
      <Box sx={{ flex: `2 1 ${grid(40)}`, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 0.5 }}>
        {conn.error && <Typography variant="body2" color="error" data-testid="project-tracker-error" sx={{ overflowWrap: 'anywhere' }}>{conn.error}</Typography>}
        {editor ? (
          <>
            <ContainerPicker projectId={projectId} editor={editor} setEditor={setEditor} disabled={busy} />
            <EditorActions busy={busy} picked={editor.picked} saveLabel="Save" onSave={onSave} onCancel={() => setEditor(null)} />
          </>
        ) : link.needs_pick || link.containers.length === 0 ? (
          <Box><StatusChip label="No projects picked" tone="warning" testId="project-tracker-needs-pick" /></Box>
        ) : (
          <Box component="ul" sx={{ listStyle: 'none', m: 0, p: 0, display: 'flex', flexDirection: 'column', gap: 0.5 }}>
            {link.containers.map(c => (
              <Box component="li" key={c.container_id} data-testid="project-tracker-container"
                sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 1, minWidth: 0 }}>
                <Typography variant="body2" sx={{ overflowWrap: 'anywhere', minWidth: 0 }}>{c.name || c.container_id}</Typography>
                {c.primary && <StartsTasksChip />}
                {canEdit && !c.primary && c.shared > 0 && (
                  <Button type="button" size="small" disabled={busy || editing} sx={tapTargetSx} data-testid="project-tracker-make-primary"
                    onClick={() => onMakePrimary(c.container_id)}>
                    Make primary
                  </Button>
                )}
              </Box>
            ))}
          </Box>
        )}
      </Box>
      {canEdit && !editor && (
        <Box sx={{ display: 'flex', gap: 0.5, flexShrink: 0, ml: 'auto' }}>
          <Button type="button" size="small" disabled={busy || editing} sx={tapTargetSx} data-testid="project-tracker-edit"
            aria-label={`Edit ${conn.name}`}
            onClick={() => setEditor({ connectionId: conn.id, picked: link.containers.map(c => c.container_id), adding: false })}>
            Edit
          </Button>
          <Button type="button" size="small" color="error" disabled={busy || editing} sx={tapTargetSx} data-testid="project-tracker-remove"
            aria-label={`Remove ${conn.name}`} onClick={onRemove}>
            Remove
          </Button>
        </Box>
      )}
    </Box>
  )
}

/** The editing connection's tracker projects, read when the editor opens; a 502 carries the tracker's sentence. */
function ContainerPicker({ projectId, editor, setEditor, disabled }: {
  projectId: number
  editor: Editor
  setEditor: (e: Editor) => void
  disabled: boolean
}): JSX.Element {
  const containers = useApiRead(['project-tracker-containers', projectId, editor.connectionId],
    () => listProjectTrackerContainers(projectId, editor.connectionId), 'Could not load projects')
  if (containers.loading && containers.data === null) return <LoadingState rows={1} />
  return (
    <Box>
      <ContainerSelect multiple label="Tracker projects" containers={containers.data} error={containers.error}
        value={editor.picked} disabled={disabled} testId="project-trackers-containers"
        onChange={ids => setEditor({ ...editor, picked: ids })} />
    </Box>
  )
}

function EditorActions({ busy, picked, saveLabel, onSave, onCancel }: {
  busy: boolean; picked: string[]; saveLabel: string; onSave: () => void; onCancel: () => void
}): JSX.Element {
  const none = !picked.some(id => id.trim())
  return (
    <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', alignItems: 'center' }}>
      <Button type="button" variant="contained" size="small" disabled={busy || none} sx={tapTargetSx}
        data-testid="project-trackers-save" onClick={onSave}>
        {saveLabel}
      </Button>
      <Button type="button" size="small" disabled={busy} sx={tapTargetSx} data-testid="project-trackers-cancel" onClick={onCancel}>
        Cancel
      </Button>
      {none && <Typography variant="caption" sx={{ color: 'text.secondary' }}>Pick at least one tracker project</Typography>}
    </Box>
  )
}
