import { type JSX, type ReactNode, useEffect, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import FormControlLabel from '@mui/material/FormControlLabel'
import FormHelperText from '@mui/material/FormHelperText'
import Link from '@mui/material/Link'
import MenuItem from '@mui/material/MenuItem'
import Switch from '@mui/material/Switch'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { useAuth } from '../../../auth'
import { useRevealOrg } from '../../../hooks/useOrgScopeUrl'
import { useSubmit } from '../../../hooks/useSubmit'
import Banner from '../../../ui/Banner'
import FormField from '../../../ui/FormField'
import FormTextField from '../../../ui/FormTextField'
import { fieldTapTargetSx, monoSx, tapTargetSx } from '../../../ui/listStyles'
import OrgSelect, { ORG_FIELD_LABEL } from '../../../ui/OrgSelect'
import type { AssignableProject, CloudConnectionOption, NodeGroupMarket } from '../../../types'
import {
  CLOUD_CONNECTIONS_PATH,
  CLOUD_CONNECTIONS_URL,
  connectionLabel,
} from '../../settings/cloudConnections/api'
import FormDialog from '../../../ui/FormDialog'
import RelativeTime from '../../../ui/RelativeTime'
import { useAwaitingApprovalReason } from '../../../lib/orgLifecycle'
import NodeLinkPicker, { pickableNodeLinks } from '../../../components/NodeLinkPicker'
import { listNodeLinks } from '../nodeLinkStore/api'
import { createNodeGroup, updateNodeGroup, type NodeGroupWrite } from './api'
import type { NodeGroupRow } from '../../../types'

/* The one form that creates AND edits a node group (`row: NodeGroupRow | null`, as
 * `settings/cloudConnections/ConnectionDialog.tsx`), on the `ui/FormDialog` shell.
 * Its own file because it carries two EDITABLE CHILD TABLES.
 * It pre-empts two server 422s that would otherwise arrive as prose:
 *   · A DUPLICATE (market, instance_type) pair — named here.
 *   · NO CLOUD CONNECTION — a group launches through one (account, region, network live in that row);
 *     the form names where to add it.
 * Instance-type/quota questions are the server's, rendered whole via `apiDetail`. An EMPTY capacity table
 * is allowed (a group parked at zero) and said, not blocked.
 */

/* ── Line geometry ────────────────────────────────────────────────────────────────────────────────
   WRAPPING FLEX LINES, not a `<table>` (which would scroll sideways at 390px). Inside `maxWidth="sm"`:
     · desktop  ~552px of content — every control fits one line (128 + 128 + 104 + 44 + gaps = 428).
     · 390px    ~278px of content — the two selects share line 1 (128 + 128 = 256 ≤ 278) and the number
                and the remove control share line 2.
   Bases in `rem` so a larger root font moves the wrap point. */
const LINE_SELECT_SX = { flex: '1 1 8rem', minWidth: 0 } as const
/* 6.5rem fits three digits plus MUI's padding; fixed so it does not read as a text field. */
const LINE_NUMBER_SX = { flex: '0 0 6.5rem', minWidth: 0 } as const
/* A project name is a human name (often 20+ chars): a basis that keeps it whole; a line is the project alone. */
const LINE_PROJECT_SX = { flex: '1 1 12rem', minWidth: 0 } as const
/* `ml: 'auto'` keeps remove at the end of whichever line it wraps onto; 44px is a real target on desktop
   too (destructive control on a dense line). */
const LINE_REMOVE_SX = {
  flex: '0 0 auto',
  ml: 'auto',
  alignSelf: 'center',
  minWidth: 44,
  height: 40,
  px: 1,
  color: 'text.secondary',
  '&:hover': { color: 'error.main', bgcolor: 'action.hover' },
} as const

/* UNDER A LINE LIST: its validation error, then the add button in its own row. */
function LineListFooter({ error, errorTestId, addTestId, addDisabled, onAdd, children }: {
  error: string
  errorTestId: string
  addTestId: string
  addDisabled: boolean
  onAdd: () => void
  children: ReactNode
}) {
  return (
    <>
      {error && <FormHelperText error data-testid={errorTestId}>{error}</FormHelperText>}
      <Box>
        <Button type="button" size="small" data-testid={addTestId} disabled={addDisabled} onClick={onAdd} sx={tapTargetSx}>
          {children}
        </Button>
      </Box>
    </>
  )
}

/** The hairline says which number belongs to which type when the line wraps. */
const LINE_SX = {
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'flex-start',
  columnGap: 1,
  rowGap: 1,
  p: 1.25,
  border: 1,
  borderColor: 'divider',
  borderRadius: 1,
} as const

/** Reds the offending line's border; the message lives at section level (see `capacityError`). */
const LINE_INVALID_SX = { borderColor: 'error.main' } as const

/* Each child table opens on a hairline: a clear section break without another heading level. */
const CHILD_TABLE_SX = {
  display: 'flex',
  flexDirection: 'column',
  gap: 1.25,
  pt: 2,
  borderTop: 1,
  borderColor: 'divider',
} as const

const SECTION_TITLE_SX = { fontSize: '0.8125rem', fontWeight: 600 } as const

/* THE ONE CONTROL THAT SPENDS MONEY, in its own box, tinted warning while ON. */
const ENABLED_BOX_SX = {
  p: 2,
  border: 1,
  borderRadius: 1,
  display: 'flex',
  flexDirection: 'column',
  gap: 0.5,
} as const
const ENABLED_ON_SX = {
  borderColor: 'warning.main',
  bgcolor: 'rgba(var(--mui-palette-warning-mainChannel) / 0.10)',
} as const
const ENABLED_OFF_SX = { borderColor: 'divider' } as const

const MARKETS: { value: NodeGroupMarket; label: string }[] = [
  { value: 'on_demand', label: 'On-demand' },
  { value: 'spot', label: 'Spot' },
]

const MARKET_LABEL: Record<NodeGroupMarket, string> = {
  on_demand: 'On-demand',
  spot: 'Spot',
}

/** EVERY NUMBER IS A STRING (parsing on input turns a half-typed "12" into 1); parsed once at submit. */
type CapacityDraft = { market: NodeGroupMarket; instance_type: string; desired: string }
type ProjectDraft = { project_id: string }

type Draft = {
  name: string
  description: string
  connection_id: string
  enabled: boolean
  capacity: CapacityDraft[]
  projects: ProjectDraft[]
}

function draftFrom(row: NodeGroupRow | null, instanceTypes: string[]): Draft {
  return {
    name: row?.name ?? '',
    description: row?.description ?? '',
    connection_id: row?.connection_id ?? '',
    /* A NEW GROUP OPENS DISABLED, ALWAYS — a deliberate departure from the server default: the
       money-spending state must never be reached by NOT reading a control. */
    enabled: row?.enabled ?? false,
    capacity: row
      ? row.capacity.map(c => ({
          market: c.market,
          instance_type: c.instance_type,
          desired: String(c.desired),
        }))
      /* ONE EMPTY LINE ON A CREATE, pre-pointed at the first allowed type; none with an empty allowlist (the
         banner says why). */
      : instanceTypes.length > 0
        ? [{ market: 'on_demand' as NodeGroupMarket, instance_type: instanceTypes[0], desired: '1' }]
        : [],
    projects: row
      ? row.projects.map(p => ({ project_id: String(p.project_id) }))
      : [],
  }
}

/** `market · instance_type` — the pair a duplicate refusal names, spelled once. */
function pairLabel(line: CapacityDraft): string {
  return `${MARKET_LABEL[line.market]} · ${line.instance_type}`
}

export function NodeGroupDialog({
  row,
  connections,
  instanceTypes,
  projects,
  optionsError,
  onClose,
  onSaved,
}: {
  /** The group being edited, or `null` to create one. */
  row: NodeGroupRow | null
  /** `GET /api/cloud-connections/options`'s `connections`: what the group may launch through. */
  connections: CloudConnectionOption[]
  /** EMPTY IS A REAL SETTING (cloud launches off), so the form says so. */
  instanceTypes: string[]
  projects: AssignableProject[]
  /** Rendered at the top; the form still opens (rename or turn OFF remain possible). */
  optionsError: string
  onClose: () => void
  onSaved: () => Promise<void>
}): JSX.Element {
  const awaitingApproval = useAwaitingApprovalReason()
  const [draft, setDraft] = useState<Draft>(() => {
    const initial = draftFrom(row, instanceTypes)
    if (row) return initial
    /* PRESELECTED TO THE FIRST CONNECTION (as `components/addNode/AddNodeDialog`): a group with none cannot be
       saved, and the usual fleet has one. */
    const first = connections[0]
    return first ? { ...initial, connection_id: first.id } : initial
  })
  const [nameError, setNameError] = useState('')
  const [connectionError, setConnectionError] = useState('')
  /* THE TENANT IS NOT PART OF `Draft`: the route takes `org_id` on POST only (`./api.ts`), so keeping it out
     makes "an edit cannot send it" structural. */
  const [orgId, setOrgId] = useState('')
  const [orgError, setOrgError] = useState('')
  /* The org picker is the operator's (`GET /api/orgs`); every other superadmin files into their own. */
  const { admin } = useAuth()
  const isOperator = admin?.is_operator === true
  const revealOrg = useRevealOrg()
  /* SECTION-LEVEL REFUSALS, a considered departure from the helper-slot rule: a duplicate belongs to two lines
     and four controls, so the sentence goes under its table and `*Invalid` reds the line's border. */
  const [capacityError, setCapacityError] = useState('')
  const [capacityInvalid, setCapacityInvalid] = useState<number | null>(null)
  const [projectError, setProjectError] = useState('')
  const [projectInvalid, setProjectInvalid] = useState<number | null>(null)
  const { busy, formError, setFormError, run } = useSubmit()

  /* NODELINKS: the group's org's. The caller's own while it has NodeLink; for the operator filing for ANOTHER
     org, that org's (`listNodeLinks(orgId)`), offered whenever it has any — its plan is the server's to enforce,
     not the caller's. `nodeLinkIds` null = untouched: a create sends every one not on Jaah-managed storage (all
     start checked; `nodelinks.birth_set` refuses a managed one), an edit sends nothing (it leaves the
     group's own set). */
  const ownOrgId = admin?.org?.id ?? ''
  const groupOrgId = row ? row.org_id : isOperator ? orgId : ownOrgId
  const forOtherOrg = groupOrgId !== '' && groupOrgId !== ownOrgId
  const nodeLinksQuery = useQuery({
    queryKey: forOtherOrg ? ['nodelinks', groupOrgId] : ['nodelinks'],
    queryFn: () => listNodeLinks(forOtherOrg ? groupOrgId : undefined),
    enabled: forOtherOrg ? isOperator : admin?.nodelink_enabled === true,
  })
  const nodeLinks = pickableNodeLinks(nodeLinksQuery.data ?? [])
  const offerNodeLinks = groupOrgId !== '' && (forOtherOrg ? isOperator : admin?.nodelink_enabled === true)
    && nodeLinks.length > 0
  const [nodeLinkIds, setNodeLinkIds] = useState<string[] | null>(null)
  const pickedNodeLinkIds = nodeLinkIds ?? row?.nodelink_ids ?? nodeLinks.filter(l => !l.store_managed).map(l => l.id)
  const sendsNodeLinks = offerNodeLinks && (!row || nodeLinkIds !== null)

  /* THE ALLOWLIST CAN ARRIVE AFTER THE FORM (`useState` initialises once). Fills only a still-empty draft,
     never a touched value. */
  useEffect(() => {
    if (row) return
    setDraft(prev => {
      const nextConnection = prev.connection_id === ''
        ? connections[0]?.id ?? ''
        : prev.connection_id
      const nextCapacity = prev.capacity.length === 0 && instanceTypes.length > 0
        ? [{ market: 'on_demand' as NodeGroupMarket, instance_type: instanceTypes[0], desired: '1' }]
        : prev.capacity
      if (nextConnection === prev.connection_id && nextCapacity === prev.capacity) return prev
      return { ...prev, connection_id: nextConnection, capacity: nextCapacity }
    })
  }, [row, connections, instanceTypes])

  const projectById = new Map(projects.map(p => [String(p.id), p]))
  /* The row can name a connection the dropdown does not offer (two reads; MUI renders a missing value
     BLANK). This form's rule is that an unlisted connection is not a choice, so it is a disabled option. */
  const ownConnectionUnlisted = row !== null && row.connection_id !== ''
    && !connections.some(c => c.id === row.connection_id)
  /* Likewise a saved line can name a project the dropdown lacks (MUI renders a missing value BLANK). The
     group row carries `project_name`, so the synthetic option says what it points at. */
  const ownProjectNames = new Map((row?.projects ?? []).map(p => [String(p.project_id), p.project_name]))

  function setField<K extends keyof Draft>(field: K, value: Draft[K]) {
    setDraft(prev => ({ ...prev, [field]: value }))
  }

  /** A line edit drops the section refusal it might have fixed, so a stale message never lingers. */
  function editCapacity(index: number, patch: Partial<CapacityDraft>) {
    setDraft(prev => ({
      ...prev,
      capacity: prev.capacity.map((line, i) => (i === index ? { ...line, ...patch } : line)),
    }))
    setCapacityError('')
    setCapacityInvalid(null)
  }

  function editProject(index: number, patch: Partial<ProjectDraft>) {
    setDraft(prev => ({
      ...prev,
      projects: prev.projects.map((line, i) => (i === index ? { ...line, ...patch } : line)),
    }))
    setProjectError('')
    setProjectInvalid(null)
  }

  function addCapacityLine() {
    setDraft(prev => ({
      ...prev,
      capacity: [
        ...prev.capacity,
        /* A new line opens on the first type not already held, so Add twice is no duplicate; falls back to
           the first allowed type (the refusal then names the pair). */
        {
          market: 'on_demand' as NodeGroupMarket,
          instance_type:
            instanceTypes.find(t => !prev.capacity.some(l => l.market === 'on_demand' && l.instance_type === t))
            ?? instanceTypes[0]
            ?? '',
          desired: '1',
        },
      ],
    }))
    setCapacityError('')
    setCapacityInvalid(null)
  }

  function addProjectLine() {
    setDraft(prev => ({
      ...prev,
      projects: [
        ...prev.projects,
        {
          // First unassigned project; `''` leaves the submit refusal to say so.
          project_id: String(projects.find(p => !prev.projects.some(l => l.project_id === String(p.id)))?.id ?? ''),
        },
      ],
    }))
    setProjectError('')
    setProjectInvalid(null)
  }

  function removeCapacityLine(index: number) {
    setDraft(prev => ({ ...prev, capacity: prev.capacity.filter((_, i) => i !== index) }))
    setCapacityError('')
    setCapacityInvalid(null)
  }

  function removeProjectLine(index: number) {
    setDraft(prev => ({ ...prev, projects: prev.projects.filter((_, i) => i !== index) }))
    setProjectError('')
    setProjectInvalid(null)
  }

  function clearRefusals() {
    setNameError('')
    setConnectionError('')
    setCapacityError('')
    setCapacityInvalid(null)
    setProjectError('')
    setProjectInvalid(null)
    setOrgError('')
    setFormError('')
  }

  async function submit() {
    clearRefusals()
    const name = draft.name.trim()
    const description = draft.description.trim()
    if (name === '') {
      setNameError('Name is required.')
      return
    }
    if (draft.connection_id === '') {
      setConnectionError('Pick a connection.')
      return
    }
    // CREATE ONLY, matching the field.
    if (row === null && isOperator && orgId === '') {
      setOrgError('Pick the organization this group belongs to.')
      return
    }

    /* ── The capacity table ──────────────────────────────────────────────────────────────────────── */
    const seenPairs = new Set<string>()
    const capacity: NodeGroupWrite['capacity'] = []
    for (const [index, line] of draft.capacity.entries()) {
      if (line.instance_type === '') {
        setCapacityInvalid(index)
        setCapacityError(`Line ${index + 1}: pick an instance type, or remove the line.`)
        return
      }
      const pair = `${line.market}|${line.instance_type}`
      if (seenPairs.has(pair)) {
        setCapacityInvalid(index)
        // NAMES THE PAIR — "duplicate line" among near-identical rows is a hunt.
        setCapacityError(`${pairLabel(line)} is already a line above — combine them.`)
        return
      }
      seenPairs.add(pair)
      const desired = Number(line.desired.trim())
      /* ZERO IS ALLOWED (keeps the shape while handing nodes back), negative is not. BLANK is refused — far
         more often an unfinished edit than a deliberate zero. */
      if (line.desired.trim() === '' || !Number.isInteger(desired) || desired < 0) {
        setCapacityInvalid(index)
        setCapacityError(`Line ${index + 1} (${pairLabel(line)}): instances must be a whole number, 0 or more.`)
        return
      }
      capacity.push({ market: line.market, instance_type: line.instance_type, desired })
    }

    /* ── The project table ───────────────────────────────────────────────────────────────────────── */
    const seenProjects = new Set<string>()
    const assigned: NodeGroupWrite['projects'] = []
    for (const [index, line] of draft.projects.entries()) {
      if (line.project_id === '') {
        setProjectInvalid(index)
        setProjectError(`Line ${index + 1}: pick a project, or remove the line.`)
        return
      }
      const project = projectById.get(line.project_id)
      if (seenProjects.has(line.project_id)) {
        setProjectInvalid(index)
        setProjectError(`${project?.name ?? 'That project'} is already a line above — combine them.`)
        return
      }
      seenProjects.add(line.project_id)
      assigned.push({ project_id: Number(line.project_id) })
    }

    /* THE SERVER'S SENTENCE, WHOLE: refusals name AWS objects, nothing to route on. */
    await run(row ? `Failed to save ${row.name}.` : `Failed to create ${name}.`, async () => {
      const body: NodeGroupWrite = {
        name,
        description,
        connection_id: draft.connection_id,
        enabled: draft.enabled,
        // BOTH LISTS ALWAYS SENT, `[]` included — the route replaces, not merges (see `api.ts`).
        capacity,
        projects: assigned,
        ...(sendsNodeLinks ? { nodelinks: pickedNodeLinkIds } : {}),
      }
      if (row) await updateNodeGroup(row.id, body)
      // `org_id` only on create.
      else {
        await createNodeGroup({ ...body, org_id: isOperator ? orgId : admin?.org?.id ?? '' })
        // The operator's org filter gains the group's org, so the new group is not hidden.
        if (isOperator) void revealOrg(orgId)
      }
      await onSaved()
      onClose()
    })
  }

  return (
    <FormDialog
      title={row ? `Edit ${row.name}` : 'New node group'}
      // Never "Add": the panel's control is New group; two same-named buttons are indistinguishable.
      submitLabel={row ? 'Save group' : 'Create group'}
      busy={busy}
      error={formError}
      onSubmit={() => void submit()}
      onClose={onClose}
      /* CREATE ONLY: a new group raises capacity, which a pending org may not. An edit stays open — lowering or
         pausing is allowed, and a raise is refused by the server in its own words. */
      submitDisabledReason={row === null ? awaitingApproval : undefined}
    >
      {optionsError && <Banner severity="error">{optionsError}</Banner>}

      {/* `last_error` IN FULL — the one surface with room for the server's whole sentence. */}
      {row && row.last_error !== '' && (
        <Banner severity="error" testId="node-group-dialog-last-error">
          <Typography component="span" variant="body2">
            <strong>Not converging.</strong> {row.last_error}
          </Typography>
        </Banner>
      )}

      {/* No error and no reconcile stamp is not healthy — the loop is not running for this group. */}
      {row && (
        <Typography
          variant="caption"
          data-testid="node-group-dialog-reconciled"
          sx={{ color: 'text.secondary', mt: -1 }}
        >
          {row.node_count} of {row.desired_total} instances ·{' '}
          {row.last_reconciled_at ? (
            <RelativeTime iso={row.last_reconciled_at} prefix="reconciled" />
          ) : 'never reconciled'}
        </Typography>
      )}

      <FormTextField
        label="Name" error={nameError} id="node-group-name"
        value={draft.name}
        onChange={v => { setField('name', v); if (nameError) setNameError('') }}
      />

      <FormTextField
        label="Description (optional)"
        id="node-group-description"
        placeholder="Spot builders for nightly sweep"
        value={draft.description}
        onChange={v => setField('description', v)}
      />

      {/* Banner only when there is NO connection to pick (as `components/addNode/AddNodeDialog`). */}
      {connections.length === 0 && optionsError === '' && (
        <Banner severity="warning" testId="node-group-no-connection">
          <Typography component="span" variant="body2">
            No cloud connection yet. Add one in{' '}
            <Link component={RouterLink} to={CLOUD_CONNECTIONS_URL} onClick={onClose}>{CLOUD_CONNECTIONS_PATH}</Link>.
          </Typography>
        </Banner>
      )}

      <FormField
        label="Connection"
        error={connectionError}
        hint={ownConnectionUnlisted ? 'Not one you can see — switching away is permanent.' : ''}
      >
        <TextField
          select
          value={draft.connection_id}
          onChange={e => { setField('connection_id', e.target.value); setConnectionError('') }}
          disabled={connections.length === 0 && !ownConnectionUnlisted}
          slotProps={{ htmlInput: { 'data-testid': 'node-group-connection' } }}
          sx={fieldTapTargetSx}
        >
          {ownConnectionUnlisted && row && (
            /* Disabled synthetic option (as the project field): shows what it holds, never re-pointable to. */
            <MenuItem value={row.connection_id} disabled>
              {row.connection_name || `connection ${row.connection_id}`}
            </MenuItem>
          )}
          {connections.map(c => (
            <MenuItem key={c.id} value={c.id}>{connectionLabel(c)}</MenuItem>
          ))}
        </TextField>
      </FormField>

      {/* CREATE ONLY: `PUT /api/node-groups/{id}` accepts no `org_id` (`./api.ts`), so an edit control would
          silently lie. */}
      {row === null && isOperator && (
        <FormField label={ORG_FIELD_LABEL} error={orgError}>
          <OrgSelect
            id="node-group-org"
            value={orgId === '' ? null : orgId}
            // Same edge conversion as the picker above.
            onChange={next => { setOrgId(next); setOrgError(''); setNodeLinkIds(null) }}
          />
        </FormField>
      )}

      {offerNodeLinks && (
        <NodeLinkPicker
          id="node-group-nodelinks"
          options={nodeLinks}
          value={pickedNodeLinkIds}
          onChange={setNodeLinkIds}
          disabled={busy}
        />
      )}

      {/* ── CAPACITY ──────────────────────────────────────────────────────────────────────────────── */}
      <Box component="section" data-testid="node-group-capacity" sx={CHILD_TABLE_SX}>
        <Typography variant="subtitle2" sx={SECTION_TITLE_SX}>
          Capacity
        </Typography>

        {instanceTypes.length === 0 && optionsError === '' && (
          <Banner severity="warning" testId="node-group-no-instance-types">
            <Typography component="span" variant="body2">
              No instance types allowed. Add them under{' '}
              <strong>Settings &gt; Infrastructure &gt; Cloud</strong>.
            </Typography>
          </Banner>
        )}

        {draft.capacity.map((line, index) => (
          <Box
            key={index}
            role="group"
            aria-label={`Capacity line ${index + 1}`}
            data-testid="node-group-capacity-line"
            sx={capacityInvalid === index ? [LINE_SX, LINE_INVALID_SX] : LINE_SX}
          >
            <Box sx={LINE_SELECT_SX}>
              <FormField label="Market">
                <TextField
                  select
                  value={line.market}
                  onChange={e => editCapacity(index, { market: e.target.value as NodeGroupMarket })}
                  slotProps={{ htmlInput: { 'data-testid': 'capacity-market' } }}
                  sx={fieldTapTargetSx}
                >
                  {MARKETS.map(m => <MenuItem key={m.value} value={m.value}>{m.label}</MenuItem>)}
                </TextField>
              </FormField>
            </Box>

            <Box sx={LINE_SELECT_SX}>
              <FormField label="Instance type">
                <TextField
                  select
                  value={line.instance_type}
                  onChange={e => editCapacity(index, { instance_type: e.target.value })}
                  disabled={instanceTypes.length === 0}
                  slotProps={{ htmlInput: { 'data-testid': 'capacity-type' } }}
                  // Mono: compared by eye against AWS console types (`t4g.small` vs `t3a.small`).
                  sx={[monoSx, fieldTapTargetSx]}
                >
                  {/* A saved line can name a type the allowlist no longer offers: a disabled synthetic option
                      so the control shows what it holds, not blank. */}
                  {line.instance_type !== '' && !instanceTypes.includes(line.instance_type) && (
                    <MenuItem value={line.instance_type} disabled>
                      {line.instance_type}
                      <Box component="span" sx={{ ml: 1, color: 'text.disabled', fontSize: '0.75rem' }}>
                        — no longer on the allowlist
                      </Box>
                    </MenuItem>
                  )}
                  {instanceTypes.map(t => <MenuItem key={t} value={t}>{t}</MenuItem>)}
                </TextField>
              </FormField>
            </Box>

            <Box sx={LINE_NUMBER_SX}>
              <FormTextField
                label="Instances"
                value={line.desired}
                onChange={v => editCapacity(index, { desired: v })}
                // `numeric` raises a digit keypad; not `type="number"` — spinner nudges start EC2 instances.
                slotProps={{ htmlInput: { inputMode: 'numeric' } }}
                testId="capacity-desired"
                sx={monoSx}
              />
            </Box>

            <Button
              type="button"
              data-testid="capacity-remove"
              aria-label={`Remove capacity line ${index + 1}, ${pairLabel(line)}`}
              onClick={() => removeCapacityLine(index)}
              sx={LINE_REMOVE_SX}
            >
              <Box component="span" aria-hidden="true">✕</Box>
            </Button>
          </Box>
        ))}

        <LineListFooter
          error={capacityError} errorTestId="node-group-capacity-error"
          addTestId="node-group-add-capacity" addDisabled={instanceTypes.length === 0} onAdd={addCapacityLine}
        >
          + Add line
        </LineListFooter>

        {draft.capacity.length === 0 && instanceTypes.length > 0 && (
          // SAID, not blocked: a group parked at zero capacity is real, but must not be silent.
          <Typography variant="caption" data-testid="node-group-no-capacity" sx={{ color: 'text.secondary' }}>
            No capacity — this group starts nothing.
          </Typography>
        )}
      </Box>

      {/* ── PROJECTS ──────────────────────────────────────────────────────────────────────────────── */}
      <Box component="section" data-testid="node-group-projects" sx={CHILD_TABLE_SX}>
        <Typography variant="subtitle2" sx={SECTION_TITLE_SX}>
          Projects
        </Typography>

        {draft.projects.map((line, index) => {
          const project = projectById.get(line.project_id)
          // See `ownProjectNames`.
          const projectUnlisted = line.project_id !== '' && project === undefined
          return (
            <Box
              key={index}
              role="group"
              aria-label={`Project assignment ${index + 1}`}
              data-testid="node-group-project-line"
              sx={projectInvalid === index ? [LINE_SX, LINE_INVALID_SX] : LINE_SX}
            >
              <Box sx={LINE_PROJECT_SX}>
                <FormField label="Project">
                  <TextField
                    select
                    value={line.project_id}
                    onChange={e => editProject(index, { project_id: e.target.value })}
                    disabled={projects.length === 0 && !projectUnlisted}
                    slotProps={{ htmlInput: { 'data-testid': 'project-select' } }}
                    sx={fieldTapTargetSx}
                  >
                    {projectUnlisted && (
                      /* Disabled synthetic option (as the instance-type field): shows what it holds. */
                      <MenuItem value={line.project_id} disabled>
                        {ownProjectNames.get(line.project_id) ?? `project ${line.project_id}`}
                        <Box component="span" sx={{ ml: 1, color: 'text.disabled', fontSize: '0.75rem' }}>
                          — not among the projects you can see
                        </Box>
                      </MenuItem>
                    )}
                    {/* Every project is eligible: a project runs on a node through its assignment alone. */}
                    {projects.map(p => (
                      <MenuItem key={p.id} value={String(p.id)}>{p.name}</MenuItem>
                    ))}
                  </TextField>
                </FormField>
              </Box>

              <Button
                type="button"
                data-testid="project-remove"
                aria-label={`Remove project assignment ${index + 1}${project ? `, ${project.name}` : ''}`}
                onClick={() => removeProjectLine(index)}
                sx={LINE_REMOVE_SX}
              >
                <Box component="span" aria-hidden="true">✕</Box>
              </Button>
            </Box>
          )
        })}

        <LineListFooter
          error={projectError} errorTestId="node-group-project-error"
          addTestId="node-group-add-project" addDisabled={projects.length === 0} onAdd={addProjectLine}
        >
          + Add project
        </LineListFooter>
      </Box>

      {/* ── THE SWITCH THAT SPENDS MONEY ─────────────────────────────────────────────────────────── */}
      <Box
        data-testid="node-group-enabled-box"
        sx={draft.enabled ? [ENABLED_BOX_SX, ENABLED_ON_SX] : [ENABLED_BOX_SX, ENABLED_OFF_SX]}
      >
        <FormControlLabel
          // A REAL SWITCH, not the two-option select the defaults form uses for booleans: hand-spelled help
          // is the point here — this control starts billing — with its own tinted box and consequence line.
          sx={{ ml: 0, gap: 1 }}
          labelPlacement="end"
          control={
            <Switch
              checked={draft.enabled}
              onChange={e => setField('enabled', e.target.checked)}
              color="warning"
              /* The handle is on the Switch ROOT: MUI rejects `data-*` in `slotProps.input`
                 (`components/tasks/TaskToolbar.tsx` is the single home of the note). */
              data-testid="node-group-enabled"
            />
          }
          label={
            <Typography component="span" variant="body2" sx={{ fontWeight: 600 }}>
              {draft.enabled ? 'Enabled — starts instances' : 'Disabled'}
            </Typography>
          }
        />
        <FormHelperText sx={{ m: 0 }}>
          {draft.enabled
            ? 'EC2 instances launch on save, billed to the connection’s account.'
            : 'Nothing starts. Existing nodes keep running.'}
        </FormHelperText>
      </Box>
    </FormDialog>
  )
}

export default NodeGroupDialog
