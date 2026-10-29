import { Fragment, type JSX } from 'react'

/** `text` with a break opportunity after each `sep`, so a long path or name wraps between its parts
 *  (`/repos/` + `acme`), never inside one; a part too long for the line still breaks where its host allows. */
export function BreakAfter({ text, sep }: { text: string; sep: string }): JSX.Element {
  return (
    <>
      {text.split(sep).map((part, i, parts) => (
        <Fragment key={i}>{part}{i < parts.length - 1 && <>{sep}<wbr /></>}</Fragment>
      ))}
    </>
  )
}
