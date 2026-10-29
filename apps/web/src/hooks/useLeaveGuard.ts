import { useCallback, useContext, useEffect, useId, useRef, useState } from 'react'
import {
  createPath,
  UNSAFE_DataRouterContext,
  UNSAFE_NavigationContext,
  useLocation,
  useNavigate,
  type To,
} from 'react-router-dom'
import type { UnsavedChoice } from '../ui/UnsavedChangesDialog'

type Go = (to: To, state?: unknown, opts?: unknown) => void
/** `popped`: the browser's Back/Forward, held by the data router's blocker until the dialog settles it. */
type Target = { path: string; state?: unknown; replace?: boolean; popped?: boolean }

const NO_KEYS: readonly string[] = []

/** `/settings/` and `/settings` are one page. */
const samePath = (a: string, b: string) => a.replace(/\/+$/, '') === b.replace(/\/+$/, '')

/** Whether `next` stays on this page: the same path, with each of `keys` (the query params that pick what the page
 *  shows, a tab strip's) unchanged. */
const stays = (next: URL, pathname: string, search: string, keys: readonly string[]) => {
  if (!samePath(next.pathname, pathname)) return false
  const current = new URLSearchParams(search)
  return keys.every(key => next.searchParams.get(key) === current.get(key))
}

/* The whole-page unsaved-work gate, driving `ui/UnsavedChangesDialog`: while `dirty`, ANY in-app way to
 * another page asks first — a link, a sidebar entry, a `navigate()` — because it is the router's own navigator that is
 * gated (`push`/`replace`, which every one of them ends in), not one crumb. A link's own click handler still runs first
 * (a flyout, a sheet that closes). A move within this same path (a Settings category, a section) is not leaving,
 * unless it changes one of `searchKeys`: the query params of a tab strip that unmounts the form (Customers › Billing's
 * `?tab=` and `?billing=`).
 * A reload or a closed tab gets the browser's own prompt (`beforeunload`, the only one a page may raise there).
 * The browser's Back/Forward is gated by the data router's blocker (`main.tsx` mounts one; the navigator never sees a
 * pop). The blocker is registered on the router itself rather than through `useBlocker`, which throws outside a data
 * router (a test's `MemoryRouter`, the Live Cast viewer): there only links and `navigate()` are gated. The router
 * keeps ONE blocker: mount one dirty guard per page.
 * `save` resolves true when the write LANDED; only then does Save leave. */
export function useLeaveGuard(
  dirty: boolean,
  save: () => Promise<boolean>,
  searchKeys: readonly string[] = NO_KEYS,
) {
  const { navigator } = useContext(UNSAFE_NavigationContext)
  const { pathname, search } = useLocation()
  const navigate = useNavigate()
  const router = useContext(UNSAFE_DataRouterContext)?.router
  const blockerKey = useId()
  /** The Back the router is holding, kept as it was blocked: it outlives the blocker's own registration. */
  const heldPop = useRef<{ proceed?: () => void; reset?: () => void } | null>(null)
  const [target, setTarget] = useState<Target | null>(null)
  const [saving, setSaving] = useState(false)
  /** Set once the gate itself leaves, and held until this page is gone: its own navigation passes, and so does
   *  what the next page navigates on arrival (the org-scope `replace`) before this page's cleanup has run. */
  const passing = useRef(false)

  const leaveTo = useCallback((path: string, state?: unknown) => {
    if (dirty) setTarget({ path, state })
    else navigate(path, { state })
  }, [dirty, navigate])

  useEffect(() => {
    if (!dirty) return
    passing.current = false
    const gated = navigator as { push: Go; replace: Go }
    const { push, replace } = gated
    const gate = (go: Go, replacing: boolean): Go => (to, state, opts) => {
      const path = typeof to === 'string' ? to : createPath(to)
      const next = new URL(path, `${window.location.origin}${pathname}${search}`)
      if (passing.current || stays(next, pathname, search, searchKeys)) go(to, state, opts)
      else setTarget({ path, state, replace: replacing })
    }
    gated.push = gate(push, false)
    gated.replace = gate(replace, true)
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      // Chrome before 119 asks only when `returnValue` is set.
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => {
      gated.push = push
      gated.replace = replace
      window.removeEventListener('beforeunload', onBeforeUnload)
    }
  }, [dirty, navigator, pathname, search, searchKeys])

  useEffect(() => {
    if (!dirty || !router) return
    router.getBlocker(blockerKey, ({ currentLocation, nextLocation, historyAction }) => {
      const next = new URL(createPath(nextLocation), window.location.origin)
      if (historyAction !== 'POP' || passing.current || stays(next, currentLocation.pathname, currentLocation.search, searchKeys)) {
        return false
      }
      setTarget({ path: createPath(nextLocation), state: nextLocation.state, popped: true })
      // The router records the blocked state right after this returns; read it once it has.
      queueMicrotask(() => {
        heldPop.current = router.state.blockers.get(blockerKey) ?? null
      })
      return true
    })
    return () => router.deleteBlocker(blockerKey)
  }, [dirty, router, blockerKey, searchKeys])

  /** A held Back resumes (or is undone) through its blocker, so the history stack stays the browser's own. */
  const settlePop = useCallback((go: boolean) => {
    const blocker = heldPop.current
    heldPop.current = null
    if (go) blocker?.proceed?.()
    else blocker?.reset?.()
  }, [])

  const leave = useCallback((to: Target) => {
    passing.current = true
    if (to.popped) settlePop(true)
    else navigate(to.path, { state: to.state, replace: to.replace })
  }, [navigate, settlePop])

  const onChoose = useCallback(async (choice: UnsavedChoice) => {
    const to = target
    if (!to || saving) return
    if (choice === 'cancel') {
      setTarget(null)
      if (to.popped) settlePop(false)
      return
    }
    if (choice === 'discard') {
      setTarget(null)
      leave(to)
      return
    }
    setSaving(true)
    try {
      // A save that throws stays, as one refused: the page says why.
      const landed = await save().catch(() => false)
      setTarget(null)
      if (landed) leave(to)
      else if (to.popped) settlePop(false)
    } finally {
      setSaving(false)
    }
  }, [target, saving, save, leave, settlePop])

  return { open: target !== null, saving, onChoose, leaveTo }
}
