import { useEffect, useRef, useState, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import Banner from './Banner'
import SelectField, { type SelectOption } from './SelectField'
import { PHONE } from './listStyles'
import Tip from './Tip'
import { SIZE } from '../theme'

/* A VALUE CHANGED IN PLACE: `inField`, it reads as plain text with a "Change…" action (where `editable`); pressing it
 * swaps in the select (`dense`, so the row lines up with the plain-text values beside it), focused with its picker
 * opened where the browser allows; a pick or a blur swaps back and hands focus to "Change…" (never the page's body —
 * the select unmounts). Not `inField`, a select labelled `label`. Option labels are the text the select shows. The
 * caller owns the submit: it passes `busy` (the Saving… word, and both controls held) and `error` (a dismissable
 * Banner). Test ids derive from `testId`. */
export default function InlineSelect({
  label,
  testId,
  rootTestId = testId,
  shown,
  title,
  selectTitle,
  value,
  options,
  width,
  onChange,
  inField,
  editable = true,
  disabled = false,
  busy,
  error,
  onDismissError,
}: {
  /** Names the select (its label, or its aria-label in a field cell); "Change <label>" names the action. */
  label: string
  testId: string
  rootTestId?: string
  /** The value as read in a field cell; `null` reads none (the value shows elsewhere), leaving "Change…" alone. */
  shown: ReactNode
  /** Hover on the shown value. */
  title?: string
  /** Hover on the select. */
  selectTitle?: string
  value: string
  options: readonly SelectOption[]
  width: number | string
  onChange: (value: string) => void
  inField: boolean
  editable?: boolean
  disabled?: boolean
  busy: boolean
  error: string
  onDismissError: () => void
}) {
  const [editing, setEditing] = useState(false)
  const changeRef = useRef<HTMLButtonElement>(null)
  const selectRef = useRef<HTMLSelectElement>(null)
  const [refocus, setRefocus] = useState(false)
  useEffect(() => {
    if (!editing && refocus) {
      changeRef.current?.focus()
      setRefocus(false)
    }
  }, [editing, refocus])
  useEffect(() => {
    if (!editing) return
    const select = selectRef.current
    select?.focus()
    // Opens the picker straight away; a browser without it, or refusing it, leaves the focused select.
    try {
      select?.showPicker?.()
    } catch {
      /* the focused select stands */
    }
  }, [editing])

  function done() {
    setEditing(false)
    setRefocus(true)
  }

  return (
    <Box data-testid={rootTestId} sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: inField ? 1 : 1.5, flexWrap: 'wrap' }}>
        {inField && !editing ? (
          <>
            {shown !== null && <Tip title={title}><Typography variant="inherit" data-testid={`${testId}-value`}>{shown}</Typography></Tip>}
            {editable && (
              <Button
                size="small"
                variant="text"
                disabled={busy || disabled}
                ref={changeRef}
                onClick={() => setEditing(true)}
                aria-label={`Change ${label.toLowerCase()}`}
                data-testid={`${testId}-change`}
                // A link-weight action at the value's height; the 44px tap floor on the phone.
                sx={{ minWidth: 0, py: 0, px: 0.5, typography: 'body2', [PHONE]: { minHeight: SIZE.tapTarget } }}
              >
                Change…
              </Button>
            )}
          </>
        ) : (
          <SelectField
            label={inField ? undefined : label}
            ariaLabel={inField ? label : undefined}
            testId={`${testId}-select`}
            value={value}
            options={options}
            disabled={busy || disabled}
            onChange={v => {
              onChange(v)
              if (inField) done()
            }}
            dense={inField}
            inputRef={selectRef}
            onBlur={inField ? done : undefined}
            onKeyDown={inField ? e => { if (e.key === 'Escape') done() } : undefined}
            title={selectTitle}
            sx={{ width, maxWidth: '100%' }}
          />
        )}
        {busy && <Typography variant="body2" sx={{ color: 'text.secondary' }}>Saving…</Typography>}
      </Box>
      {error && (
        <Banner severity="error" onDismiss={onDismissError} testId={`${testId}-error`}>
          {error}
        </Banner>
      )}
    </Box>
  )
}
