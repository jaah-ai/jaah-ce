import { useCallback, useRef, useState, type JSX, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiDetail } from '../api'
import { getOrgsByIds, listOrgs, ORG_LOOKUP_PER } from '../lib/orgs'
import type { Org } from '../types'
import { AsyncLookup, type LookupOption } from './AsyncLookup'

/* ── THE CONSOLE'S ONE "WHICH ORG" CONTROL ──
 *
 * Operator-plane CREATE forms (nodes, node groups, crons, groups, users, projects) take a REQUIRED `org_id` and
 * 422 without one. Shared for its behaviours, most invisible when wrong:
 *
 *   1. **THERE IS NO INVISIBLE DEFAULT, EVER — NOT EVEN FOR A ROSTER OF ONE.** A hidden fallback files work
 *      under a tenant nobody chose, and the form succeeds while wrong. (A one-org roster can occur live:
 *      only the operator plane is kept enabled, `routes/orgs.py::_OPERATOR_UNDISABLABLE`.)
 *   2. **A DISABLED ORG IS NOT AN OPTION**: filtered out, not greyed (unlike `CredentialSelect`) — an org
 *      that is off was turned off on purpose.
 *   3. **EVERY UNHAPPY STATE SAYS WHICH ONE IT IS** in the helper slot: naming the held value, refused
 *      (`GET /api/orgs` is superadmin-only), a held value that is disabled or out of sight.
 *
 * A SERVER-SEARCHED `ui/AsyncLookup`: the roster is paged, so the control never holds every org.
 * Focus offers the first `ORG_LOOKUP_PER`; typing searches name and slug. A value it did not pick (a form's
 * default, a preselect) is named by one `?ids=` read. Callers wrap it in `ui/FormField`, which clones `label`/
 * `required`/`error`/`helperText` onto it; it overrides that slot only when its own value needs a word
 * (`valueMessage`).
 */

/** THE OPERATOR PLANE'S OWN SLUG — org zero (`apps/api/app/tenancy/ids.py::OPERATOR_ORG`), permanently
 *  enabled, the one option that is not a tenant. Matched on SLUG, not id (`src/types.ts`'s `Org` rule:
 *  the uuids live only in `ids.py`).
 *
 *  LABELLED, NOT HIDDEN: filing into it is legitimate, but on `nodes` the choice is PERMANENT
 *  (`trg_nodes_org_id_immutable`), so it must never read as somebody's tenant. */
export const OPERATOR_PLANE_SLUG = 'operator'

/** The field's default label, spelled once for every form. */
export const ORG_FIELD_LABEL = 'Organization'

/** What the operator plane's row says beside its slug (see OPERATOR_PLANE_SLUG). */
const OPERATOR_PLANE_HINT = 'operator plane, not a tenant'

export interface OrgSelectProps {
  /** Required and stable — it is the input's `id`, so `FormField`'s `<label>` can reach it. */
  id: string
  /** The chosen `orgs.id`, or `null` for nothing chosen. Never `''` (a form converts at its own edge). */
  value: string | null
  /** An id, never null: no route accepts an unset org, so clearing the box keeps the held value. The row
   *  rides along for a caller that needs the NAME (the Add-node wizard titles a way after the org). */
  onChange: (next: string, org: Org) => void
  /** The held org's name when the caller already knows it: the box shows it while the lookup reads the row,
   *  instead of a bare `…`. */
  valueLabel?: string
  /** Normally supplied by the enclosing `ui/FormField`. */
  label?: string
  helperText?: ReactNode
  error?: boolean
  required?: boolean
  /** The FORM's reason to lock the field. */
  disabled?: boolean
  testId?: string
  /** A word a form adds to a row (the automation wizard: "not a member"), after its slug; null adds nothing. */
  optionNote?: (org: Org) => string | null
  /** A row LEFT OUT of the list: the org a move's subject is already in (`components/ChangeOrgDialog`). */
  excludeOrgId?: string | null
  /** A row offered but NOT PICKABLE, with the reason after its slug (the operator plane for a kind whose rule
   *  refuses it); null offers the row. Unlike a disabled org (filtered out, rule 2), the reader must see WHY. */
  optionDisabled?: (org: Org) => string | null
  /** From `ui/FormField` for hover help; the lookup's input carries no description slot, so it is accepted
   *  and unused rather than breaking `FormField`'s clone. */
  slotProps?: { htmlInput?: Record<string, unknown> }
}

/** One org as a lookup row: the name, the slug beside it (names can collide; the slug keys the fleet), and the
 *  operator plane said in words. */
export function orgOption(org: Org): LookupOption {
  return {
    key: org.id,
    label: org.name,
    hint: org.slug === OPERATOR_PLANE_SLUG ? `${org.slug} · ${OPERATOR_PLANE_HINT}` : org.slug,
  }
}

/** `orgOption` with a form's note after the slug (`optionNote`). */
function notedOption(org: Org, note: string | null | undefined): LookupOption {
  const option = orgOption(org)
  return note ? { ...option, hint: `${option.hint} · ${note}` } : option
}

/** The held org ids' rows, by one `?ids=` read; shared by every control that names a value it did not pick. */
export function useOrgsByIds(ids: readonly string[], enabled = true) {
  const key = [...new Set(ids)].sort()
  return useQuery({
    queryKey: ['orgs', 'ids', key],
    queryFn: ({ signal }) => getOrgsByIds(key, signal),
    enabled: enabled && key.length > 0,
    staleTime: 60_000,
    retry: false,
  })
}

export function OrgSelect({
  id,
  value,
  onChange,
  valueLabel,
  label,
  helperText,
  error,
  required,
  disabled = false,
  testId,
  optionNote,
  excludeOrgId = null,
  optionDisabled,
}: OrgSelectProps): JSX.Element {
  /* Every row a search returned, by id, so a pick hands the caller the full `Org` — and the box names a value
     this control picked without a second read. */
  const seen = useRef(new Map<string, Org>())
  const known = value ? seen.current.get(value) : undefined
  const held = useOrgsByIds(value ? [value] : [], !known)
  const row = known ?? held.data?.find(o => o.id === value) ?? null

  /* No route accepts `null`, so the box draws no clear ✕. A `null` that still arrives keeps the held
     value, and the lookup is remounted so its box shows that value again rather than an empty field over a live
     choice. */
  const [mount, setMount] = useState(0)

  /* One org as its row: the form's note after the slug, or the refusal — which also disables the row. */
  const toOption = useCallback((org: Org): LookupOption => {
    const refusal = optionDisabled?.(org) ?? null
    const option = notedOption(org, refusal ?? optionNote?.(org))
    return refusal ? { ...option, disabled: true } : option
  }, [optionNote, optionDisabled])

  const fetchOptions = useCallback(async (query: string, signal: AbortSignal) => {
    const page = await listOrgs({ q: query, per: ORG_LOOKUP_PER }, signal)
    for (const org of page.items) seen.current.set(org.id, org)
    return page.items.filter(org => org.enabled && org.id !== excludeOrgId).map(toOption)
  }, [toOption, excludeOrgId])

  /* Non-null when the held value needs a word the CALLER's hint does not carry; it takes the shared helper
     slot. Loading reds nothing; a refusal, a disabled or an unseen org does. */
  let valueMessage: string | null = null
  let valueError = false
  if (value && !known) {
    if (held.isError) {
      valueMessage = apiDetail(held.error, 'Could not load the organization.')
      valueError = true
    } else if (held.isPending) {
      valueMessage = valueLabel ? null : 'Loading the organization…'
    } else if (!row) {
      valueMessage = 'Not among the organizations you can see. Pick another.'
      valueError = true
    }
  }
  if (row && !row.enabled) {
    valueMessage = `${row.name} is disabled. Pick another.`
    valueError = true
  }

  const option: LookupOption | null = value
    ? row ? toOption(row) : { key: value, label: valueLabel ?? (held.isPending && !held.isError ? '…' : `Organization ${value}`) }
    : null

  return (
    <AsyncLookup
      key={mount}
      id={id}
      value={option}
      onChange={next => {
        if (!next) {
          setMount(n => n + 1)
          return
        }
        // Only a listed row can be picked, so the lookup cannot miss.
        const picked = seen.current.get(next.key)
        if (picked) onChange(picked.id, picked)
      }}
      fetchOptions={fetchOptions}
      fetchOnFocus
      disableClearable
      label={label}
      placeholder="Search by name or slug"
      noOptionsText="No enabled organization matches"
      required={required}
      error={Boolean(error) || valueError}
      helperText={valueMessage ?? helperText}
      disabled={disabled}
      testId={testId}
    />
  )
}

export default OrgSelect
