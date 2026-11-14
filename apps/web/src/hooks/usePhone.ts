import useMediaQuery from '@mui/material/useMediaQuery'
import { PHONE_QUERY } from '../ui/listStyles'

/** Whether the viewport is phone width (`PHONE_QUERY`, 480px) — the ONE JS-side phone branch; CSS branches use
 *  `PHONE`. `noSsr` answers on the first render (MUI's `useMediaQuery` option) where a layout must not flash. */
export function usePhone({ noSsr = false }: { noSsr?: boolean } = {}): boolean {
  return useMediaQuery(PHONE_QUERY, { noSsr })
}
