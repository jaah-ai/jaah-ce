import { useEffect, useRef, useState } from 'react'
import type { JSX, ReactNode } from 'react'
import AsyncLookup, { type LookupOption } from './AsyncLookup'

/* ── The console's one "choose a credential" control ──
 * Every surface that asks which stored credential to use renders this. What it owns:
 *   1. The value is an id (`credentials.id`; `name` is deliberately not unique), the display a name.
 *   2. The saved id may be absent from the roster (deleted, ungranted, or invisible — the roster is
 *      grant-scoped), or from the page of it fetched so far. `resolveOption` names it by id; one it cannot
 *      name is rendered AS the id, never blank: a blank reads as "not configured" and an unrelated save
 *      would clear it.
 *   3. `null` ("no credential") is a storable state with its own affordance.
 *   4. An option may be offered but disabled, with the reason on the row.
 * Not owned: where rows come from. `fetchOptions` may round-trip per query (`GET /api/credentials?type=…`)
 * or filter a list already held (`GET /api/cloud-connections/options`). A lookup, not a native `<select>`,
 * because the roster is unbounded; `AsyncLookup` carries the request discipline.
 */

/** One offerable credential. `hint` disambiguates on the row's right edge (a type, a region, or why it
 *  is unusable): two rows can share a name. */
export type CredentialOption = {
  /** `credentials.id`, what `onChange` reports. */
  id: string
  name: string
  hint?: string
  /** Offered but not choosable; `hint` MUST then carry why. */
  disabled?: boolean
  /** The vendor mark from `ui/credentialMarks`, passed to `LookupOption.icon` (decorative). Supplied by
   *  the caller because some option wire shapes carry no type. Optional. */
  icon?: ReactNode
}

/** How an id nothing could name renders: the id itself. Callers that know more pass `selected`. */
const unresolvedLabel = (id: string) => `Credential ${id}`

/** The hint on an unnamed saved id: only that the roster lacks it (the surface may say why). Withheld
 *  until a roster has been fetched (`fetched`). */
const UNRESOLVED_HINT = 'not in the list'

/** How a credential reads in the popup, shared by offered and saved rows so they render identically. */
function toLookupOption(option: CredentialOption): LookupOption {
  return {
    key: option.id,
    label: option.name,
    hint: option.hint,
    disabled: option.disabled,
    icon: option.icon,
  }
}

export interface CredentialSelectProps {
  /** Required and stable: the input's `id`, for `<label htmlFor>`. */
  id: string
  /** The chosen `credentials.id`, or `null` for "nothing chosen". */
  value: string | null
  onChange: (next: string | null) => void
  /** The only server seam. A fetching implementation must pass the `AbortSignal` to
   *  `api(path, { signal })`; an in-memory filter ignores it. */
  fetchOptions: (query: string, signal: AbortSignal) => Promise<CredentialOption[]>
  /** Names a set `value` nothing fetched or passed has named (a by-id read; null when it cannot). Asked once
   *  per id; a failure leaves the id showing. Same signal contract as `fetchOptions`. */
  resolveOption?: (id: string, signal: AbortSignal) => Promise<CredentialOption | null>
  /** What the caller knows about the saved row (a denormalised `credential_name`, a by-id fetch). It wins
   *  over anything seen, and is ignored unless its `id` matches `value`.
   *  Pass a stable reference (`useMemo`): it is watched by identity, and a fresh literal each render can
   *  loop. Identity is right because a caller may relabel an id (a rename under an open form). */
  selected?: CredentialOption | null
  /** The visible field copy, per `ui/FormField`'s contract. */
  label?: string
  placeholder?: string
  /** Help text: the surface's sentence about what the choice means. */
  helperText?: ReactNode
  error?: boolean
  required?: boolean
  disabled?: boolean
  /** Below this many characters no request is made. 1 by default, matching `AsyncLookup`. */
  minChars?: number
  /** What an empty answer to a real query reads as. */
  noOptionsText?: string
  testId?: string
  fullWidth?: boolean
  size?: 'small' | 'medium'
}

export function CredentialSelect({
  id,
  value,
  onChange,
  fetchOptions,
  resolveOption,
  selected = null,
  label,
  placeholder,
  helperText,
  error,
  required,
  disabled = false,
  minChars = 1,
  noOptionsText = 'No matching credential',
  testId,
  fullWidth = true,
  size = 'small',
}: CredentialSelectProps): JSX.Element {
  /* The last option seen for each id, so the box names the saved id without a second request. Keyed by
  id: `value` can change under a remembered label, and a label paired with the wrong id is worse than
  none. */
  const [seen, setSeen] = useState<Record<string, CredentialOption>>({})
  /* Whether any roster has come back yet; licenses the "not in the list" hint. */
  const [fetched, setFetched] = useState(false)

  /* The fetcher from a ref, as in `AsyncLookup`: consumers pass inline arrows. */
  const fetchRef = useRef(fetchOptions)
  fetchRef.current = fetchOptions
  const resolveRef = useRef(resolveOption)
  resolveRef.current = resolveOption
  /* Ids already asked of `resolveOption`, so one it cannot name is not asked again on every render. */
  const asked = useRef(new Set<string>())

  /* Remember what the caller tells us, so a `selected` that later goes away keeps its label. */
  useEffect(() => {
    if (selected && selected.id) {
      setSeen(prev => (prev[selected.id] === selected ? prev : { ...prev, [selected.id]: selected }))
    }
  }, [selected])

  /* A set id nothing has named yet: ask for it by id. */
  const named = value === null || value === '' || (selected !== null && selected.id === value) || value in seen
  useEffect(() => {
    if (named || value === null || !resolveRef.current || asked.current.has(value)) return
    asked.current.add(value)
    const controller = new AbortController()
    let answered = false
    resolveRef.current(value, controller.signal)
      .then(row => {
        answered = true
        if (!controller.signal.aborted && row) setSeen(prev => ({ ...prev, [value]: row }))
      })
      // An id it cannot name stays showing as the id (header point 2).
      .catch(() => { answered = !controller.signal.aborted })
    return () => {
      controller.abort()
      // Abandoned before it answered: ask again when the id is back.
      if (!answered) asked.current.delete(value)
    }
  }, [named, value])

  /* The caller's fetcher, recording every returned row on the way past, so there is still one request
  per query. */
  const lookup = async (query: string, signal: AbortSignal): Promise<LookupOption[]> => {
    const rows = await fetchRef.current(query, signal)
    if (!signal.aborted) setFetched(true)
    if (!signal.aborted && rows.length > 0) {
      setSeen(prev => {
        const next = { ...prev }
        for (const row of rows) next[row.id] = row
        return next
      })
    }
    return rows.map(toLookupOption)
  }

  /* The box's display, by authority: the caller's `selected`, then what was seen, then the id itself —
  a saved value is never an empty box (header point 2). */
  const current: LookupOption | null =
    value === null || value === ''
      ? null
      : selected && selected.id === value
        ? toLookupOption(selected)
        : seen[value]
          ? toLookupOption(seen[value])
          : { key: value, label: unresolvedLabel(value), hint: fetched ? UNRESOLVED_HINT : undefined }

  return (
    <AsyncLookup
      id={id}
      testId={testId}
      value={current}
      /* The id leaves, never the option; a clear is `null`, not `''` (a `''` wire converts at its own edge). */
      onChange={next => onChange(next === null ? null : next.key)}
      fetchOptions={lookup}
      label={label}
      placeholder={placeholder}
      helperText={helperText}
      error={error}
      required={required}
      disabled={disabled}
      minChars={minChars}
      noOptionsText={noOptionsText}
      /* Focus offers the list, so it behaves like a dropdown; typing narrows. */
      fetchOnFocus
      fullWidth={fullWidth}
      size={size}
    />
  )
}

export default CredentialSelect
