import { useEffect, useMemo, useState } from 'react'
import Box from '@mui/material/Box'
import { getCredential, loadCredentialOptions, type Credential } from '../../pages/credentials/api'
import { useCredentialTypes } from '../../pages/credentials/useCredentialTypes'
import Banner from '../../ui/Banner'
import CredentialSelect, { type CredentialOption } from '../../ui/CredentialSelect'
import { DetailCell } from '../../ui/detail/DetailPage'
import FieldRow from '../../ui/FieldRow'
import FormField from '../../ui/FormField'
import FormTextField from '../../ui/FormTextField'
import { useOwnOrgRows } from '../../ui/OrgIndicator'
import { grid } from '../../theme'
import {
  fieldsOfType,
  PORT_MAX,
  PORT_MIN,
  toCredentialOption,
  type EnvVarSource,
  type ProjectEnvVar,
  type ProjectEnvVarWrite,
} from './projectEnvVars'

/* ONE ENVIRONMENT VARIABLE'S FIELDS, AND THE DRAFT THEY EDIT — the body of `pages/projects/ProjectEnvVarPage.tsx`,
 * kept apart from the page so the page holds only its frame (trail, chips, Save bar, leave guard) and this module the
 * form's own rules: the draft's TIER is converted to the wire's `user_id` at the edge (`bodyOf`) and nowhere else, and
 * what the form refuses itself (`localRefusal`) lands on the control it is about. The name grammar, the one-line rule,
 * duplicates and the tier are the server's: its sentence shows verbatim.
 *
 * Secret floor: the wire never carries a resolved value (credential NAME, TYPE LABEL and FIELD only), so there is
 * nothing to redact here. */

/** The form draft. `id` is `null` for a create; `scope` is the TIER, converted to the wire's `user_id` at
 *  the edge only. */
export type Draft = {
  id: string | null
  name: string
  scope: 'project' | 'user'
  source: EnvVarSource
  literal: string
  credentialId: string | null
  credentialField: string
  /** A port row's value: assigned per slot, or a preferred port (`portPreferred`, as typed). */
  portMode: 'auto' | 'preferred'
  portPreferred: string
  description: string
}

/* The new-row tier is a CONSTANT: seeding from `canEdit` read an unhydrated grant map (false) and stuck on
   "Only me". The tier is decided at the edge (`effectiveScope`), and view-only holders see no select. */
export const emptyDraft = (): Draft => ({
  id: null,
  name: '',
  scope: 'project',
  source: 'literal',
  literal: '',
  credentialId: null,
  credentialField: '',
  portMode: 'auto',
  portPreferred: '',
  description: '',
})

export const draftFromRow = (row: ProjectEnvVar): Draft => ({
  id: row.id,
  name: row.name,
  scope: row.scope,
  source: row.source,
  literal: row.literal ?? '',
  credentialId: row.credential?.id ?? null,
  credentialField: row.credential?.field ?? '',
  portMode: row.port_preferred === null ? 'auto' : 'preferred',
  portPreferred: row.port_preferred === null ? '' : String(row.port_preferred),
  description: row.description,
})

/** The tier this draft writes: a holder without `projects:edit` has one and sees no control for it. */
const effectiveScope = (d: Draft, canEdit: boolean): 'project' | 'user' => (canEdit ? d.scope : 'user')

/** The wire body for a draft. The TIER is converted here, at the edge, and nowhere else. */
export function bodyOf(d: Draft, canEdit: boolean, viewerId: string | null): ProjectEnvVarWrite {
  const credential = d.source === 'credential'
  const port = d.source === 'port'
  return {
    name: d.name.trim(),
    // A port row is always a project default (the server refuses a per-user one).
    user_id: port || effectiveScope(d, canEdit) === 'project' ? null : viewerId,
    source: d.source,
    literal: d.source === 'literal' ? d.literal : null,
    credential_id: credential ? d.credentialId : null,
    credential_field: credential ? d.credentialField : null,
    ...(port ? { port_preferred: d.portMode === 'preferred' ? Number(d.portPreferred) : null } : {}),
    description: d.description.trim(),
  }
}

/** A preferred port as typed: a whole number in `PORT_MIN..PORT_MAX`, else `null`. */
const parsePort = (text: string): number | null => {
  const n = Number(text.trim())
  return text.trim() !== '' && Number.isInteger(n) && n >= PORT_MIN && n <= PORT_MAX ? n : null
}

/** What the form refuses itself, and WHERE: on the control it is about, or the form for none. Name
 *  grammar, one-line rule, duplicate and tier are the server's (its sentence shows in the form error). */
export function localRefusal(
  d: Draft,
  canEdit: boolean,
  viewerId: string | null,
): { where: 'name' | 'port' | 'form'; message: string } | null {
  if (d.name.trim() === '') return { where: 'name', message: 'Enter a variable name.' }
  if (d.source === 'port' && d.portMode === 'preferred' && parsePort(d.portPreferred) === null) {
    return { where: 'port', message: `Enter a port from ${PORT_MIN} to ${PORT_MAX}.` }
  }
  if (d.source === 'literal' && d.literal === '') {
    return { where: 'form', message: 'Enter a value, or reference a credential.' }
  }
  if (d.source === 'credential' && d.credentialId === null) {
    return { where: 'form', message: 'Choose a credential.' }
  }
  if (d.source === 'credential' && d.credentialField === '') {
    return { where: 'form', message: 'Choose a credential field.' }
  }
  if (d.source !== 'port' && effectiveScope(d, canEdit) === 'user' && viewerId === null) {
    return { where: 'form', message: 'Your session is still loading — try again in a moment.' }
  }
  return null
}

/** The field picker, its own component so `useCredentialTypes` (`GET /api/credentials/types`) mounts only
 *  when a credential source is chosen. The field list is the server's registry: `kind: 'secret'` is the
 *  classification the server strips by. */
function CredentialFieldPicker({
  credential,
  value,
  onChange,
  readOnly,
}: {
  credential: Credential | null
  value: string
  onChange: (next: string) => void
  readOnly: boolean
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
  const hint = credential === null
    ? 'Choose a credential first.'
    : loading
      ? 'Loading fields…'
      : fields.length === 0
        ? 'This credential’s type declares no fields.'
        : undefined
  return (
    <FormTextField
      id="project-env-credential-field"
      label="Credential field"
      help={{
        tip: 'The credential field whose value the variable gets.',
        more: 'Resolved when a slot is claimed. A secret field\'s value never reaches this screen.',
      }}
      value={value}
      error={error || undefined}
      hint={hint}
      onChange={onChange}
      readOnly={readOnly}
      testId="project-env-credential-field"
      options={options.map(f => ({ value: f.key, label: `${f.label}${f.kind === 'secret' ? ' (secret)' : ''}` }))}
    >
      {/* Nothing picked yet: a native select would show its first field as if chosen. */}
      {value === '' && <option value="">Choose a field</option>}
    </FormTextField>
  )
}

/** The literal's secrets warning: its label's hover AND the input's description (a hover alone is sight-only). */
export const LITERAL_WARNING = 'Cleartext anyone here can read; put secrets on a credential.'

/** The variable's fields, for the create and edit page alike. `readOnly`: a row the viewer may not write (another
 *  member's override, a project default without `projects:edit`) shows its values and refuses the edit. */
export function EnvVarForm({
  draft,
  setDraft,
  canEdit,
  readOnly = false,
  nameError,
  portError = null,
  formError,
}: {
  draft: Draft
  setDraft: (next: Draft) => void
  canEdit: boolean
  readOnly?: boolean
  /** A refusal about the NAME, in the name field's own helper slot. */
  nameError: string | null
  /** A refusal about the preferred PORT, in that field's own helper slot. */
  portError?: string | null
  /** A form-level refusal, or one about a control with no helper slot — above the fields. */
  formError: string
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
  /* Port is a project default only: not offered on an Only me row (the server refuses it), nor to a holder whose
     one tier is Only me. A stored port row keeps its option, so its select never renders empty. */
  const offerPort = draft.source === 'port' || effectiveScope(draft, canEdit) === 'project'
  return (
    <Box data-testid="project-env-form" sx={{ minWidth: 0, pt: 0.5 }}>
      {/* Above the fields, where it is read before the Save that raised it. */}
      {formError && (
        <Box sx={{ mb: 1.5 }}>
          <Banner severity="error" testId="project-env-form-error">{formError}</Banner>
        </Box>
      )}
      <FieldRow align="start" columnGap={grid(1)} rowGap={1}>
        <DetailCell basis={grid(34.5)} max={grid(53)}>
          <FormTextField
            id="project-env-name"
            autoFocus={!editing && !readOnly}
            label="Variable name"
            help="Key in the slot’s .env."
            placeholder="e.g. API_BASE_URL"
            value={draft.name}
            error={nameError ?? undefined}
            onChange={name => setDraft({ ...draft, name })}
            readOnly={readOnly}
            testId="project-env-name-input"
            slotProps={{ htmlInput: { spellCheck: false } }}
          />
        </DetailCell>

        {/* The tier select, only for a `projects:edit` holder (view-only has one tier). */}
        {canEdit && (
          <DetailCell basis={grid(24)} max={grid(32)}>
            <FormTextField
              id="project-env-scope"
              label="Applies to"
              help={{
                tip: 'Only me overrides Everyone, in your slots only.',
                more: 'Can\'t be changed once saved.',
                docs: 'using-jaah/projects/add-an-environment-variable',
              }}
              /* An edit cannot move a row's tier (422): tiers are separately unique per name. */
              disabled={editing}
              readOnly={readOnly}
              value={draft.scope}
              onChange={scope => setDraft({
                ...draft,
                scope: scope as 'project' | 'user',
                // Only me cannot hold a port: the type falls back to a literal.
                source: scope === 'user' && draft.source === 'port' ? 'literal' : draft.source,
              })}
              testId="project-env-scope-input"
              options={[
                { value: 'project', label: 'Everyone', help: 'Written into every member\'s slots for this project.' },
                { value: 'user', label: 'Only me', help: 'Written into your slots only, overriding Everyone.' },
              ]}
            />
          </DetailCell>
        )}

        <DetailCell basis={grid(24)} max={grid(32)}>
          <FormTextField
            id="project-env-source"
            label="Value from"
            help="A credential keeps the value off this screen."
            value={draft.source}
            onChange={source => setDraft({ ...draft, source: source as EnvVarSource })}
            readOnly={readOnly}
            testId="project-env-source-input"
            options={[
              { value: 'literal', label: 'A literal', help: 'Typed here; stored and shown in cleartext.' },
              { value: 'credential', label: 'A credential', help: 'Read from a credential field when a slot is claimed.' },
              ...(offerPort
                ? [{ value: 'port', label: 'Port', help: 'A free port the node picks for each slot.' }]
                : []),
            ]}
          />
        </DetailCell>

        {draft.source === 'port' ? (
          <>
            <DetailCell basis={grid(29)} max={grid(42.5)}>
              <FormTextField
                id="project-env-port-mode"
                label="Port"
                help={{
                  tip: 'Two slots never share a port.',
                  more: 'A preferred port is used when it is free on the node and no other slot has it; else the next free one.',
                  docs: 'using-jaah/worktrees/give-a-service-a-fixed-port',
                }}
                value={draft.portMode}
                onChange={mode => setDraft({ ...draft, portMode: mode as 'auto' | 'preferred' })}
                readOnly={readOnly}
                testId="project-env-port-mode-input"
                options={[
                  { value: 'auto', label: 'Auto-assigned per slot' },
                  { value: 'preferred', label: 'Preferred port' },
                ]}
              />
            </DetailCell>
            {draft.portMode === 'preferred' && (
              <DetailCell basis={grid(18.5)} max={grid(26.5)}>
                <FormTextField
                  id="project-env-port-preferred"
                  label="Preferred port"
                  type="number"
                  placeholder="e.g. 5173"
                  value={draft.portPreferred}
                  error={portError ?? undefined}
                  onChange={portPreferred => setDraft({ ...draft, portPreferred })}
                  readOnly={readOnly}
                  testId="project-env-port-preferred-input"
                  slotProps={{ htmlInput: { min: PORT_MIN, max: PORT_MAX, step: 1 } }}
                />
              </DetailCell>
            )}
          </>
        ) : draft.source === 'literal' ? (
          <DetailCell basis={grid(34.5)} max={grid(58.5)}>
            <FormTextField
              id="project-env-literal"
              /* The one secrets warning, on the label of the field where the mistake would be made. */
              label="Value"
              help={LITERAL_WARNING}
              value={draft.literal}
              onChange={literal => setDraft({ ...draft, literal })}
              readOnly={readOnly}
              testId="project-env-literal-input"
              slotProps={{ htmlInput: { spellCheck: false } }}
            />
          </DetailCell>
        ) : (
          <>
            <DetailCell basis={grid(34.5)} max={grid(53)}>
              <FormField label="Credential" help="Value resolves from it when a slot is claimed.">
                <CredentialSelect
                  id="project-env-credential"
                  testId="project-env-credential"
                  value={draft.credentialId}
                  selected={selected}
                  disabled={readOnly}
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
            </DetailCell>
            <DetailCell basis={grid(29)} max={grid(42.5)}>
              <CredentialFieldPicker
                credential={chosen}
                value={draft.credentialField}
                onChange={next => setDraft({ ...draft, credentialField: next })}
                readOnly={readOnly}
              />
            </DetailCell>
          </>
        )}

        <DetailCell basis={grid(29)} max={grid(48)}>
          <FormTextField
            id="project-env-description"
            label="Description"
            help="Shown in the list; not written to .env."
            value={draft.description}
            onChange={description => setDraft({ ...draft, description })}
            readOnly={readOnly}
            testId="project-env-description-input"
          />
        </DetailCell>
      </FieldRow>
    </Box>
  )
}
