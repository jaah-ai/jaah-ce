import { useEffect, useRef, useState, type JSX } from 'react'
import Box from '@mui/material/Box'
import Tooltip, { type TooltipProps } from '@mui/material/Tooltip'

/** How long a tapped label's explanation stays up. */
const TAP_SHOW_MS = 4000

/* A field label whose explanation is a HOVER, not a caption. Dotted underline + help cursor are the
 * affordance. The tooltip is sight-only (the span is not focusable), so `ui/FormField` and
 * `ui/SettingCheckbox` also set `aria-description`. `ui/InfoTip` is the ⓘ sibling for prose not attached to a
 * label. No `help` renders the bare label. `pointerEvents: 'auto'`: MUI turns pointer events off on an outlined
 * field's resting (unshrunk) label, which would leave an empty field's hover dead. A label MUI has shrunk onto the
 * outline's notch drops the underline: dotted at 0.75 scale it read as noise; the help cursor and the
 * hover stay.
 * A TAP is for reading: it holds the tooltip open for `TAP_SHOW_MS` (controlled, because the browser's
 * compatibility mouse events after a tap would otherwise close it at once) and cancels its click, so it never
 * toggles the checkbox or focuses the field the label belongs to. A mouse click still does. */
export function HelpLabel({ label, help, placement }: {
  label: string
  help?: string
  /** Where the tooltip opens; MUI's default (below) over a stack of rows covers the next row's label. */
  placement?: TooltipProps['placement']
}): JSX.Element {
  const [open, setOpen] = useState(false)
  const touched = useRef(false)
  const tapTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(tapTimer.current), [])
  if (!help) return <>{label}</>
  return (
    <Tooltip
      title={help}
      placement={placement}
      describeChild
      open={open}
      onOpen={() => setOpen(true)}
      onClose={() => { if (tapTimer.current === undefined) setOpen(false) }}
    >
      <Box
        component="span"
        data-role="help-label"
        onTouchStart={() => {
          touched.current = true
          clearTimeout(tapTimer.current)
          setOpen(true)
          tapTimer.current = setTimeout(() => {
            tapTimer.current = undefined
            setOpen(false)
          }, TAP_SHOW_MS)
        }}
        onClick={e => {
          if (!touched.current) return
          touched.current = false
          e.preventDefault()
        }}
        sx={{
          textDecoration: 'underline dotted', textUnderlineOffset: '3px', cursor: 'help', pointerEvents: 'auto',
          'label[data-shrink="true"] &': { textDecoration: 'none' },
        }}
      >
        {label}
      </Box>
    </Tooltip>
  )
}

export default HelpLabel
