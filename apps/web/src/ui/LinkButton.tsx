import { forwardRef } from 'react'
import Link, { type LinkProps } from '@mui/material/Link'
import Tip from './Tip'

/* THE CONSOLE'S ONE INLINE TEXT ACTION: a MUI `Link` rendered as a `<button type="button">`, for a verb that
 * sits in prose or beside a value and does not navigate (toggle, jump, open a dialog). The two fixed props are
 * what every site used to spell out; everything else (`variant`, `sx`, `underline`, `disabled`, `aria-*`,
 * `data-testid`, `color`) is the caller's; a `title` is its `ui/Tip`, typed as MUI types a `Link` over a `<button>`.
 * `type="button"` is NOT optional: inside a form the UA default is `submit`. The ref reaches the `<button>`,
 * so a `Tooltip` (or a focus call) can hold it as a bare `Link` would. */
export type LinkButtonProps = Omit<LinkProps<'button'>, 'component' | 'type' | 'ref'>

const LinkButton = forwardRef<HTMLButtonElement, LinkButtonProps>(function LinkButton({ title, ...props }, ref) {
  return <Tip title={title}><Link ref={ref} component="button" type="button" {...props} /></Tip>
})

export default LinkButton
