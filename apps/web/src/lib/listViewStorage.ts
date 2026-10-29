/* Persistence for Automation's rosters' VIEW — Schedule, Playbooks and Automations: the search text,
 * Flat/Tree and its open folders — remembered per operator across reloads and navigation, as the
 * Credentials roster's (`credentialFilterStorage.ts`, whose reasoning this follows: `localStorage`, an
 * ADMIN-SCOPED key, a per-field read whose fallback always WIDENS). One shape, one key per roster. The
 * org narrowing is no part of it: that is the global org filter's (`lib/orgScope.ts`), so a stale persisted
 * `orgId` from before is simply not read. The storage is pure; `usePersistedListView` is the one React
 * writer every Flat/Tree roster (Credentials too) drives its view through. */

import { useState } from 'react'
import { NAME_PATH_SEPARATOR } from './nameTree'
import { readJsonObject, writeJson } from './safeStorage'
import { readExpandedPaths, readTreeMode, type TreeViewMode } from './treeViewStorage'

export type ListView = {
  /** Matched in the browser (the lists are unpaged). `''` is none. */
  q: string
  mode: TreeViewMode
  /** Group PATHS open in Tree mode, never pruned against the roster (see `CredentialView.expanded`). */
  expanded: string[]
}

/** Which roster — its storage key's stem. */
export type ListViewRoster = 'playbooks' | 'automations'

export const DEFAULT_LIST_VIEW: ListView = { q: '', mode: 'flat', expanded: [] }

export function listViewStorageKey(roster: ListViewRoster, adminId: string | null): string {
  return `cockpit.${roster}.view:${adminId ?? 'anon'}`
}

/** The view before this viewer ever changed it. `fallback` lets a roster open in Tree mode (Automations, whose
 *  `org/area/name` rows are filed by prefix); it is returned BY IDENTITY, so a page can tell a first visit from
 *  a stored view that happens to match it. */
export function loadListView(roster: ListViewRoster, adminId: string | null, fallback: ListView = DEFAULT_LIST_VIEW): ListView {
  const stored = readJsonObject('local', listViewStorageKey(roster, adminId))
  if (!stored) return fallback
  return {
    q: typeof stored.q === 'string' ? stored.q : '',
    mode: readTreeMode(stored.mode),
    expanded: readExpandedPaths(stored.expanded),
  }
}

/** Swallows a blocked-storage throw: failing to REMEMBER must never cost the view. */
export function saveListView(roster: ListViewRoster, adminId: string | null, view: ListView): void {
  writeJson('local', listViewStorageKey(roster, adminId), view)
}

/** A roster's view, restored at mount (`load`) and written through (`save`) on each `patchView`, which answers
 *  the next view. `toggleGroup` opens or shuts one folder; `revealFolder` opens `path` and every ancestor, so a
 *  re-filed row lands visible where it was dropped. */
export function usePersistedListView<V extends { expanded: string[] }>(load: () => V, save: (view: V) => void) {
  const [view, setView] = useState<V>(load)
  function patchView(patch: Partial<V>): V {
    const next = { ...view, ...patch }
    setView(next)
    save(next)
    return next
  }
  function setExpanded(edit: (expanded: Set<string>) => void) {
    const expanded = new Set(view.expanded)
    edit(expanded)
    patchView({ expanded: [...expanded] } as Partial<V>)
  }
  const toggleGroup = (path: string) => setExpanded(open => (open.has(path) ? open.delete(path) : open.add(path)))
  const revealFolder = (path: string) => setExpanded(open => {
    const segments = path.split(NAME_PATH_SEPARATOR)
    for (let depth = 1; depth <= segments.length; depth += 1) open.add(segments.slice(0, depth).join(NAME_PATH_SEPARATOR))
  })
  return { view, patchView, toggleGroup, revealFolder }
}
