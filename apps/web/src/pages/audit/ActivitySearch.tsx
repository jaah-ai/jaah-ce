import { useEffect, useRef, useState, type JSX } from 'react'
import Autocomplete from '@mui/material/Autocomplete'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import CircularProgress from '@mui/material/CircularProgress'
import ListSubheader from '@mui/material/ListSubheader'
import TextField from '@mui/material/TextField'
import { get } from '../../api'
import { useDebouncedCallback } from '../../hooks/useDebouncedCallback'
import { highlight, OPTION_ROW_SX } from '../../ui/AsyncLookup'
import { AUDIT_FOLD_FILTERS, ELLIPSIS_SX, fieldTapTargetSx, PHONE } from '../../ui/listStyles'
import { NO_AUTOFILL_INPUT } from '../../ui/noAutofill'
import { SEARCH_DEBOUNCE_MS, SEARCH_MAX_WAIT_MS } from '../../ui/SearchBox'
import { FACET_LABEL, matchResults, matchSession, recordLabel, type ChipFacet } from './activityFilters'
import { Q_MAX_LEN, Q_MIN_LEN } from './queryLimits'
import type { AuditSuggestions } from './types'

/* AUDIT › ACTIVITY'S SEARCH BOX — free text and filter CHIPS in one field (MUI `Autocomplete` `multiple` +
 * `freeSolo`, so a typed phrase and the chips coexist).
 *
 *   • The free text becomes the `q` search ONLY on Enter or "Search for “…”" (first and highlighted, so
 *     Enter is the natural key). Never on a typing pause: typing "acme" to find an Organization must not
 *     blank the list under the open popup. A commit moves the text into a "Search: …" chip (the caller's,
 *     from the URL) and empties the field, so the committed search is never hidden. The field is a local
 *     draft: clearing it (×, Escape, delete) leaves the committed search alone; Enter on an empty field,
 *     the chip's ×, or Clear filters removes it.
 *   • Typing also asks `GET /api/audit/suggest?q=` on the same debounce, one request at a time, a superseded
 *     answer DISCARDED (aborted, then fenced on `signal.aborted` — `ui/AsyncLookup`'s rule). Result options
 *     and a typed session id are client-side (`activityFilters.ts::matchResults`, `matchSession`), so they appear
 *     without waiting.
 *   • Picking a suggestion adds its chip and clears the text that found it (that text was a lookup, not a
 *     search). Result and Record are single: a second pick replaces the first.
 *   • A chip goes by its ×, or Backspace in the empty field (MUI's own; ←/→ walk the chips). The Search
 *     chip goes the same way.
 * Organization is the operator's alone: a tenant is never offered it (the server sends `orgs: []` too). */

type Suggestion =
  | { kind: 'search'; key: string; group: ''; text: string }
  | { kind: 'chip'; key: string; group: string; facet: ChipFacet; value: string; label: string; hint?: string }
  | { kind: 'note'; key: string; group: ''; text: string }

type ChipItem = Extract<Suggestion, { kind: 'chip' }>

export type ActivityChip = { facet: ChipFacet; value: string; label: string }

const GROUP: Record<ChipFacet, string> = {
  q: '',
  user: 'Users',
  org: 'Organizations',
  record: 'Records',
  result: 'Results',
  session: 'Sessions',
}

const NO_SUGGESTIONS: AuditSuggestions = { users: [], orgs: [], subjects: [] }

/* The chip: prefix muted and fixed, value free to WRAP — a long email on a phone breaks onto a second line
   inside its chip rather than ellipsising to a stub (the shredded-title failure, in a chip). */
const CHIP_SX = {
  height: 'auto',
  maxWidth: '100%',
  py: '3px',
  borderRadius: '6px',
  '& .MuiChip-label': { whiteSpace: 'normal', overflowWrap: 'anywhere', lineHeight: 1.35, px: '8px' },
} as const

const PREFIX_SX = {
  color: 'text.secondary',
  fontWeight: 600,
  fontSize: '0.6875rem',
  letterSpacing: '0.04em',
  textTransform: 'uppercase',
  mr: '6px',
} as const

export default function ActivitySearch({
  query,
  onQuery,
  chips,
  onAddChip,
  onRemoveChip,
  operator,
  resetKey,
}: {
  /** The committed free text (the URL's `q`); its chip is in `chips`. */
  query: string
  onQuery: (next: string) => void
  /** Includes the `q` chip while a search is committed. */
  chips: readonly ActivityChip[]
  onAddChip: (chip: ActivityChip) => void
  onRemoveChip: (chip: ActivityChip) => void
  /** Offers the Organization facet. */
  operator: boolean
  /** Bumped by Clear filters, to drop typed-but-uncommitted text. */
  resetKey: number
}): JSX.Element {
  const [draft, setDraft] = useState('')
  /* Read at fire time by the debounce, so it sees the latest keystroke. */
  const draftRef = useRef(draft)
  draftRef.current = draft

  /* ── Suggestions: debounced, one in flight, superseded answers discarded ── */
  const [requested, setRequested] = useState<string | null>(null)
  const [answer, setAnswer] = useState<{ for: string; body: AuditSuggestions; failed: boolean } | null>(null)
  const { trigger: scheduleSuggest, cancel: cancelSuggest } = useDebouncedCallback(() => {
    const text = draftRef.current.trim()
    setRequested(text ? text : null)
  }, SEARCH_DEBOUNCE_MS, SEARCH_MAX_WAIT_MS)

  /* Clear filters drops typed-but-uncommitted text. */
  const seenReset = useRef(resetKey)
  useEffect(() => {
    if (seenReset.current === resetKey) return
    seenReset.current = resetKey
    clearText()
    // `clearText` only sets state and refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey])

  useEffect(() => {
    if (requested === null) return
    const controller = new AbortController()
    get<AuditSuggestions>(`/api/audit/suggest?q=${encodeURIComponent(requested)}`, { signal: controller.signal })
      .then(body => {
        if (controller.signal.aborted) return
        setAnswer({ for: requested, body: body ?? NO_SUGGESTIONS, failed: false })
      })
      .catch(() => {
        if (controller.signal.aborted) return
        setAnswer({ for: requested, body: NO_SUGGESTIONS, failed: true })
      })
    return () => controller.abort()
  }, [requested])

  const text = draft.trim()
  /* Only the answer FOR this text is shown: during the debounce the previous rows would be a lie. */
  const settled = answer !== null && answer.for === text
  const busy = text !== '' && !settled
  const held = new Set(chips.map(chip => `${chip.facet}:${chip.value}`))

  const options: Suggestion[] = []
  if (text) {
    options.push(text.length >= Q_MIN_LEN
      ? { kind: 'search', key: 'search', group: '', text }
      : { kind: 'note', key: 'short', group: '', text: `Type ${Q_MIN_LEN} or more characters to search` })
    const add = (item: Omit<ChipItem, 'kind' | 'key' | 'group'>) => {
      const key = `${item.facet}:${item.value}`
      if (!held.has(key)) options.push({ kind: 'chip', key, group: GROUP[item.facet], ...item })
    }
    if (settled) {
      for (const u of answer.body.users) add({ facet: 'user', value: u.id, label: u.label, hint: u.hint ?? undefined })
      if (operator) for (const o of answer.body.orgs) add({ facet: 'org', value: o.id, label: o.name, hint: o.slug })
      for (const s of answer.body.subjects) {
        const value = `${s.type}:${s.id}`
        add({ facet: 'record', value, label: recordLabel(value, s.label), hint: s.type })
      }
    }
    for (const r of matchResults(text)) add({ facet: 'result', value: r.value, label: r.label })
    const session = matchSession(text)
    if (session) add({ facet: 'session', value: session, label: session })
    if (settled && answer.failed) {
      options.push({ kind: 'note', key: 'note', group: '', text: 'Suggestions unavailable' })
    }
  }

  const value: ChipItem[] = chips.map(chip => ({
    kind: 'chip',
    key: `${chip.facet}:${chip.value}`,
    group: GROUP[chip.facet],
    ...chip,
  }))

  /** Empties the field only: a committed search stays (its chip, the URL). */
  function clearText() {
    cancelSuggest()
    setDraft('')
    setRequested(null)
  }

  /** Commits `next` as the search (pushes history; '' removes it) and empties the field. */
  function commit(next: string) {
    if (next !== '' && next.length < Q_MIN_LEN) return
    clearText()
    if (next !== query) onQuery(next)
  }

  const placeholder = chips.length > 0 ? '' : 'Search or filter activity'

  return (
    <Autocomplete<Suggestion, true, true, true>
      multiple
      freeSolo
      disableClearable
      id="audit-activity-search"
      data-testid="audit-search"
      options={options}
      value={value}
      inputValue={draft}
      autoHighlight
      filterOptions={received => received}
      groupBy={option => option.group}
      getOptionKey={option => (typeof option === 'string' ? option : option.key)}
      getOptionLabel={option =>
        typeof option === 'string' ? option : option.kind === 'chip' ? option.label : option.text}
      getOptionDisabled={option => option.kind === 'note'}
      isOptionEqualToValue={(a, b) => typeof b !== 'string' && a.key === b.key}
      /* The popup can outgrow a narrow field, never the window (`ui/AsyncLookup`'s measurement). */
      slotProps={{ popper: { style: { minWidth: 'min(22rem, calc(100vw - 2rem))' } } }}
      sx={{
        flex: '1 1 420px',
        minWidth: 260,
        // On a phone, and on a list folded beside an open event (`AUDIT_FOLD_FILTERS`), the frame's other controls
        // are the Filters button (and Clear): they share this row.
        [PHONE]: { flexBasis: 0, minWidth: 0 },
        [AUDIT_FOLD_FILTERS]: { flexBasis: 0, minWidth: 0 },
        '& .MuiAutocomplete-inputRoot': { flexWrap: 'wrap', gap: '4px', py: '5px' },
        '& .MuiAutocomplete-input': { minWidth: '8ch !important', flexGrow: 1 },
      }}
      onInputChange={(_event, next, reason) => {
        // Only typing moves the draft; MUI's resets after a pick are handled in `onChange`.
        if (reason !== 'input') return
        setDraft(next)
        if (next.trim()) scheduleSuggest()
        else {
          cancelSuggest()
          setRequested(null)
        }
      }}
      onChange={(_event, _next, reason, details) => {
        const option = details?.option
        if (reason === 'removeOption' && option && typeof option !== 'string' && option.kind === 'chip') {
          onRemoveChip({ facet: option.facet, value: option.value, label: option.label })
          return
        }
        if (reason === 'createOption') {
          // Enter on free text with nothing highlighted: the search.
          commit((typeof option === 'string' ? option : draft).trim())
          return
        }
        if (reason !== 'selectOption' || !option || typeof option === 'string') return
        if (option.kind === 'search') {
          commit(option.text)
          return
        }
        if (option.kind === 'chip') {
          clearText()
          onAddChip({ facet: option.facet, value: option.value, label: option.label })
        }
      }}
      onKeyDown={event => {
        // Escape with nothing to close clears the text (the box's other clear, `ui/SearchBox`'s rule).
        if (event.key === 'Escape' && draft !== '') clearText()
        // Enter on an empty field ends the committed search (MUI fires nothing for an empty freeSolo Enter).
        if (event.key === 'Enter' && draft.trim() === '' && query !== '') {
          event.preventDefault()
          commit('')
        }
      }}
      renderValue={(items, getItemProps) =>
        items.map((item, index) => {
          if (typeof item === 'string' || item.kind !== 'chip') return null
          const { key, ...itemProps } = getItemProps({ index })
          const name = `${FACET_LABEL[item.facet]}: ${item.label}`
          return (
            <Chip
              key={key}
              {...itemProps}
              size="small"
              variant="outlined"
              data-testid="audit-chip"
              data-facet={item.facet}
              data-value={item.value}
              aria-label={name}
              title={name}
              sx={CHIP_SX}
              label={
                <>
                  <Box component="span" sx={PREFIX_SX}>{FACET_LABEL[item.facet]}</Box>
                  <Box component="span">{item.label}</Box>
                </>
              }
            />
          )
        })}
      renderGroup={params =>
        params.group === ''
          ? <li key={params.key}><Box component="ul" sx={{ p: 0 }}>{params.children}</Box></li>
          : (
            <li key={params.key}>
              <ListSubheader
                component="div"
                sx={{ lineHeight: '28px', fontSize: '0.6875rem', fontWeight: 600, letterSpacing: '0.04em', textTransform: 'uppercase' }}
              >
                {params.group}
              </ListSubheader>
              <Box component="ul" sx={{ p: 0 }}>{params.children}</Box>
            </li>
          )}
      renderOption={(props, option) => {
        const { key: _muiKey, ...rest } = props
        if (option.kind === 'search') {
          return (
            <Box component="li" key={option.key} {...rest} data-testid="audit-suggest-search">
              <Box sx={{ ...ELLIPSIS_SX, fontSize: '0.8125rem' }} title={option.text}>
                Search for “{option.text}”
              </Box>
            </Box>
          )
        }
        if (option.kind === 'note') {
          return (
            <Box component="li" key={option.key} {...rest} sx={{ fontSize: '0.75rem', color: 'text.secondary' }}>
              {option.text}
            </Box>
          )
        }
        const full = option.hint ? `${option.label} · ${option.hint}` : option.label
        return (
          <Box
            component="li"
            key={option.key}
            {...rest}
            title={full}
            aria-label={`${FACET_LABEL[option.facet]}: ${full}`}
            data-testid="audit-suggest-option"
            data-facet={option.facet}
          >
            <Box sx={OPTION_ROW_SX}>
              <Box component="span" sx={{ ...ELLIPSIS_SX, flex: '1 1 auto', fontSize: '0.8125rem' }}>
                {highlight(option.label, text)}
              </Box>
              {option.hint && (
                <Box component="span" sx={{ ...ELLIPSIS_SX, flex: '0 1 auto', maxWidth: '40%', fontSize: '0.75rem', color: 'text.secondary' }}>
                  {option.hint}
                </Box>
              )}
            </Box>
          </Box>
        )
      }}
      renderInput={params => (
        <TextField
          {...params}
          label="Search"
          placeholder={placeholder}
          sx={fieldTapTargetSx}
          slotProps={{
            ...params.slotProps,
            inputLabel: { shrink: true },
            input: {
              ...params.slotProps.input,
              endAdornment: busy
                ? <CircularProgress size={14} thickness={5} aria-label="Finding suggestions" data-testid="audit-suggest-busy" />
                : null,
            },
            htmlInput: {
              ...params.slotProps.htmlInput,
              ...NO_AUTOFILL_INPUT,
              'aria-label': 'Search activity',
              maxLength: Q_MAX_LEN,
            },
          }}
        />
      )}
    />
  )
}
