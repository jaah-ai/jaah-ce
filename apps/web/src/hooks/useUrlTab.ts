import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

/* The ONE home for a tab strip whose selection lives in the URL (`?tab=comments`), so a link lands on the
   tab it names and a reload keeps it. Every `?tab=` strip reads through it; not Audit and the directory (a
   path segment per tab) or a dialog's strip.
   - READ-THROUGH: the URL is the state; there is no component copy to drift from it.
   - An absent or unknown value — or a real tab this reader is not offered right now — reads as `fallback`,
     so a corrupt link never blanks the page. `fallback` may change per render (a gated task's is Answer):
     with no key, the page follows it, so a reader who clicked the old default moves with it.
   - Selecting the fallback DELETES the key, so the bare path stays the canonical address of the default —
     unless `pin`ned: a selection that must outlive a fallback about to change (the task page's hold on Task while
     its question waits, kept across a remount) writes the key anyway.
   - `replace: true`: the strip activates on focus, so arrowing across it would bury Back under tab hops.
   - Other query keys are kept; `param` namespaces a second strip on one page (`?activity=qa`).
   - `aliases` maps a retired id to its tab, so old links still land; selecting writes the canonical id. */
export function useUrlTab<Id extends string>(
  tabs: readonly { id: Id }[],
  fallback: Id,
  param = 'tab',
  aliases?: Readonly<Record<string, Id>>,
): [Id, (next: Id, pin?: boolean) => void] {
  const [searchParams, setSearchParams] = useSearchParams()
  const raw = searchParams.get(param)
  const requested = raw !== null && aliases && Object.hasOwn(aliases, raw) ? aliases[raw] : raw
  const tab = tabs.find(t => t.id === requested)?.id ?? fallback
  const selectTab = useCallback((next: Id, pin = false) => {
    setSearchParams(
      prev => {
        const params = new URLSearchParams(prev)
        if (next === fallback && !pin) params.delete(param)
        else params.set(param, next)
        return params
      },
      { replace: true },
    )
  }, [setSearchParams, fallback, param])
  return [tab, selectTab]
}
