import type { JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Button from '@mui/material/Button'
import type { SxProps, Theme } from '@mui/material/styles'
import { CompactVerbLabel, compactVerbProps } from './CompactVerb'
import Tip from './Tip'

/* THE CONSOLE'S CREATE CONTROL — every "create a record" button: a list header's action, a tab strip's verb, an
 * empty state's one action. Contained primary; its words from 481px up, a wordless ＋ on a phone (`ui/CompactVerb`),
 * so it shares the strip's row there; `worded` opts out (an empty state's lone action). The words are "New <thing>"
 * for what is made in Jaah (New task, New ticket) and "Add <thing>" for what exists outside it (Add user, Add
 * account). The label also rides `aria-label` (or is the text itself when `worded`), so AT and
 * `getByRole('button'|'link', { name })` find it at every width. `worded` wins over `wordless`.
 * `to` makes it a router link (a create PAGE), `onClick` a button (a dialog or an in-place create). */
export default function NewButton({
  label,
  to,
  onClick,
  testId,
  wordless = false,
  worded = false,
  disabled,
  title,
  sx,
}: {
  /** The verb and noun, e.g. "New playbook" — the words and the accessible name. */
  label: string
  to?: string
  onClick?: () => void
  testId?: string
  /** The ＋ at every width: a narrow list beside an open record (Projects), where the words would take the row. */
  wordless?: boolean
  /** The words at every width: an empty state's one action, alone on its line, where a lone ＋ says nothing. */
  worded?: boolean
  disabled?: boolean
  /** Hover text, only when it says more than the label (why it is disabled). */
  title?: string
  /** The caller's own geometry (the Tasks split button squares one side). */
  sx?: SxProps<Theme>
}): JSX.Element {
  const link = to === undefined ? {} : { component: RouterLink, to }
  const props = worded ? { sx } : compactVerbProps(label, sx, wordless)
  return (
    <Tip title={title}><Button
      {...link}
      variant="contained"
      color="primary"
      onClick={onClick}
      disabled={disabled}
      {...props}
      data-testid={testId}
    >
      {worded ? label : <CompactVerbLabel label={label} wordless={wordless} />}
    </Button></Tip>
  )
}
