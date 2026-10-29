import { useEffect, useRef, useState, type ReactNode } from 'react'
import IconButton from '@mui/material/IconButton'
import InputAdornment from '@mui/material/InputAdornment'
import TextField from '@mui/material/TextField'
import Tooltip from '@mui/material/Tooltip'
import type { SxProps, Theme } from '@mui/material/styles'
import { useDebouncedCallback } from '../hooks/useDebouncedCallback'
import { NO_AUTOFILL_INPUT } from './noAutofill'

/* THE DEBOUNCED SEARCH BOX, one for every list that searches server-side. The field is LOCAL STATE; the
 * committed term (`value`, the caller's query or URL key) catches up once typing has been quiet for
 * `delayMs`, so a word is one request, not one per key. `maxWaitMs` forces a commit under a sustained
 * typist; omit it where a query mid-word is the thing the delay exists to avoid. Enter commits at once;
 * ✕ and Escape clear (`onClear`, else `onChange('')`). It syncs back, so a caller's Clear all empties it —
 * and drops a pending commit; a Clear all that leaves `value` as it was ('' already) bumps `resetKey`.
 * `title` (what the box searches) is a tooltip on hover AND on focus, so a phone reaches it with a tap; it
 * hides once there is text, so it never covers the results being typed for. It sits ABOVE the box (flipping
 * below only without room): below, it covered the filter chips a list keeps under its search. The console's one debounce pair
 * lives here: a box that names no `delayMs` takes BOTH (tabs of one page that debounce differently are two
 * pages); a box with its own `delayMs` has no max-wait unless it also passes one. */
export const SEARCH_DEBOUNCE_MS = 300
export const SEARCH_MAX_WAIT_MS = 1000

export default function SearchBox({
  value,
  onChange,
  onClear,
  delayMs,
  maxWaitMs = delayMs === undefined ? SEARCH_MAX_WAIT_MS : undefined,
  ariaLabel,
  label,
  placeholder,
  title,
  helperText,
  maxLength,
  testId,
  resetKey,
  sx,
}: {
  /** The committed term, verbatim. */
  value: string
  /** Typing, once it pauses; Enter at once. */
  onChange: (next: string) => void
  /** Removing the term, when that differs from committing '' (e.g. a history push). */
  onClear?: () => void
  /** Omitted: the console pair above. Set: yours alone — `maxWaitMs` only if you pass it. */
  delayMs?: number
  maxWaitMs?: number
  ariaLabel: string
  label?: string
  placeholder?: string
  title?: string
  helperText?: ReactNode
  maxLength?: number
  /** The field's test id; the ✕ gets `${testId}-clear`. */
  testId?: string
  /** Changed by the caller to discard the typed text and its pending commit (Clear all). */
  resetKey?: number
  sx?: SxProps<Theme>
}) {
  const [draft, setDraft] = useState(value)
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  // What this box last committed: its own commit echoing back as `value` is not "set from outside", and
  // treating it so would drop a key typed between a `maxWaitMs` commit and its render.
  const committed = useRef<string | null>(null)
  const seenResetKey = useRef(resetKey)
  function commit(next: string) {
    committed.current = next
    onChange(next)
  }
  const { trigger: scheduleCommit, cancel } = useDebouncedCallback(() => {
    // Typed and undone within the delay: no change, so no request (it would reset the page).
    if (draft !== value) commit(draft)
  }, delayMs ?? SEARCH_DEBOUNCE_MS, maxWaitMs)
  useEffect(() => {
    const reset = seenResetKey.current !== resetKey
    seenResetKey.current = resetKey
    const echo = committed.current === value
    committed.current = null
    if (echo && !reset) return
    // Set from outside: show it, and never let the text it replaced land afterwards.
    cancel()
    setDraft(value)
    // `cancel` only touches refs; listing it would re-run this every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, resetKey])

  function clear() {
    cancel()
    setDraft('')
    if (value === '') return
    if (onClear) onClear()
    else onChange('')
  }

  const field = (
    <TextField
      data-testid={testId}
      type="search"
      label={label}
      value={draft}
      placeholder={placeholder}
      helperText={helperText}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      onChange={event => {
        setDraft(event.target.value)
        scheduleCommit()
      }}
      onKeyDown={event => {
        if (event.key === 'Enter') {
          cancel()
          if (draft !== value) commit(draft)
        } else if (event.key === 'Escape' && draft !== '') {
          event.preventDefault()
          clear()
        }
      }}
      // The native ✕ of `type="search"` would be a second clear control beside ours.
      sx={[{ '& input::-webkit-search-cancel-button': { display: 'none' } }, ...(Array.isArray(sx) ? sx : [sx])]}
      slotProps={{
        inputLabel: { shrink: true },
        htmlInput: { ...NO_AUTOFILL_INPUT, 'aria-label': ariaLabel, maxLength },
        input: {
          startAdornment: (
            <InputAdornment position="start" aria-hidden sx={{ color: 'text.secondary' }}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6">
                <circle cx="7" cy="7" r="4.75" />
                <path d="M10.5 10.5 14 14" strokeLinecap="round" />
              </svg>
            </InputAdornment>
          ),
          endAdornment: draft !== '' && (
            <InputAdornment position="end">
              <IconButton
                size="small"
                edge="end"
                aria-label="Clear search"
                data-testid={testId && `${testId}-clear`}
                onClick={clear}
              >
                ✕
              </IconButton>
            </InputAdornment>
          ),
        },
      }}
    />
  )
  if (!title) return field
  return (
    <Tooltip
      title={title}
      describeChild
      placement="top-start"
      open={hovered || (focused && draft === '')}
      disableHoverListener
      disableFocusListener
      disableTouchListener
    >
      {field}
    </Tooltip>
  )
}
