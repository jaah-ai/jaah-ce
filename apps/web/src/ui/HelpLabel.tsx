import { useEffect, useRef, useState, type JSX, type MouseEvent, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import type { SxProps, Theme } from '@mui/material/styles'
import ExplainPopover from './ExplainPopover'
import { hasMore, toExplain, type ChoiceHelp, type HelpProp } from './explain'
import { SR_ONLY_SX } from './listStyles'
import { FONT_SIZE, FONT_WEIGHT, RADIUS } from '../theme'
import { Tip, TAP_SHOW_MS, TIP_INTERACTIVE_LEAVE_MS, closeTapTips, tapClosers, tapMouseEcho, type TipProps } from './Tip'

/* Every label's tap-held tooltip closes when any explanation pop-up opens or another tooltip is tapped (`ui/Tip`'s
 * shared closers), so a tooltip held by a tap never sits over another label's pop-up or tooltip. */

/* ══ THE CONSOLE'S ONE EXPLANATION AFFORDANCE ═════════════════════════════════════════════════════════
 *
 * A setting's label whose explanation is a HOVER on the label: no icon, no underline, no caption — the label
 * reads plain and only the help cursor marks it. The input box shows nothing. The tooltip is the `tip`
 * (`ui/explain.ts`); when there is more to say (`more`, `choices`, `docs`) it ends with a "More" link that opens
 * `ui/ExplainPopover`. A form reaches it through a wrapper's `help` (`ui/FormField`, `ui/FormTextField`,
 * `ui/SettingCheckbox`, `ui/ChoiceField`, `ui/SectionCard`, the lookups); a wrapper also sets the control's
 * `aria-description` and renders `ExplainMoreButton`, the keyboard way to the same pop-up. No `help` renders
 * the bare label.
 *
 * The tooltip is `ui/Tip`, controlled here for the tap path below; interactive only when it carries "More", so the
 * pointer can reach the link, and then it opens ABOVE by default, so a tip the pointer may rest on never covers the
 * next row.
 *
 * A label OUTSIDE a `<label>` (a heading, a column header, a sentence) has no control to carry its description:
 * it takes focus itself (the tooltip opens on keyboard focus) and carries the tip as its own `aria-description`.
 * Its "More" sits in the tooltip's own layer, out of the tab order: the keyboard way to the pop-up is ALWAYS an
 * `ExplainMoreButton` the caller renders beside the label.
 * Inside a `<label>` it does neither — the wrapper's control carries the description, and a focus stop inside a
 * label would steal its click.
 * `pointerEvents: 'auto'`: MUI turns pointer events off on an outlined field's resting (unshrunk) label, which
 * would leave an empty field's hover dead.
 * A TAP is for reading: it opens on touchstart and holds the tooltip open for `TAP_SHOW_MS` (controlled: MUI's
 * own touch path opens only after `enterTouchDelay` of held touch, which a quick tap lifts first, and the browser's
 * compatibility mouse events after a tap would otherwise close it at once) and cancels its click, so it never
 * toggles the checkbox or focuses the field the label belongs to. A mouse click still does, and closes the tooltip,
 * which would otherwise sit over the field the choice reveals until the pointer leaves. "More" stops its
 * click, so opening the pop-up never toggles the control either. */
export function HelpLabel({ label, help, choices, placement }: {
  label: string
  help?: HelpProp
  /** One pop-up line per choice of the setting (a select's options). */
  choices?: readonly ChoiceHelp[]
  /** Where the tooltip opens; MUI's default (below) over a stack of rows covers the next row's label. */
  placement?: TipProps['placement']
}): JSX.Element {
  const [open, setOpen] = useState(false)
  const [popOpen, setPopOpen] = useState(false)
  const [popMounted, setPopMounted] = useState(false)
  const anchor = useRef<HTMLSpanElement>(null)
  const touched = useRef(false)
  // Whether the mouseover the tip is about to open on was a tap's echo, judged when it arrived (`tapMouseEcho`).
  const echoed = useRef(false)
  const tapTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const [standalone, setStandalone] = useState(false)
  useEffect(() => {
    const close = () => { clearTimeout(tapTimer.current); tapTimer.current = undefined; setOpen(false) }
    tapClosers.add(close)
    return () => { tapClosers.delete(close); clearTimeout(tapTimer.current) }
  }, [])
  // An outlined field's notch copy of its label (MUI's legend in an aria-hidden fieldset) is never a focus stop; a
  // visible `<legend>` stays standalone, and so does a label under a modal's aria-hidden app root.
  useEffect(() => { setStandalone(Boolean(anchor.current) && !anchor.current?.closest('label, fieldset[aria-hidden="true"] > legend')) })
  const explain = toExplain(help)
  if (!explain) return <>{label}</>
  const more = hasMore(explain, choices)
  const openMore = (e: MouseEvent) => {
    e.stopPropagation()
    e.preventDefault()
    clearTimeout(tapTimer.current)
    tapTimer.current = undefined
    setOpen(false)
    closeTapTips()
    setPopMounted(true)
    setPopOpen(true)
  }
  return (
    <>
      <Tip
        title={<>{explain.tip}{more && <>{' '}<MoreLink onClick={openMore} /></>}</>}
        placement={placement ?? (more ? 'top' : undefined)}
        disableInteractive={!more}
        leaveDelay={more ? TIP_INTERACTIVE_LEAVE_MS : undefined}
        open={open && !popOpen}
        // A tap's own path below opens it; the mouse events a browser fires after a tap are no hover (`tapMouseEcho`).
        onOpen={e => { if (!(e?.type.startsWith('mouse') && echoed.current)) setOpen(true) }}
        onClose={() => { if (tapTimer.current === undefined) setOpen(false) }}
      >
        <Box
          component="span"
          ref={anchor}
          data-role="help-label"
          aria-description={standalone ? explain.tip : undefined}
          tabIndex={standalone ? 0 : undefined}
          onMouseOver={e => { echoed.current = tapMouseEcho(e) }}
          onTouchStart={() => {
            touched.current = true
            closeTapTips() // one tap-held tip at a time: a second label's tap replaces the first's
            setOpen(true)
            tapTimer.current = setTimeout(() => {
              tapTimer.current = undefined
              setOpen(false)
            }, TAP_SHOW_MS)
          }}
          onClick={e => {
            // A mouse click picks the control: the tip has been read, and left open it covers what the pick reveals.
            if (!touched.current) { setOpen(false); return }
            touched.current = false
            e.preventDefault()
          }}
          sx={{ pointerEvents: 'auto', '&:hover': { cursor: 'help' } }}
        >
          {label}
        </Box>
      </Tip>
      {popMounted && (
        <ExplainPopover
          open={popOpen}
          anchorEl={anchor.current}
          onClose={() => setPopOpen(false)}
          label={label}
          explain={explain}
          choices={choices}
        />
      )}
    </>
  )
}

/** The tooltip's trailing "More": link-styled, so the tooltip still reads as one sentence. */
function MoreLink({ onClick }: { onClick: (e: MouseEvent) => void }): JSX.Element {
  return (
    <ButtonBase
      type="button"
      data-role="help-more"
      onClick={onClick}
      sx={{
        font: 'inherit', color: 'inherit', fontWeight: FONT_WEIGHT.semibold, textDecoration: 'underline', textUnderlineOffset: 'auto',
        verticalAlign: 'baseline', borderRadius: RADIUS.sm,
      }}
    >
      More
    </ButtonBase>
  )
}

/** The keyboard way to an explanation's pop-up: a wrapper renders it right after its control, OUTSIDE the
 *  `<label>` (a button inside a label would steal the label's click). Hidden until focused, then a small link.
 *  Renders nothing when there is no more to say. Its hidden form sits at its containing block's top-left, so it
 *  never makes the page scroll (`SR_ONLY_SX`'s caveat). */
export function ExplainMoreButton({ label, help, choices }: {
  label: string
  help?: HelpProp
  choices?: readonly ChoiceHelp[]
}): JSX.Element | null {
  const [open, setOpen] = useState(false)
  const [mounted, setMounted] = useState(false)
  const button = useRef<HTMLButtonElement>(null)
  const explain = toExplain(help)
  if (!explain || !hasMore(explain, choices)) return null
  return (
    <>
      <ButtonBase
        ref={button}
        type="button"
        data-role="help-more-kbd"
        data-open={open || undefined}
        onClick={e => {
          e.stopPropagation()
          closeTapTips()
          setMounted(true)
          setOpen(true)
        }}
        sx={{
          ...SR_ONLY_SX, top: 0, left: 0,
          // Shown while focused, and while its pop-up is open: the pop-up stays anchored where it opened.
          '&:focus-visible, &:focus, &[data-open]': {
            position: 'static', width: 'auto', height: 'auto', m: 0, clip: 'auto', clipPath: 'none',
            overflow: 'visible', whiteSpace: 'normal', alignSelf: 'flex-start',
            fontSize: FONT_SIZE.md, color: 'primary.main', textDecoration: 'underline', px: 0.5, borderRadius: RADIUS.sm,
          },
        }}
      >
        More about {label}
      </ButtonBase>
      {mounted && (
        <ExplainPopover
          open={open}
          anchorEl={button.current}
          onClose={() => setOpen(false)}
          label={label}
          explain={explain}
          choices={choices}
        />
      )}
    </>
  )
}

/** A choice's explanation where the choice itself is the control (a segment of `ui/SegmentedControl`): the same
 *  tooltip as a label's (`ui/Tip`), on hover and keyboard focus. A label inside the control would swallow the tap that picks
 *  it, so the choice carries its `help` as its own `aria-description` and this tooltip instead. Out of the way once
 *  used: it opens ABOVE (clear of the row below) and goes on a click and on the pointer leaving. It listens on a
 *  `<span>` (the choice's cell, `cellSx`), so a disabled choice still explains itself. No `help`, no tooltip. */
export function OptionTooltip({ help, cellSx, children }: { help?: string; cellSx?: SxProps<Theme>; children: ReactNode }): JSX.Element {
  const [open, setOpen] = useState(false)
  const cell = (
    <Box component="span" sx={cellSx} onClickCapture={() => setOpen(false)} onPointerLeave={() => setOpen(false)}>
      {children}
    </Box>
  )
  if (!help) return cell
  return (
    <Tip
      title={help}
      open={open}
      onOpen={() => setOpen(true)}
      onClose={() => setOpen(false)}
      placement="top"
      disableInteractive
    >
      {cell}
    </Tip>
  )
}

export default HelpLabel
