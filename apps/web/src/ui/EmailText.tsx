import type { JSX } from 'react'

const WHOLE = { whiteSpace: 'nowrap' } as const

/** An email that wraps only right after its "@": the local part and the domain each stay whole, even inside an
 *  `overflowWrap: anywhere` host (`BreakAfter` lets a part break; an address must not). Text without an "@" (a
 *  name) renders as it is. */
export default function EmailText({ text }: { text: string }): JSX.Element {
  const at = text.indexOf('@')
  if (at < 0) return <>{text}</>
  return <><span style={WHOLE}>{text.slice(0, at + 1)}</span><wbr /><span style={WHOLE}>{text.slice(at + 1)}</span></>
}
