import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { CREATED_PARAM, PICK_PARAM, TOKEN_PARAM, nestedCreateUrl, returnUrl } from '../lib/nestedCreate'

/* The parent drafts waiting for their New page to come back, by token. Several can wait at once: a New page may
   open one of its own (New project → New node → New connection). Module memory only, never storage: a parent's
   draft can hold a secret. A full reload loses them (the parent's leave guard warns before one). */
const stashes = new Map<string, { pick: string; draft: unknown; under: string | null }>()
let minted = 0
/* The New pages mounted now, each with its opener's draft token. One that unmounts without going back (the user
   went elsewhere) drops that draft. */
const newPages = new Map<symbol, string | null>()

/* What a way back adds to the opener's address. A `pick` there is the opener's own ask (it is a New page too). */
const BACK_PARAMS = [TOKEN_PARAM, CREATED_PARAM]

/** Drops `token`'s draft and the drafts it sits under (its opener's opener's, …): a chain left behind. It stops
 *  at a draft a mounted New page still waits on (browser Back from an inner page lands on the middle one). */
function drop(token: string | null): void {
  while (token !== null && ![...newPages.values()].includes(token)) {
    const entry = stashes.get(token)
    stashes.delete(token)
    token = entry?.under ?? null
  }
}

/** The draft token in a page's own `?return=` (it is a New page opened through the seam), else null. */
function openerToken(search: string, origin: string): string | null {
  const path = returnUrl(search, origin, null)
  return path === null ? null : new URL(path, origin).searchParams.get(TOKEN_PARAM)
}

/** `search` without the way back's parameters, `?`-prefixed or empty. */
function withoutSeam(search: string): string {
  const params = new URLSearchParams(search)
  for (const key of BACK_PARAMS) params.delete(key)
  const rest = params.toString()
  return rest === '' ? '' : `?${rest}`
}

/**
 * A PARENT FORM's side of a nested create (lib/nestedCreate.ts). `openNew('/nodes/new', 'node_id')` stashes
 * `draft` (the form's current input) and opens that New page, told to come back here. On the way back the
 * form mounts again: seed its state from `restored`, THEN apply `picked` (invalidate the roster the field
 * lists first, so the new record is there to pick). Both are read once, on mount; the seam's parameters are
 * then stripped from the address (`replace`). Cancel comes back with `restored` and no `picked`.
 *
 * `go` replaces `navigate` for the way out, e.g. a leave guard's `leaveNow` so a dirty form leaves unasked.
 */
export function useNestedCreate<D>(draft: D, go?: (path: string) => void): {
  openNew: (newPath: string, pick: string) => void
  restored: D | null
  picked: { pick: string; id: string } | null
} {
  const location = useLocation()
  const navigate = useNavigate()
  // Idempotent under StrictMode's second initializer call: the stash is only cleared once the mount commits.
  const [back] = useState(() => {
    const params = new URLSearchParams(location.search)
    const token = params.get(TOKEN_PARAM)
    const mine = token === null ? undefined : stashes.get(token)
    const created = params.get(CREATED_PARAM)
    // Only a round trip this tab started picks: never a `?created=` typed into the parent's own address.
    const pick = mine?.pick ?? null
    return {
      adopted: mine === undefined ? null : token,
      restored: mine === undefined ? null : (mine.draft as D),
      picked: created !== null && created !== '' && pick !== null && pick !== '' ? { pick, id: created } : null,
    }
  })
  const stripped = useRef(false)
  useEffect(() => {
    if (back.adopted !== null) stashes.delete(back.adopted)
    if (stripped.current) return
    stripped.current = true
    // Only an address the seam built (`?nc=`): a page's own `created` parameter is not the seam's.
    if (!new URLSearchParams(location.search).has(TOKEN_PARAM)) return
    navigate(
      { pathname: location.pathname, search: withoutSeam(location.search), hash: location.hash },
      { replace: true, state: location.state },
    )
    // Once per mount: the address it strips is the one the form mounted at.
  }, [])

  // No hash: the token joins the query, which a hash would follow.
  const here = `${location.pathname}${withoutSeam(location.search)}`
  // This form's last stash: a second Add new… replaces it, so a New page that never mounted leaves nothing behind.
  const own = useRef<string | null>(null)
  const openNew = useCallback((newPath: string, pick: string) => {
    minted += 1
    const token = `${Date.now().toString(36)}${minted.toString(36)}`
    if (own.current !== null) stashes.delete(own.current)
    own.current = token
    // A form that is itself a New page sits under its opener's draft: the new page now carries both.
    stashes.set(token, { pick, draft, under: openerToken(location.search, window.location.origin) })
    ;(go ?? navigate)(nestedCreateUrl(newPath, here, pick, token))
  }, [draft, go, navigate, here, location.search])

  return { openNew, restored: back.restored, picked: back.picked }
}

/**
 * A NEW PAGE's side: non-null when its `?return=` is a safe in-app path. Call it after the create with the new
 * id, or on Cancel with `null`. The id rides back (`?created=`) only when the opener asked for a pick
 * (`?pick=`), so a plain `?return=` (a page linking to New without the seam) lands exactly where it said.
 * `go` replaces `navigate`, e.g. a leave guard's `leaveNow`; `via` replaces it for one call (a Cancel that should
 * still ask about unsaved input: the guard's `leaveTo`).
 */
export function useNestedReturn(
  go?: (path: string) => void,
): ((createdId: string | null, via?: (path: string) => void) => void) | null {
  const { search } = useLocation()
  const navigate = useNavigate()
  const origin = window.location.origin
  const [self] = useState(() => Symbol('new page'))
  // The opener's draft waiting for this page (the token in its `?return=`): the only one its leaving may drop.
  const [opener] = useState(() => openerToken(search, origin))
  const returned = useRef(false)
  useEffect(() => {
    newPages.set(self, opener)
    returned.current = false
    return () => {
      newPages.delete(self)
      /* Left without Save or Cancel: the opener's draft (it may hold a secret) is nobody's now. Deferred a task
         so StrictMode's unmount-remount of this page keeps it. A page that left for a New page of its own (which
         may still be loading) handed the draft on: that page's draft sits under it and drops it in turn. */
      if (!returned.current && opener !== null) {
        setTimeout(() => {
          if ([...newPages.values()].includes(opener)) return
          if ([...stashes.values()].some(entry => entry.under === opener)) return
          drop(opener)
        })
      }
    }
  }, [self, opener])
  const back = useCallback((createdId: string | null, via?: (path: string) => void) => {
    const asked = new URLSearchParams(search).has(PICK_PARAM)
    const to = returnUrl(search, origin, asked ? createdId : null)
    if (to === null) return
    returned.current = true
    ;(via ?? go ?? navigate)(to)
  }, [search, origin, go, navigate])
  return returnUrl(search, origin, null) === null ? null : back
}
