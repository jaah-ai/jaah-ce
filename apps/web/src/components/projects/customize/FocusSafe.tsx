import { type JSX, type ReactNode, useLayoutEffect, useRef } from 'react'
import Box from '@mui/material/Box'

/* A Customize Save bar drawn only while its draft is changed (`CustomizeTab`, `CiPipelineSection`) leaves on Discard or
 * a landed Save with its own button focused, which would drop focus to the page's top. On the way out it hands focus
 * to the nearest fold summary above it: the section whose edit just ended. The cleanup runs before React removes the
 * bar's node, so focus inside it can still be seen. A box that draws nothing (`display: contents`), so a sticky bar
 * still sticks to the form it sits in. */
export default function FocusSafe({ children }: { children: ReactNode }): JSX.Element {
  const ref = useRef<HTMLDivElement | null>(null)
  useLayoutEffect(() => {
    const el = ref.current
    return () => {
      if (!el || !el.contains(document.activeElement)) return
      const above = Array.from(document.querySelectorAll('summary'))
        .filter(s => el.compareDocumentPosition(s) & Node.DOCUMENT_POSITION_PRECEDING)
      above.at(-1)?.focus()
    }
  }, [])
  return <Box ref={ref} sx={{ display: 'contents' }}>{children}</Box>
}
