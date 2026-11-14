import { useEffect, useMemo, useState, type JSX } from 'react'
import Autocomplete from '@mui/material/Autocomplete'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { useApiRead } from '../../hooks/useApiRead'
import { listAccounts } from '../../pages/credentials/api'
import { listProjects } from '../../hooks/useRosters'
import { useAuth } from '../../auth'
import { getOrgsByIds, useOrgSearch } from '../../lib/orgs'
import { SCOPE_TYPES, SCOPE_TYPE_LABELS, type ScopeType } from './api'
import { NO_AUTOFILL_INPUT } from '../../ui/noAutofill'
import { fieldTapTargetSx } from '../../ui/listStyles'
import HelpLabel from '../../ui/HelpLabel'
import FormField from '../../ui/FormField'
import SelectField from '../../ui/SelectField'

// Pick one scope (org / project / account) by name: a type select, then a searchable list of
// that type's rows. The rows are the reader's own lists (`GET /api/projects`, `/accounts`;
// orgs are `GET /api/orgs` for an operator, the caller's own org otherwise), so nothing is offered that
// the reader could not already see.
// THE ORG ROSTER IS PAGED, so an operator's orgs are never listed whole: narrowed to one org, that
// org is read by id; unnarrowed, `useScopeOptions` carries no org rows and the picker SEARCHES the server
// as the operator types.

/** The scope list's explanation: its hover and its input's description. */
const SCOPE_TIP = 'The grant applies here and to everything inside it.'

export type ScopeOption = {
  type: ScopeType
  id: string
  name: string
  /** A second line: a project's org, an account's slug. */
  detail?: string
  /** The org the row belongs to, when the list says (orgs, projects). */
  orgId?: string
}

export { SCOPE_TYPE_LABELS }

/** Every scope the reader may name, narrowed to `orgId` where a row's org is known. One read per type; a
 *  failed read costs only that type (a reader without `accounts:view` still picks projects). An operator's
 *  org rows: the one org `orgId` names, or none unnarrowed (`ScopePicker` searches them). */
export function useScopeOptions(orgId?: string | null): { options: ScopeOption[]; loading: boolean } {
  const { admin } = useAuth()
  const isOperator = admin?.is_operator === true
  const ownOrg = admin?.org ?? null
  // Never rejects (each read is soft), so the fallback is never shown.
  const { data: lists } = useApiRead(['scope-picker-options', isOperator, orgId ?? null], async () => {
    const soft = <T,>(p: Promise<T[]>) => p.catch(() => [] as T[])
    const [orgs, projects, accounts] = await Promise.all([
      isOperator ? soft(orgId ? getOrgsByIds([orgId]) : Promise.resolve([])) : Promise.resolve(null),
      soft(listProjects()),
      soft(listAccounts()),
    ])
    return { orgs, projects, accounts }
  }, 'Could not load the scopes.')

  const options = useMemo(() => {
    if (!lists) return []
    const orgs: { id: string; name: string }[] = lists.orgs ?? (ownOrg ? [ownOrg] : [])
    const inOrg = (id: string | undefined) => !orgId || !id || id === orgId
    const out: ScopeOption[] = []
    for (const o of orgs) if (inOrg(o.id)) out.push({ type: 'org', id: o.id, name: o.name, orgId: o.id })
    for (const p of lists.projects) {
      const oid = p.org?.id
      if (!inOrg(oid)) continue
      out.push({
        type: 'project', id: String(p.id), name: p.name, orgId: oid,
        detail: isOperator && oid ? p.org?.name : undefined,
      })
    }
    for (const a of lists.accounts) out.push({ type: 'account', id: a.id, name: a.name || a.slug, detail: a.slug })
    return out
  }, [lists, ownOrg, orgId, isOperator])

  return { options, loading: lists === null }
}

export function ScopePicker({
  options,
  loading,
  value,
  onChange,
  types = SCOPE_TYPES,
  label,
  noOptionsText,
  idPrefix,
  disabled = false,
}: {
  options: ScopeOption[]
  loading: boolean
  value: ScopeOption | null
  onChange: (next: ScopeOption | null) => void
  /** Which scope types are offered. Exactly one: no Type select, the list's label names it. */
  types?: readonly ScopeType[]
  /** The list's label; the type's name by default. A repeated row numbers it (`Project 2`). */
  label?: string
  /** The empty list's line; by default the reader can see none of this type. */
  noOptionsText?: string
  idPrefix: string
  disabled?: boolean
}): JSX.Element {
  const { admin } = useAuth()
  const [type, setType] = useState<ScopeType>(value?.type ?? types[0])
  const listed = useMemo(() => options.filter(o => o.type === type), [options, type])
  /* An operator's unnarrowed orgs are searched, not listed (`useScopeOptions`); a picked name echoed back into
     the box is not a search. */
  const searching = type === 'org' && admin?.is_operator === true && !loading && listed.length === 0
  const { query: orgSearch, type: search } = useOrgSearch({ enabled: searching })
  const ofType = useMemo<ScopeOption[]>(() => {
    if (!searching) return listed
    const found = (orgSearch.data?.items ?? []).map(o => ({ type: 'org' as const, id: o.id, name: o.name, detail: o.slug, orgId: o.id }))
    // The picked org stays an option after a search moves past it, or MUI reads the value as unmatched.
    return value && value.type === 'org' && !found.some(o => o.id === value.id) ? [value, ...found] : found
  }, [searching, listed, orgSearch.data, value])
  const busy = loading || (searching && orgSearch.isFetching)
  // An org reader with exactly one org to name: pick it, so "Organization" needs no second step.
  useEffect(() => {
    if (type === 'org' && !searching && ofType.length === 1 && !value) onChange(ofType[0])
  }, [type, searching, ofType, value, onChange])

  return (
    <Stack spacing={2} direction={{ xs: 'column', sm: 'row' }}>
      {/* `width: auto` beside the scope list: the form row's `fullWidth` would crowd it out. */}
      {types.length > 1 && (
        <FormField label="Type" help="Narrows the next list to scopes of this kind.">
          <SelectField
            id={`${idPrefix}-scope-type`}
            value={type}
            onChange={v => {
              setType(v as ScopeType)
              onChange(null)
            }}
            sx={{ minWidth: { sm: 160 }, width: { sm: 'auto' } }}
            disabled={disabled}
            options={types.map(t => ({ value: t, label: SCOPE_TYPE_LABELS[t] }))}
          />
        </FormField>
      )}
      <Autocomplete
        id={`${idPrefix}-scope`}
        size="small"
        sx={{ flex: 1, minWidth: 0 }}
        options={ofType}
        disabled={disabled}
        value={value && value.type === type ? value : null}
        onChange={(_, next) => onChange(next)}
        onInputChange={(_, next, reason) => {
          if (reason === 'input' && searching) search(next)
        }}
        // A searched list is the server's answer (name or slug); re-filtering on the label would drop a slug hit.
        filterOptions={searching ? list => list : undefined}
        loading={busy}
        isOptionEqualToValue={(a, b) => a.type === b.type && a.id === b.id}
        getOptionLabel={o => o.name}
        getOptionKey={o => `${o.type}:${o.id}`}
        noOptionsText={busy ? 'Loading…' : orgSearch.isError && searching ? 'Could not search the organizations' : noOptionsText ?? `No ${SCOPE_TYPE_LABELS[type].toLowerCase()}s you can see`}
        renderOption={({ key, ...props }, o) => (
          <Box component="li" key={key} {...props}>
            <Box sx={{ minWidth: 0 }}>
              <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>{o.name}</Typography>
              {o.detail && (
                <Typography variant="caption" component="div" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
                  {o.detail}
                </Typography>
              )}
            </Box>
          </Box>
        )}
        renderInput={params => <TextField {...params} label={<HelpLabel label={label ?? SCOPE_TYPE_LABELS[type]} help={SCOPE_TIP} />} sx={fieldTapTargetSx} slotProps={{ ...params.slotProps, htmlInput: { ...params.slotProps.htmlInput, ...NO_AUTOFILL_INPUT, 'aria-description': SCOPE_TIP } }} />}
      />
    </Stack>
  )
}

export default ScopePicker
