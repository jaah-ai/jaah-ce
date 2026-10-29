import { type JSX, type ReactNode, useState } from 'react'
import Alert from '@mui/material/Alert'
import AlertTitle from '@mui/material/AlertTitle'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { useQuery } from '@tanstack/react-query'
import { apiDetail, del, get, post, put } from '../api'
import { useAuth } from '../auth'
import type { PortGridPayload, PortGridVar, RebandResult, SettingsPayload } from '../types'
import { knobWritable, SETTINGS_QUERY_KEY } from '../pages/settings/KnobSection'
import { useConfirm } from './ConfirmDialog'
import Banner from '../ui/Banner'
import FeedList from '../ui/feed/FeedList'
import type { FeedRowSpec } from '../ui/feed/types'
import { deleteItem, editItem } from './tasks/rowActions'
import FormTextField from '../ui/FormTextField'
import { monoSx, tapTargetSx } from '../ui/listStyles'
// The app's ONE form-modal frame (lives under `settings/tags` for history's sake).
import FormDialog from '../ui/FormDialog'
import { useApiRead } from '../hooks/useApiRead'
import { useBusyIds } from '../hooks/useBusyIds'
import { useSubmit } from '../hooks/useSubmit'
import LoadingState from '../ui/LoadingState'
import SectionCard from '../ui/SectionCard'

/* Settings' port section: per-service PORT RANGE STARTS, which are DB rows. The five scalars
 * (block size, stride, window, tier-2 floor/ceiling) render as an ordinary server-declared group in
 * `Settings.tsx`; the SET of env vars is open (a project can add `WORKER_PORT` without a deploy), so
 * these are CRUD.
 * A row = TIER 1 (port derived deterministically from the slot number). No row = TIER 2 (node picks from
 * `auto_port_floor`…`auto_port_ceiling` at lease time — works, just unpredictable). Hence Delete's
 * "nothing breaks" copy, and Reband (restarts live stacks) is the one control behind a warning. */

/* Tabular figures so digit columns align for by-eye comparison; the mono stack alone does not
 * guarantee it. */
const NUM_SX = [monoSx, { fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }]

/* The meta line's facts: each owns the "·" in the gap before it (sight only: the `/ ""` alt text keeps it out
 * of the accessible name), centred in that gap. */
const FACT_GAP = '1.5rem'
const FACT_SX = {
  ml: FACT_GAP,
  position: 'relative',
  '&::before': { content: '"·" / ""', position: 'absolute', left: `-${FACT_GAP}`, width: FACT_GAP, textAlign: 'center' },
} as const

/* Trailing cluster by gate strength (`ui/feed/geometry`): RANGE START survives every width; `Used by`
 * and the window gate off at 900px, the description at 768px; all stay in ⋯ → Edit. */
const WINDOW_SLOT = 96
const PROJECTS_SLOT = 104
const DESCRIPTION_SLOT = 176
const RANGE_SLOT = 56

/** The band a var owns, inclusive on both ends — what the derived `Window` slot states. */
function windowLabel(start: number, size: number): string {
  return `${start}–${start + Math.max(size, 1) - 1}`
}

/** The five scalars the knobs line states (`app/port_grid.py`'s keys). */
const PORT_KNOBS = ['port_block_size', 'port_stride', 'port_window', 'auto_port_floor', 'auto_port_ceiling']

/** "1 port", "10 ports". */
function portCount(n: number): string {
  return n === 1 ? '1 port' : `${n} ports`
}

/** "acme and acme-web", "acme, acme-web and acme-api" — the declaring projects as a readable clause. */
function projectClause(projects: string[]): string {
  if (projects.length <= 1) return projects.join('')
  return `${projects.slice(0, -1).join(', ')} and ${projects[projects.length - 1]}`
}

/* The delete confirm must answer "am I about to break something" on its own: the var demotes to tier 2
 * and keeps getting a port, names who is affected, and is reversible. Never blocked or disabled for a
 * declaring project — retiring a service must be able to retire its band. */
function deleteCopy(v: PortGridVar, floor: number, ceiling: number): ReactNode {
  const who = v.projects.length > 0
    ? `${projectClause(v.projects)} still get a port for it`
    : 'No project declares it today, so nothing changes anywhere until one does'
  return (
    <>
      <Typography component="span" variant="body2" sx={{ fontWeight: 600 }}>
        Delete {v.name}? It demotes to tier 2 — nothing breaks.
      </Typography>{' '}
      <Typography component="span" variant="body2" sx={{ color: 'text.secondary' }}>
        {who} — the node auto-assigns one from the flat {floor}–{ceiling} pool at each slot’s next
        lease. No stack restarts. Re-adding the row puts {v.name} back on the grid.
      </Typography>
    </>
  )
}

type VarDraft = { name: string; range_start: string; description: string }
type VarField = keyof VarDraft

/* Create or edit one var off one form, not click-to-edit cells: a feed row cannot edit the description
 * where its slot is gated off, and one write avoids racing PUTs. A dialog, not a route: navigating would
 * remount `Settings` onto its first tab (see `ui/FormDialog`). */
function PortVarDialog({
  row,
  onClose,
  onSaved,
}: {
  /** The var being edited, or `null` to add one. */
  row: PortGridVar | null
  onClose: () => void
  onSaved: () => Promise<void>
}): JSX.Element {
  const [draft, setDraft] = useState<VarDraft>(() => ({
    name: row?.name ?? '',
    range_start: row ? String(row.range_start) : '',
    description: row?.description ?? '',
  }))
  const { busy, errors, setErrors, formError, run } = useSubmit<VarField>()

  const set = (field: VarField, value: string) => setDraft(prev => ({ ...prev, [field]: value }))

  async function submit() {
    const name = draft.name.trim()
    const start = draft.range_start.trim()
    // The server trims `name` but not `description`; trim here so a whitespace label cannot round-trip.
    const description = draft.description.trim()
    /* Both fields checked client-side, beside the field, before the round trip. */
    if (name === '') {
      setErrors({ name: 'Give the variable a name, e.g. API_PORT.' })
      return
    }
    const parsed = Number(start)
    if (start === '' || !Number.isInteger(parsed)) {
      setErrors({ range_start: 'Range start must be a whole port number, e.g. 8000.' })
      return
    }
    /* The server owns overlap/duplicate/range rules; its refusal names a VAR, so it goes in the form banner,
     * not on a field. */
    await run(row ? `Failed to update ${row.name}` : `Failed to add ${name}`, async () => {
      const body = { name, range_start: parsed, description }
      // The whole set on edit, not a diff: no second "same value" check in TypeScript.
      if (row) await put<PortGridVar>(`/api/port-grid/vars/${row.id}`, body)
      else await post<PortGridVar>('/api/port-grid/vars', body)
      await onSaved()
      onClose()
    })
  }

  return (
    <FormDialog
      title={row ? `Edit ${row.name}` : 'Add range start'}
      // "Create": the panel's own button is "Add variable", and two controls must not share a name.
      submitLabel={row ? 'Save' : 'Create'}
      busy={busy}
      error={formError}
      onSubmit={() => void submit()}
      onClose={onClose}
    >
      <FormTextField
        label="Variable"
        error={errors.name}
        help="Exactly as in the project’s .env."
        id="port-grid-name"
        value={draft.name}
        onChange={v => set('name', v)}
        sx={NUM_SX}
      />

      <FormTextField
        label="Range start"
        error={errors.range_start}
        id="port-grid-range-start"
        value={draft.range_start}
        onChange={v => set('range_start', v)}
        // `numeric` raises a digit keypad; not `type="number"`, whose spinner invites off-by-one port nudges.
        slotProps={{ htmlInput: { inputMode: 'numeric' } }}
        sx={NUM_SX}
      />

      <FormTextField
        label="Description"
        error={errors.description}
        id="port-grid-description"
        value={draft.description}
        onChange={v => set('description', v)}
      />
    </FormDialog>
  )
}

export default function PortGridSection() {
  // Every write REFETCHES: the response lacks the recomputed `warnings` and the `projects` resolution.
  const { data, error: loadError, reload } = useApiRead(
    ['port-grid'],
    () => get<PortGridPayload>('/api/port-grid'),
    'Failed to load the port grid',
  )

  const [dialog, setDialog] = useState<{ row: PortGridVar | null } | null>(null)
  const [busyIds, withBusy] = useBusyIds<string>()
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({})

  const { busy: rebanding, formError: rebandError, run: runReband } = useSubmit()
  const [rebandResults, setRebandResults] = useState<RebandResult[] | null>(null)

  /* The knobs line reads the Settings page's cached payload (`enabled: false`: never a fetch of its own); no
     payload, no knobs line. */
  const settingsPayload = useQuery({
    queryKey: SETTINGS_QUERY_KEY,
    queryFn: () => get<SettingsPayload>('/api/settings'),
    enabled: false,
  }).data
  /* The block, stride and window knobs are drawn on this page (the Settings payload serves them): their values
     are in the card above, so the knobs line keeps only what those rows do not say themselves. */
  const knobsShown = ['port_block_size', 'port_stride', 'port_window']
    .every(key => settingsPayload?.settings.some(s => s.key === key) === true)
  // The knobs line points at the knobs only for a viewer who may write one of them.
  const canWriteKnobs = PORT_KNOBS.some(key => knobWritable(settingsPayload, settingsPayload?.settings.find(s => s.key === key)))
  const [confirm, confirmDialog] = useConfirm()
  /* REBAND IS THE OPERATOR'S: it restarts live stacks across every org's nodes, so
     `POST /api/port-grid/reband` refuses every other superadmin. The var CRUD is org-scoped. */
  const canReband = useAuth().admin?.is_operator === true

  function setRowError(id: string, message: string) {
    setRowErrors(prev => ({ ...prev, [id]: message }))
  }

  function clearRowError(id: string) {
    setRowErrors(prev => {
      if (!(id in prev)) return prev
      const next = { ...prev }
      delete next[id]
      return next
    })
  }

  async function confirmAndDelete(v: PortGridVar) {
    if (!data) return
    const ok = await confirm({
      title: 'Delete range start',
      identity: <Typography component="span" sx={monoSx}>{v.name}</Typography>,
      message: deleteCopy(v, data.auto_port_floor, data.auto_port_ceiling),
      confirmLabel: 'Delete',
      destructive: true,
    })
    if (!ok) return
    await withBusy(v.id, async () => {
      try {
        await del(`/api/port-grid/vars/${v.id}`)
        clearRowError(v.id)
        await reload()
      } catch (err) {
        setRowError(v.id, apiDetail(err, `Failed to delete ${v.name}`))
      }
    })
  }

  /* Reband is the one destructive control: row edits change the NEXT lease; this pushes the band onto
   * every existing slot and restarts its stacks. The confirm says what goes down. */
  async function reband() {
    const ok = await confirm({
      title: 'Reband every slot now?',
      message: 'This pushes the recomputed band to every slot on every node and RESTARTS their stacks. '
        + 'Anything running in a slot goes down and comes back on its new ports. Your range starts '
        + 'are already saved; this only decides when the running slots pick them up.',
      confirmLabel: 'Reband and restart stacks',
      destructive: true,
    })
    if (!ok) return
    await runReband('Failed to dispatch the reband', async () => {
      setRebandResults(null)
      const res = await post<{ dispatched: RebandResult[] }>('/api/port-grid/reband', {})
      setRebandResults(res.dispatched ?? [])
    })
  }

  // Sorted by range start so overlaps show as adjacency.
  const vars = [...(data?.vars ?? [])].sort((a, b) => a.range_start - b.range_start)
  const warnings = data?.warnings ?? []
  const windowSize = data?.window ?? 0

  const addVarButton = (
    <Button
      type="button"
      variant="contained"
      color="primary"
      size="small"
      disabled={data === null}
      onClick={() => setDialog({ row: null })}
      sx={tapTargetSx}
    >
      Add variable
    </Button>
  )

  const varRows: FeedRowSpec[] = vars.map((v): FeedRowSpec => {
    const declared = v.projects.length > 0
    const rowError = rowErrors[v.id]
    return {
      key: v.id,
      indicator: {
        /* Neither state is a fault (an undeclared var is a reserved band), so no warning tone; the glyph (filled
         * vs hollow) survives greyscale. */
        tone: declared ? 'info' : 'neutral',
        glyph: declared ? '●' : '○',
        label: declared
          ? `declared by ${projectClause(v.projects)}`
          : 'no project declares it today',
      },
      // The var name is the row's identity (matched against `.env`), so it leads the greedy region.
      title: <Box component="span" sx={[...NUM_SX, { fontWeight: 600 }]}>{v.name}</Box>,
      titleText: v.name,
      busy: busyIds.has(v.id),
      slots: [
        {
          /* Derived, read-only: `range_start` plus the window knob. */
          key: 'window',
          width: WINDOW_SLOT,
          gate: 'meta',
          align: 'right',
          mono: true,
          dim: true,
          title: `Window ${windowLabel(v.range_start, windowSize)}`,
          node: windowLabel(v.range_start, windowSize),
        },
        {
          key: 'projects',
          width: PROJECTS_SLOT,
          gate: 'meta',
          align: 'right',
          title: declared ? `Used by ${projectClause(v.projects)}` : 'No project declares this variable',
          node: declared ? v.projects.join(', ') : 'unused',
        },
        {
          key: 'description',
          width: DESCRIPTION_SLOT,
          gate: 'id',
          align: 'left',
          title: v.description || undefined,
          node: v.description || null,
        },
        {
          key: 'range',
          width: RANGE_SLOT,
          align: 'right',
          mono: true,
          title: `Range start ${v.range_start}`,
          node: v.range_start,
        },
      ],
      actions: [
        // Names WHICH var for screen readers.
        editItem(v.name, () => setDialog({ row: v })),
        deleteItem(v.name, () => void confirmAndDelete(v)),
      ],
      // A failed write is its own row's note.
      notes: rowError ? [{ key: 'error', tone: 'error', text: rowError }] : undefined,
    }
  })

  return (
    // ITS OWN CARD, title bar and all, like the Ports knobs above it (Settings' `framed` panel: not wrapped again).
    <SectionCard title="Range starts" actions={addVarButton} testId="port-grid-section">
      {data && (
        // Prose, so the body font; tabular figures keep the numbers steady. The clip hides the separator of
        // whichever fact starts a line, so a wrap never leaves a "·" dangling at a line's end or start.
        <Box
          data-testid="port-grid-meta"
          sx={{ overflow: 'hidden', fontSize: '0.8125rem', fontVariantNumeric: 'tabular-nums', color: 'text.secondary' }}
        >
          <Box sx={{ display: 'flex', flexWrap: 'wrap', rowGap: '0.25rem', ml: `-${FACT_GAP}` }}>
            {/* Beside Ports knobs an operator set, whose these are — the org's own. */}
            {settingsPayload && !canWriteKnobs && <Box component="span" data-testid="port-grid-owner" sx={FACT_SX}>Set by an admin</Box>}
            {/* The five knobs in plain words (their labels' meaning, not the grid's internal terms) — with the
                knobs on the page, only the tier-2 band, which their rows do not put in those words. */}
            {!knobsShown && (
              <>
                <Box component="span" sx={FACT_SX}>{portCount(data.block_size)} per project per service</Box>
                <Box component="span" sx={FACT_SX}>slots {portCount(data.stride)} apart</Box>
                <Box component="span" sx={FACT_SX}>{portCount(data.window)} per service</Box>
              </>
            )}
            <Box component="span" sx={FACT_SX}>
              Auto-assign {data.auto_port_floor}–{data.auto_port_ceiling} for variables without a range start
            </Box>
            {/* Only for a viewer who may write them: everyone else reads "Set by an operator" over those rows. */}
            {canWriteKnobs && (
              <Box component="span" sx={{ ml: FACT_GAP }}>
                (edit in the knobs above)
              </Box>
            )}
          </Box>
        </Box>
      )}

      {loadError && <Box sx={{ mt: '12px' }}><Banner severity="error">{loadError}</Banner></Box>}

      {/* Titled "Saved" and `role="status"`: these come with stored rows (an overlap may be held mid-migration);
      not a rejected write. */}
      {warnings.length > 0 && (
        <Alert
          severity="warning"
          role="status"
          data-testid="port-grid-warnings"
          sx={{ mt: '12px' }}
        >
          <AlertTitle sx={{ fontSize: '0.8125rem', fontWeight: 600, mb: '2px' }}>
            Saved, with warnings
          </AlertTitle>
          <Box component="ul" sx={{ m: 0, pl: '1.25rem', display: 'grid', gap: '2px' }}>
            {warnings.map(w => (
              <Typography component="li" variant="body2" key={w}>{w}</Typography>
            ))}
          </Box>
        </Alert>
      )}

      {data === null && !loadError && <LoadingState />}

      {data !== null && (
        <Box sx={{ mt: '12px' }}>
          {/* An empty grid means every var is tier 2 — not an error, so one line, not the list's tall empty row. */}
          {varRows.length === 0 ? (
            <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="port-grid-empty">
              No range starts — every variable takes an auto-assign port.
            </Typography>
          ) : (
            <FeedList
              ariaLabel="Port grid range starts"
              testId="port-grid-list"
              sections={[{ key: 'vars', rows: varRows }]}
            />
          )}
        </Box>
      )}

      {canReband && <Box sx={{ mt: '14px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
        <Box sx={{ display: 'flex' }}>
          <Button
            type="button"
            variant="outlined"
            color="warning"
            size="small"
            disabled={rebanding || data === null}
            onClick={() => void reband()}
            sx={tapTargetSx}
          >
            {rebanding ? 'Rebanding…' : 'Reband now'}
          </Button>
        </Box>
        {/* Under the button, not beside it, so the prose keeps its measure. */}
        <Typography variant="body2" sx={{ color: 'text.secondary', maxWidth: '72ch' }}>
          Optional — every slot re-bands at its next lease anyway. This restarts running stacks now.
        </Typography>

        {rebandError && <Banner severity="error">{rebandError}</Banner>}

        {rebandResults && (
          <Alert
            severity={rebandResults.some(r => r.status !== 'ok') ? 'warning' : 'success'}
            role="status"
            data-testid="port-grid-reband-results"
          >
            <AlertTitle sx={{ fontSize: '0.8125rem', fontWeight: 600, mb: '2px' }}>
              {rebandResults.length === 0
                ? 'No nodes to reband'
                : `Reband dispatched to ${rebandResults.length} node${rebandResults.length === 1 ? '' : 's'}`}
            </AlertTitle>
            {/* Per node: a partial reband leaves some nodes on the old band, and the operator needs to know which. */}
            <Box component="ul" sx={{ m: 0, pl: '1.25rem', display: 'grid', gap: '2px' }}>
              {rebandResults.map(r => (
                <Typography component="li" variant="body2" key={r.node_id}>
                  <Box component="span" sx={[...NUM_SX, { fontWeight: 600 }]}>{r.node_id}</Box>
                  {' — '}
                  <Box
                    component="span"
                    sx={{ color: r.status === 'ok' ? 'success.main' : 'error.main', fontWeight: 600 }}
                  >
                    {r.status}
                  </Box>
                  {r.detail ? ` · ${r.detail}` : ''}
                </Typography>
              ))}
            </Box>
          </Alert>
        )}
      </Box>}

      {dialog && (
        <PortVarDialog row={dialog.row} onClose={() => setDialog(null)} onSaved={reload} />
      )}

      {confirmDialog}
    </SectionCard>
  )
}
