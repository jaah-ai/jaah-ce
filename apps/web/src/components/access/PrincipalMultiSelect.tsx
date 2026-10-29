import { useRef, useState } from 'react'
import type { JSX, KeyboardEvent, Ref } from 'react'
import Autocomplete from '@mui/material/Autocomplete'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { useAsyncOptions } from '../../hooks/useAsyncOptions'
import { searchPrincipals } from './api'
import type { AccessScope, PickerEntry } from './api'
import { NO_AUTOFILL_INPUT } from '../../ui/noAutofill'
import HelpLabel from '../../ui/HelpLabel'

// The Add dialog's "Who": users and groups (users only for a group's members), searched server-side (`GET /api/access/principals`)
// through `hooks/useAsyncOptions` (the debounce, one in-flight request, superseded answers discarded, the busy and
// error lines); the rows and chips are this picker's own.
// DM1: Enter resolves against the results for the CURRENT text (awaiting the pending search, so a fast
// type+Enter works); exactly one match is selected and reported via `onEnterComplete` with the NEW value —
// the parent's state may not have updated yet. Several matches: Enter selects the highlighted one only.

const same = (a: PickerEntry, b: PickerEntry) => a.kind === b.kind && a.id === b.id

export function PrincipalMultiSelect({
  scope,
  value,
  onChange,
  onEnterComplete,
  autoFocus,
  inputRef,
  help,
  usersOnly,
}: {
  scope: AccessScope
  value: PickerEntry[]
  onChange: (value: PickerEntry[]) => void
  onEnterComplete?: (value: PickerEntry[]) => void
  autoFocus?: boolean
  inputRef?: Ref<HTMLInputElement>
  /** The label's hover (`ui/HelpLabel`); the Add-access dialog's, not a group's member picker. */
  help?: string
  /** Offer users only: a group's member picker, since a group cannot join a group. */
  usersOnly?: boolean
}): JSX.Element {
  const [input, setInput] = useState('')
  const latestInput = useRef('')
  const highlighted = useRef<PickerEntry | null>(null)
  latestInput.current = input

  const query = input.trim()
  const { shown, busy, emptyText, failed, scheduleQuery, settle } = useAsyncOptions<PickerEntry>({
    // The route takes no signal: the hook itself discards a superseded answer.
    fetchOptions: q => searchPrincipals(scope, q).then(rows => (usersOnly ? rows.filter(o => o.kind === 'user') : rows)),
    query,
    typed: query.length > 0,
    minChars: 1,
    fetchOnFocus: false,
    noOptionsText: 'No matches',
  })

  const options = shown.filter(o => !value.some(v => same(v, o)))

  const onKeyDown = async (event: KeyboardEvent<HTMLInputElement> & { defaultMuiPrevented?: boolean }) => {
    if (event.key !== 'Enter' || event.nativeEvent.isComposing) return
    event.defaultMuiPrevented = true
    event.preventDefault()
    const q = input.trim()
    if (!q) {
      if (value.length) onEnterComplete?.(value)
      return
    }
    let rows: readonly PickerEntry[]
    try {
      rows = await settle()
    } catch {
      return
    }
    if (latestInput.current.trim() !== q) return
    const candidates = rows.filter(o => !value.some(v => same(v, o)))
    if (!candidates.length) return
    const unambiguous = candidates.length === 1
    const pick = unambiguous
      ? candidates[0]
      : (candidates.find(o => highlighted.current && same(o, highlighted.current)) ?? candidates[0])
    const next = [...value, pick]
    onChange(next)
    setInput('')
    if (unambiguous) onEnterComplete?.(next)
  }

  return (
    <Autocomplete
      multiple
      data-testid="principal-select"
      options={options}
      value={value}
      onChange={(_, next) => onChange(next)}
      inputValue={input}
      onInputChange={(_, next) => {
        setInput(next)
        scheduleQuery()
      }}
      onHighlightChange={(_, option) => {
        highlighted.current = option
      }}
      filterOptions={o => o}
      autoHighlight
      loading={busy}
      noOptionsText={query ? <Box component="span" sx={failed ? { color: 'error.main' } : undefined}>{emptyText}</Box> : 'Type to search'}
      isOptionEqualToValue={same}
      getOptionLabel={o => o.name}
      getOptionKey={o => `${o.kind}:${o.id}`}
      renderOption={({ key, ...props }, o) => (
        <Box component="li" key={key} {...props} sx={{ gap: 1 }}>
          <Box sx={{ minWidth: 0, flex: 1 }}>
            <Typography variant="body2" noWrap>{o.name}</Typography>
            {/* A nameless user's name is its email: printed once, not again as the caption. */}
            {(o.kind === 'group' || o.email !== o.name) && (
              <Typography variant="caption" sx={{ color: 'text.secondary' }} noWrap component="div">
                {o.kind === 'group' ? `Group · ${o.member_count ?? 0} members` : o.email}
              </Typography>
            )}
          </Box>
          {o.kind === 'group' && <Chip label="Group" size="small" variant="outlined" />}
        </Box>
      )}
      renderValue={(selected, getItemProps) =>
        selected.map((o, index) => {
          const { key, ...itemProps } = getItemProps({ index })
          return (
            <Chip
              key={key}
              {...itemProps}
              size="small"
              label={o.name}
              variant={o.kind === 'group' ? 'outlined' : 'filled'}
            />
          )
        })
      }
      renderInput={params => (
        <TextField
          {...params}
          label={<HelpLabel label="Who" help={help} />}
          autoFocus={autoFocus}
          inputRef={inputRef}
          error={failed}
          helperText={failed ? emptyText : undefined}
          onKeyDown={onKeyDown}
          slotProps={{ ...params.slotProps, htmlInput: { ...params.slotProps.htmlInput, ...NO_AUTOFILL_INPUT } }}
        />
      )}
    />
  )
}

export default PrincipalMultiSelect
