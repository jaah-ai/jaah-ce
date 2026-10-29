import { type JSX, type KeyboardEvent, type ReactNode, useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { alpha } from '@mui/material/styles'
import MenuItem from '@mui/material/MenuItem'
import Stack from '@mui/material/Stack'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { apiDetail, del, get, post, put } from '../../api'
import { useSubmit } from '../../hooks/useSubmit'
import { useConfirm } from '../ConfirmDialog'
import { deleteItem, editItem, RowActionsOverflow, type OverflowItem } from '../tasks/rowActions'
import { getCredential, loadCredentialOptions, type Credential } from '../../pages/credentials/api'
import { useCredentialTypes } from '../../pages/credentials/useCredentialTypes'
import Banner from '../../ui/Banner'
import { BreakAfter } from '../../ui/BreakAfter'
import { CopyButton } from '../../ui/CopyButton'
import DataTable from '../../ui/DataTable'
import CredentialSelect, { type CredentialOption } from '../../ui/CredentialSelect'
import EmptyState from '../../ui/EmptyState'
import FieldRow from '../../ui/FieldRow'
import FormField from '../../ui/FormField'
import HelpLabel from '../../ui/HelpLabel'
import FormDialog from '../../ui/FormDialog'
import {
  FULL_BLEED_SX,
  fieldTapTargetSx,
  monoSx,
  PHONE,
  PHONE_CARD_SX,
  ROW_ACTION_SX,
  SR_ONLY_SX,
  tapTargetSx,
} from '../../ui/listStyles'
import LoadingState from '../../ui/LoadingState'
import StatusChip from '../../ui/StatusChip'
import { useOwnOrgRows } from '../../ui/OrgIndicator'
import {
  envFileLines,
  fieldsOfType,
  scopeLabel,
  STATUS_OK,
  toCredentialOption,
  unresolvedReason,
  type ProjectEnvVar,
  type ProjectEnvVarWrite,
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
 * The catalogue is a `ui/DataTable` (one row per variable, restacked as a card when narrow); names and
 * values wrap rather than clip, so neither loses its tail.
 *
 * Secret floor: nothing on the wire can carry a resolved value (the serializer sends credential NAME,
 * TYPE LABEL and FIELD only; the preview never resolves), so there is nothing to redact.
 *
 * Renders inside the project form's `<form>`: the editor is a portalled `FormDialog`, which stops its own
 * submit before it bubbles (through the React tree) to the project form; every other button is
 * `type="button"` (else Delete would save the project), and `onEnterSubmit` blocks Enter.
 *
 * Height is a budget: `e2e/project-edit-tabs.spec.ts` measures every panel against the 900px scrollport
 * with no allowance, so the preview is BEHIND A TOGGLE and optional row lines render only when non-empty.
 *
 * The preview is the `.env` text this tab contributes (`envFileLines`): literals in clear, credentials
 * masked, an unresolved winner commented out in error tone.
 */

/** The catalogue for one project, keyed by project. */
const listKey = (projectId: number) => ['project-env-vars', projectId] as const
/** The preview is a second read (winners only, for one identity): its own key, fetched only while open. */
const previewKey = (projectId: number) => ['project-env-preview', projectId] as const

/* Enter commits the row, never submits the project form — see the file header. */
const onEnterSubmit = (submit: () => void) => (e: KeyboardEvent<HTMLDivElement>) => {
  if (e.key !== 'Enter') return
  e.preventDefault()
  submit()
}

/** A cell sized by its content's rem width — `ui/FieldRow`'s `flex-basis` wrap, stacking on phones. */
function Cell({ basis, max, children }: { basis: string; max?: string; children: ReactNode }) {
  return <Box sx={{ flex: `1 1 ${basis}`, minWidth: 0, maxWidth: max }}>{children}</Box>
}

/** One muted or toned line under a row's name; `whiteSpace: normal`, since a clipped or hovered reason
 *  goes unread (AC17 c). */
function RowNote({
  tone,
  testId,
  children,
}: {
  tone: 'muted' | 'warning' | 'error'
  testId: string
  children: ReactNode
}) {
  return (
    <Typography
      variant="caption"
      data-testid={testId}
      sx={{
        display: 'block',
        whiteSpace: 'normal',
        overflowWrap: 'anywhere',
        color:
          tone === 'error' ? 'error.main' : tone === 'warning' ? 'warning.main' : 'text.secondary',
      }}
    >
      {children}
    </Typography>
  )
}

/** THE NAME, never ellipsised: it breaks after an `_`, and `overflowWrap: 'anywhere'` is only the safety net
 *  for a name with no `_` wider than its cell. */
function VarName({ name }: { name: string }) {
  return (
    <Box
      component="span"
      data-testid="project-env-name"
      sx={theme => ({
        ...monoSx(theme),
        fontSize: '0.8125rem',
        fontWeight: 600,
        overflowWrap: 'anywhere',
      })}
    >
      {/* A break opportunity after each `_`: a long name wraps between its words, never inside one. */}
      <BreakAfter text={name} sep="_" />
    </Box>
  )
}

/** The value line. A LITERAL wraps (a clipped value goes unread); a CREDENTIAL reference
 *  names credential and field in words. */
function ValueLine({ row }: { row: ProjectEnvVar }) {
  if (row.source === 'literal') {
    return (
      <Typography
        variant="caption"
        data-testid="project-env-value"
        title={row.literal ?? undefined}
        sx={theme => ({ ...monoSx(theme), display: 'block', overflowWrap: 'anywhere', color: 'text.secondary' })}
      >
        {row.literal}
      </Typography>
    )
  }
  const credential = row.credential
  return (
    <Typography
      variant="caption"
      data-testid="project-env-value"
      sx={{ display: 'block', color: 'text.secondary', overflowWrap: 'anywhere' }}
    >
      {'credential '}
      <Box component="span" sx={{ fontWeight: 600 }} data-testid="project-env-credential-name">
        {/* A tombstone: the delete nulls the reference; the field survives so a replacement is clear. */}
        {credential?.name ?? '(deleted)'}
      </Box>
      {', field '}
      <Box
        component="span"
        data-testid="project-env-credential-field"
        sx={theme => ({ ...monoSx(theme), fontWeight: 600 })}
      >
        {credential?.field}
      </Box>
    </Typography>
  )
}

/* THE CARD LAYOUT, keyed on the TABLE'S OWN WIDTH, not the viewport's: the project editor opens in a
 * ~550px drawer even on a desktop, where five columns clipped Description behind a scroll. Below
 * `ENV_CARD_WIDTH` each row restacks as `listStyles.PHONE_CARD_SX`'s card (its rules, re-keyed): the name
 * leads with the ⋯ beside it, value and description each take a full line, the two pills flow between.
 * `order`, not DOM moves, keeps reading order. Selected by `data-col`. */
const ENV_CONTAINER = 'env-table'
const ENV_CARD_WIDTH = `@container ${ENV_CONTAINER} (max-width:719.95px)`
const ENV_TABLE_SX = {
  containerType: 'inline-size',
  containerName: ENV_CONTAINER,
  [ENV_CARD_WIDTH]: {
    ...PHONE_CARD_SX[PHONE],
    '& tbody tr': { ...PHONE_CARD_SX[PHONE]['& tbody tr'], py: 0.75, rowGap: 0.5 },
    '& tbody td[data-col="name"]': { order: 0, flex: '1 1 0', display: 'block' },
    '& tbody td[data-col="actions"]': { order: 1, flex: '0 0 auto', width: 'auto' },
    '& tbody td[data-col="value"]': { order: 2, flex: '1 1 100%', display: 'block', maxWidth: '100%' },
    '& tbody td[data-col="source"]': { order: 3, flex: '0 0 auto' },
    '& tbody td[data-col="scope"]': { order: 4, flex: '0 1 auto', flexWrap: 'wrap' },
    '& tbody td[data-col="description"]': { order: 5, flex: '1 1 100%' },
    '& tbody td[data-col="description"]:not(:has(*:not([data-cell-label])))': { display: 'none' },
    /* `thead` is hidden on the card, so the heading rides in the cell, announced but not shown. */
    '& [data-cell-label]': { ...SR_ONLY_SX, display: 'inline' },
  },
} as const

/** A column heading for the card, where `thead` is hidden: absent from the table (its `<th>` names the
 *  cell), screen-reader-only on the card. Needs a positioned ancestor (`SR_ONLY_SX`): the cell. */
function CardLabel({ children }: { children: string }) {
  return <Box component="span" data-cell-label sx={{ display: 'none' }}>{children}: </Box>
}

/** Head cells in column order; the actions column is named for screen readers only. */
const COLUMNS: readonly { key: string; label: string }[] = [
  { key: 'name', label: 'Name' },
  { key: 'value', label: 'Value' },
  { key: 'source', label: 'Source' },
  { key: 'scope', label: 'Applies to' },
  { key: 'description', label: 'Description' },
]

/** One catalogue row. Status notes ride under the VALUE (it is the value they qualify); the owner rides
 *  under the scope pill (it says whose tier). */
function EnvVarRow({
  row,
  viewerId,
  ownerLabel,
  onEdit,
  onDelete,
  busy,
}: {
  row: ProjectEnvVar
  viewerId: string | null
  ownerLabel: string | null
  onEdit?: () => void
  onDelete?: () => void
  busy: boolean
}): JSX.Element {
  const viewerOwnsRow = viewerId !== null && row.user_id === viewerId
  const reason = unresolvedReason(row, viewerOwnsRow)
  const fromCredential = row.source === 'credential'
  /* Named for the row (not bare `Edit`/`Delete`). An empty list renders nothing (`RowActionsOverflow`). */
  const actions: OverflowItem[] = [
    ...(onEdit ? [editItem(`environment variable ${row.name}`, onEdit)] : []),
    ...(onDelete ? [deleteItem(`environment variable ${row.name}`, onDelete)] : []),
  ]
  return (
    <TableRow
      data-testid="project-env-row"
      data-name={row.name}
      data-status={row.status}
      data-scope={row.scope}
      data-winning={row.winning ? 'true' : 'false'}
      sx={{ verticalAlign: 'top' }}
    >
      <TableCell data-col="name" sx={{ minWidth: '9rem' }}>
        <VarName name={row.name} />
        {/* The winner's ✓ only on a row that wins AND resolves (a winner supplying nothing is the row
            whose warning matters most). */}
        {row.winning && row.status === STATUS_OK && (
          <Box component="span" data-testid="project-env-applies" sx={{ position: 'relative', ml: 0.5 }}>
            {/* The glyph is decoration; the sentence is the fact, reachable by screen readers. */}
            <Box
              component="span"
              aria-hidden="true"
              title="The value your slot gets."
              sx={{ color: 'success.main', fontSize: '0.75rem' }}
            >
              ✓
            </Box>
            <Box component="span" sx={SR_ONLY_SX}>The value your slot gets.</Box>
          </Box>
        )}
      </TableCell>

      <TableCell data-col="value" sx={{ minWidth: '15rem', maxWidth: '24rem', position: 'relative' }}>
        <CardLabel>Value</CardLabel>
        <ValueLine row={row} />
        {/* A project default that is not the winner. Reports `winning` and names no cause ("your override
            wins" would be a precedence inference); the winning row is adjacent and carries the ✓. */}
        {row.scope === 'project' && !row.winning && (
          <RowNote tone="muted" testId="project-env-shadowed">
            Not the value your slot gets.
          </RowNote>
        )}
        {/* The reason, inline: three causes, three remedies; phones have no hover. */}
        {reason !== null && (
          <RowNote tone="error" testId="project-env-reason">
            ⚠ {reason}
          </RowNote>
        )}
        {/* What writes this name instead, for a node-owned name: the server's per-project sentence naming a
            MECHANISM. */}
        {row.warning !== null && (
          <RowNote tone="warning" testId="project-env-warning">
            ⚑ {row.warning}
          </RowNote>
        )}
      </TableCell>

      <TableCell data-col="source">
        {/* The app's `● / ○`: filled = from a stored credential, hollow = inline. Wrapped (not
            `labelTestId`): `ui/StatusChip` emits its label hook only with a GLYPH. */}
        <Box component="span" data-testid="project-env-source" sx={{ display: 'inline-flex' }}>
          <StatusChip
            tone={fromCredential ? 'success' : 'neutral'}
            glyph={fromCredential ? '●' : '○'}
            label={fromCredential ? 'Credential' : 'Literal'}
            data-status={row.source}
          />
        </Box>
      </TableCell>

      <TableCell data-col="scope">
        <Box component="span" data-testid="project-env-scope" sx={{ display: 'inline-flex' }}>
          <StatusChip
            tone={row.scope === 'project' ? 'info' : 'neutral'}
            label={scopeLabel(row, viewerId)}
            data-status={row.scope}
          />
        </Box>
        {/* WHOSE override (the pill says the tier, this the person), when the member list can name them. */}
        {row.scope === 'user' && !viewerOwnsRow && ownerLabel !== null && (
          <RowNote tone="muted" testId="project-env-owner">
            Set by {ownerLabel}
          </RowNote>
        )}
      </TableCell>

      <TableCell data-col="description" sx={{ minWidth: '12rem', position: 'relative' }}>
        <CardLabel>Description</CardLabel>
        {row.description !== '' && (
          <RowNote tone="muted" testId="project-env-description">
            {row.description}
          </RowNote>
        )}
      </TableCell>

      <TableCell data-col="actions" align="right" sx={{ width: 44, px: 0.5, py: 0 }}>
        <RowActionsOverflow name={row.name} busy={busy} items={actions} />
      </TableCell>
    </TableRow>
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

/** The form draft. `id` is `null` for a create; `scope` is the TIER, converted to the wire's `user_id` at
 *  the edge only. */
type Draft = {
  id: string | null
  name: string
  scope: 'project' | 'user'
  source: 'literal' | 'credential'
  literal: string
  credentialId: string | null
  credentialField: string
  description: string
}

/* The new-row tier is a CONSTANT: seeding from `canEdit` read an unhydrated grant map (false) and stuck on
   "Only me". The tier is decided at the edge (`effectiveScope`), and view-only holders see no select. */
const emptyDraft = (): Draft => ({
  id: null,
  name: '',
  scope: 'project',
  source: 'literal',
  literal: '',
  credentialId: null,
  credentialField: '',
  description: '',
})

const draftFromRow = (row: ProjectEnvVar): Draft => ({
  id: row.id,
  name: row.name,
  scope: row.scope,
  source: row.source,
  literal: row.literal ?? '',
  credentialId: row.credential?.id ?? null,
  credentialField: row.credential?.field ?? '',
  description: row.description,
})

/** The field picker, its own component so `useCredentialTypes` (`GET /api/credentials/types`) mounts only
 *  when a credential source is chosen (every panel stays mounted). The field list is the server's
 *  registry: `kind: 'secret'` is the classification the server strips by. */
function CredentialFieldPicker({
  credential,
  value,
  onChange,
}: {
  credential: Credential | null
  value: string
  onChange: (next: string) => void
}) {
  const { types, loading, error } = useCredentialTypes()
  const fields = useMemo(
    () => fieldsOfType(types, credential?.type ?? null),
    [types, credential?.type],
  )
  /* The stored field is ALWAYS an option: a select whose value is not among its options renders EMPTY and
     would save the row with its field cleared. An unknown value is offered under its raw key. */
  const options = fields.some(f => f.key === value) || value === ''
    ? fields
    : [...fields, { key: value, label: value, kind: 'text' }]
  const helper = error
    ? error
    : credential === null
      ? 'Choose a credential first.'
      : loading
        ? 'Loading fields…'
        : fields.length === 0
          ? 'This credential’s type declares no fields.'
          : undefined
  return (
    <TextField
      id="project-env-credential-field"
      select
      label={<HelpLabel label="Credential field" help="Its value is written; secret fields never reach this screen." />}
      size="small"
      fullWidth
      value={value}
      error={Boolean(error)}
      helperText={helper}
      onChange={e => onChange(e.target.value)}
      slotProps={{ select: { native: false }, htmlInput: { 'data-testid': 'project-env-credential-field' } }}
      sx={fieldTapTargetSx}
    >
      {options.map(f => (
        <MenuItem key={f.key} value={f.key}>
          {f.label}
          {f.kind === 'secret' ? ' (secret)' : ''}
        </MenuItem>
      ))}
    </TextField>
  )
}

/** The literal's secrets warning: its label's hover AND the input's description (a hover alone is sight-only). */
const LITERAL_WARNING = 'Cleartext anyone here can read; put secrets on a credential.'

/** Shared fields for the Add and Edit dialogs. */
function EnvVarForm({
  draft,
  setDraft,
  canEdit,
  nameError,
  formError,
  onSubmit,
}: {
  draft: Draft
  setDraft: (next: Draft) => void
  canEdit: boolean
  /** A refusal about the NAME, in the name field's own helper slot. */
  nameError: string | null
  /** A form-level refusal, or one about a control with no helper slot — shown beside the submit. */
  formError: string
  onSubmit: () => void
}) {
  /* Every credential this form has seen, by id: the picker's roster rows AND the one an edit was handed
     (which arrives as an id only; the field list depends on its TYPE). */
  const [known, setKnown] = useState<Record<string, Credential>>({})
  const chosen = draft.credentialId === null ? null : known[draft.credentialId] ?? null
  const ownOrgRows = useOwnOrgRows()
  /* Returns `prev` UNCHANGED when nothing new was learned: the effect below keys on `known`, and a fresh
     object per roster fetch re-sent a refused lookup on every keystroke. */
  const remember = (rows: Credential[]) =>
    setKnown(prev => {
      const fresh = rows.filter(r => prev[r.id] === undefined)
      if (fresh.length === 0) return prev
      const next = { ...prev }
      for (const r of fresh) next[r.id] = r
      return next
    })
  /** Ids the by-id lookup has already answered for, refusals included — so a refusal is asked once. */
  const [asked] = useState<Set<string>>(() => new Set())
  /* One read, only for an unnameable id. Failure is swallowed: `ui/CredentialSelect` still renders the id
     and the stored field stays, so the row stays saveable (fail-safe for a labels-only lookup). */
  useEffect(() => {
    const id = draft.credentialId
    if (id === null || known[id] !== undefined || asked.has(id)) return
    asked.add(id)
    let live = true
    getCredential(id)
      .then(row => { if (live) remember([row]) })
      .catch(() => {})
    return () => { live = false }
  }, [draft.credentialId, known, asked])
  /* Memoised: `ui/CredentialSelect` watches this prop BY IDENTITY. */
  const selected: CredentialOption | null = useMemo(
    () => (chosen !== null ? toCredentialOption(chosen) : null),
    [chosen],
  )
  const editing = draft.id !== null
  return (
    <Box data-testid="project-env-form" sx={{ minWidth: 0, pt: 0.5 }}>
      {/* Above the fields: below the row it landed under the sticky action bar at 448px (measured). */}
      {formError && (
        <RowNote tone="error" testId="project-env-form-error">
          {formError}
        </RowNote>
      )}
      <FieldRow align="start" columnGap="0.5rem" rowGap="0.5rem">
        <Cell basis="13rem" max="20rem">
          <TextField
            id="project-env-name"
            autoFocus
            label={<HelpLabel label="Variable name" help="Key in the slot’s .env." />}
            placeholder="e.g. API_BASE_URL"
            size="small"
            fullWidth
            value={draft.name}
            error={Boolean(nameError)}
            helperText={nameError ?? undefined}
            onChange={e => setDraft({ ...draft, name: e.target.value })}
            onKeyDown={onEnterSubmit(onSubmit)}
            slotProps={{ htmlInput: { 'data-testid': 'project-env-name-input', spellCheck: false } }}
            sx={fieldTapTargetSx}
          />
        </Cell>

        {/* The tier select, only for a `projects:edit` holder (view-only has one tier). */}
        {canEdit && (
          <Cell basis="9rem" max="12rem">
            <TextField
              id="project-env-scope"
              select
              label={<HelpLabel label="Applies to" help="Only me overrides Everyone, in your slots only." />}
              size="small"
              fullWidth
              /* An edit cannot move a row's tier (422): tiers are separately unique per name. */
              disabled={editing}
              value={draft.scope}
              onChange={e => setDraft({ ...draft, scope: e.target.value as 'project' | 'user' })}
              slotProps={{ htmlInput: { 'data-testid': 'project-env-scope-input' } }}
              sx={fieldTapTargetSx}
            >
              <MenuItem value="project">Everyone</MenuItem>
              <MenuItem value="user">Only me</MenuItem>
            </TextField>
          </Cell>
        )}

        <Cell basis="9rem" max="12rem">
          <TextField
            id="project-env-source"
            select
            label={<HelpLabel label="Value from" help="A credential keeps the value off this screen." />}
            size="small"
            fullWidth
            value={draft.source}
            onChange={e =>
              setDraft({ ...draft, source: e.target.value as 'literal' | 'credential' })
            }
            slotProps={{ htmlInput: { 'data-testid': 'project-env-source-input' } }}
            sx={fieldTapTargetSx}
          >
            <MenuItem value="literal">A literal</MenuItem>
            <MenuItem value="credential">A credential</MenuItem>
          </TextField>
        </Cell>

        {draft.source === 'literal' ? (
          <Cell basis="13rem" max="22rem">
            <TextField
              id="project-env-literal"
              /* The one secrets warning, on the label of the field where the mistake would be made. */
              label={<HelpLabel label="Value" help={LITERAL_WARNING} />}
              size="small"
              fullWidth
              value={draft.literal}
              onChange={e => setDraft({ ...draft, literal: e.target.value })}
              onKeyDown={onEnterSubmit(onSubmit)}
              slotProps={{
                htmlInput: { 'data-testid': 'project-env-literal-input', spellCheck: false, 'aria-description': LITERAL_WARNING },
              }}
              sx={fieldTapTargetSx}
            />
          </Cell>
        ) : (
          <>
            <Cell basis="13rem" max="20rem">
              <FormField label="Credential" help="Value resolves from it when a slot is claimed.">
                <CredentialSelect
                  id="project-env-credential"
                  testId="project-env-credential"
                  value={draft.credentialId}
                  selected={selected}
                  /* Changing the credential CLEARS THE FIELD: a field key belongs to a TYPE, and the write
                     route never checks the field exists on the credential (a kept `secret_access_key` came
                     back `field_absent`). */
                  onChange={next =>
                    setDraft({
                      ...draft,
                      credentialId: next,
                      credentialField: next === draft.credentialId ? draft.credentialField : '',
                    })
                  }
                  /* The roster is grant-scoped server-side (`GET /api/credentials`), and the write route 422s
                     an unresolvable reference — why the picker can be offered to view-only holders. */
                  fetchOptions={(query, signal) =>
                    loadCredentialOptions(query, signal, toCredentialOption, {
                      keep: rows => {
                        // Recorded on the way past, so a pick needs no second request for its TYPE.
                        remember(rows)
                        // A system credential never reaches a runner (the write route 422s it): not offered. Nor
                        // another org's, which an operator sees but may not attach.
                        return ownOrgRows(rows).filter(item => item.ownership !== 'system')
                      },
                    })}
                />
              </FormField>
            </Cell>
            <Cell basis="11rem" max="16rem">
              <CredentialFieldPicker
                credential={chosen}
                value={draft.credentialField}
                onChange={next => setDraft({ ...draft, credentialField: next })}
              />
            </Cell>
          </>
        )}

        <Cell basis="11rem" max="18rem">
          <TextField
            id="project-env-description"
            label={<HelpLabel label="Description" help="Shown in the list; not written to .env." />}
            size="small"
            fullWidth
            value={draft.description}
            onChange={e => setDraft({ ...draft, description: e.target.value })}
            onKeyDown={onEnterSubmit(onSubmit)}
            slotProps={{ htmlInput: { 'data-testid': 'project-env-description-input' } }}
            sx={fieldTapTargetSx}
          />
        </Cell>
      </FieldRow>
    </Box>
  )
}

const DELETE_FALLBACK = 'Could not delete the environment variable.'

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
  const [confirm, confirmDialog] = useConfirm()

  const varsQuery = useQuery({
    queryKey: listKey(projectId),
    queryFn: () => get<ProjectEnvVar[]>(`/api/projects/${projectId}/env-vars`),
  })

  /* The draft seed (see `emptyDraft`). */
  const [draft, setDraft] = useState<Draft>(() => emptyDraft())
  const [nameError, setNameError] = useState<string | null>(null)
  /** `formError`: a refusal about the form as a whole, or about a control with no helper slot of its own. */
  const { busy, formError, setFormError, run } = useSubmit()
  const [actionError, setActionError] = useState<string | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [previewOpen, setPreviewOpen] = useState(false)

  const previewQuery = useQuery({
    queryKey: previewKey(projectId),
    queryFn: () => get<ProjectEnvVar[]>(`/api/projects/${projectId}/env-vars/preview`),
    // Fetched only while the section is open.
    enabled: previewOpen,
  })

  const rows = varsQuery.data ?? []
  /* Data wins over `isError`: react-query persists this key, so a reload while the API is down has both. */
  const listState = varsQuery.data ? 'ready' : varsQuery.isError ? 'error' : 'loading'

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: listKey(projectId) })
    await queryClient.invalidateQueries({ queryKey: previewKey(projectId) })
  }

  /** The tier this draft writes: a holder without `projects:edit` has one and sees no control for it. */
  const effectiveScope = (d: Draft): 'project' | 'user' => (canEdit ? d.scope : 'user')

  /** The wire body for the current draft. The TIER is converted here, at the edge, and nowhere else. */
  function bodyOf(d: Draft): ProjectEnvVarWrite {
    const credential = d.source === 'credential'
    return {
      name: d.name.trim(),
      user_id: effectiveScope(d) === 'project' ? null : viewerId,
      source: d.source,
      literal: credential ? null : d.literal,
      credential_id: credential ? d.credentialId : null,
      credential_field: credential ? d.credentialField : null,
      description: d.description.trim(),
    }
  }

  /** What the form refuses itself, and WHERE: on the control it is about, or the form for none. Name
   *  grammar, one-line rule, duplicate and tier are the server's (its sentence shows in the banner). */
  function localRefusal(d: Draft): { where: 'name' | 'form'; message: string } | null {
    if (d.name.trim() === '') return { where: 'name', message: 'Enter a variable name.' }
    if (d.source === 'literal' && d.literal === '') {
      return { where: 'form', message: 'Enter a value, or reference a credential.' }
    }
    if (d.source === 'credential' && d.credentialId === null) {
      return { where: 'form', message: 'Choose a credential.' }
    }
    if (d.source === 'credential' && d.credentialField === '') {
      return { where: 'form', message: 'Choose a credential field.' }
    }
    if (effectiveScope(d) === 'user' && viewerId === null) {
      return { where: 'form', message: 'Your session is still loading — try again in a moment.' }
    }
    return null
  }

  async function handleSubmit(): Promise<void> {
    if (busy) return
    const refusal = localRefusal(draft)
    if (refusal !== null) {
      setNameError(refusal.where === 'name' ? refusal.message : null)
      setFormError(refusal.where === 'form' ? refusal.message : '')
      return
    }
    setNameError(null)
    setActionError(null)
    /* The server's sentence verbatim: 403 tier, 409 duplicate, 422 grammar/credential — different remedies. */
    await run('Could not save the environment variable.', async () => {
      const body = bodyOf(draft)
      if (draft.id === null) {
        await post<ProjectEnvVar>(`/api/projects/${projectId}/env-vars`, body)
      } else {
        await put<ProjectEnvVar>(`/api/projects/${projectId}/env-vars/${draft.id}`, body)
      }
      // The draft clears only on SUCCESS, so a refused write keeps what was typed.
      setDraft(emptyDraft())
      setDialogOpen(false)
      await invalidate()
    })
  }

  async function handleDelete(row: ProjectEnvVar): Promise<void> {
    if (busy) return
    setActionError(null)
    const ok = await confirm({
      title: 'Delete environment variable',
      identity: row.name,
      message:
        'Delete this variable? Slots claimed after this stop receiving it. This cannot be undone.',
      confirmLabel: 'Delete',
      destructive: true,
    })
    if (!ok) return
    await run(DELETE_FALLBACK, async () => {
      try {
        await del(`/api/projects/${projectId}/env-vars/${row.id}`)
        if (draft.id === row.id) {
          setDraft(emptyDraft())
          setDialogOpen(false)
        }
        await invalidate()
      } catch (err) {
        // From the dialog, the refusal must show inside it (`run` puts it in `formError`): the page banner
        // sits behind the modal. From the page, it goes to the banner instead.
        if (dialogOpen) throw err
        setActionError(apiDetail(err, DELETE_FALLBACK))
      }
    })
  }

  /** May the viewer write THIS row (to avoid offering a control that 403s)? Defaults need
   *  `projects:edit`; an override belongs to its user — `projects:edit` does not lift that (D20). */
  const mayWrite = (row: ProjectEnvVar): boolean =>
    row.user_id === null ? canEdit : viewerId !== null && row.user_id === viewerId
  /* The row the dialog edits, for its Delete; only a row the viewer may write ever opens it. */
  const editingRow = draft.id === null ? undefined : rows.find(r => r.id === draft.id)

  return (
    <Stack data-testid="project-environment" spacing={1} sx={{ pt: 1.5, minWidth: 0 }}>
      <Box>
        <Typography id="project-environment-heading" variant="subtitle2" component="h3">
          Environment
        </Typography>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          Variables written into a slot’s <code>.env</code> when claimed.
        </Typography>
      </Box>

      {actionError && (
        <Banner severity="error" testId="project-environment-error" onDismiss={() => setActionError(null)}>
          {actionError}
        </Banner>
      )}

      {listState === 'loading' && <LoadingState label="Loading environment variables…" rows={2} />}
      {listState === 'error' && (
        <Banner severity="error" testId="project-environment-load-error">
          {apiDetail(varsQuery.error, 'Could not load environment variables.')}
        </Banner>
      )}

      {listState === 'ready' && rows.length === 0 && (
        <EmptyState title="No environment variables" />
      )}

      {listState === 'ready' && rows.length > 0 && (
        /* An array, never a spread: a spread of two `sx` objects drops a shared key. */
        <Box sx={[FULL_BLEED_SX, ENV_TABLE_SX]}>
          <DataTable ariaLabel="Environment variables" testId="project-environment-list">
            <TableHead sx={{ '& th': { whiteSpace: 'nowrap' } }}>
              <TableRow>
                {COLUMNS.map(column => <TableCell key={column.key}>{column.label}</TableCell>)}
                <TableCell sx={{ position: 'relative' }}>
                  <Box component="span" sx={SR_ONLY_SX}>Actions</Box>
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {rows.map(row => (
                <EnvVarRow
                  key={row.id}
                  row={row}
                  viewerId={viewerId}
                  ownerLabel={row.user_id === null ? null : overrideOwners[row.user_id] ?? null}
                  busy={busy}
                  onEdit={mayWrite(row) ? () => {
                    setDraft(draftFromRow(row))
                    setNameError(null)
                    setFormError('')
                    setActionError(null)
                    setDialogOpen(true)
                  } : undefined}
                  onDelete={mayWrite(row) ? () => void handleDelete(row) : undefined}
                />
              ))}
            </TableBody>
          </DataTable>
        </Box>
      )}

      <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Button type="button" variant="contained" data-testid="project-env-add" disabled={busy}
          onClick={() => {
            setDraft(emptyDraft())
            setNameError(null)
            setFormError('')
            setActionError(null)
            setDialogOpen(true)
          }}>
          Add
        </Button>
      </Box>
      <FormDialog open={dialogOpen} onClose={() => setDialogOpen(false)} onSubmit={() => void handleSubmit()}
        title={draft.id === null ? 'Add environment variable' : 'Edit environment variable'}
        submitLabel="Save" busyLabel="Save" busy={busy} hideCancel testId="project-env-dialog"
        submitTestId="project-env-submit" secondaryAction={<>
          {editingRow && (
            <Button type="button" data-testid="project-env-dialog-delete" color="error" disabled={busy}
              onClick={() => void handleDelete(editingRow)} sx={{ mr: 'auto' }}>Delete</Button>
          )}
          <Button type="button" data-testid="project-env-cancel" color="inherit" disabled={busy}
            onClick={() => setDialogOpen(false)} sx={tapTargetSx}>Cancel</Button>
        </>}>
        <EnvVarForm draft={draft} setDraft={setDraft} canEdit={canEdit}
          nameError={nameError} formError={formError} onSubmit={() => void handleSubmit()} />
      </FormDialog>

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
