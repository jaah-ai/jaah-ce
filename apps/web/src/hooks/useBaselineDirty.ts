import { useCallback, useState } from 'react'

/* An edit page's "did anything change" (`PlaybookEditPage`, `TemplateEditPage`, `AutomationEditPage`): `current` is
 * the wire body a save would send as JSON, `null` until the form is seeded. The first non-null `current` becomes
 * the baseline, once — a background refetch never moves it, so dirty is "the body a save would send moved".
 * `extra` adds what the body cannot show (staged files, a removed image). `initial` is a baseline carried across a
 * remount; `reset` forgets it, for a page that moves to another row (with `current` nulled in the same batch, else the
 * body still shown becomes the baseline). The baseline is adopted in the render that first sees `current`, never in an
 * effect a render later: an edit landing in that gap would otherwise become the baseline and leave the page clean. */
export function useBaselineDirty(
  current: string | null,
  { extra = false, initial = null }: { extra?: boolean; initial?: string | null } = {},
): { base: string | null; dirty: boolean; reset: () => void } {
  const [stored, setBase] = useState<string | null>(initial)
  const base = stored ?? current
  if (stored === null && current !== null) setBase(current)
  const dirty = base !== null && current !== null && (current !== base || extra)
  const reset = useCallback(() => setBase(null), [])
  return { base, dirty, reset }
}
