/* The API key permission grid's model, pure: rows from the served catalog (`GET /api/permissions`, never the
 * `types.ts` mirrors), the presets, and the grid value <-> the wire's `grants_all`/`permissions`/`org_admin`.
 * Every box ticked is `grants_all`: the key then follows the catalog as it grows. Any untick is an explicit set. */

import type { CatalogAction } from '../../../components/access/api'
import type { ApiKey, KeyGrants } from './api'
import { capitalize } from '../../../lib/humanize'

export const GRID_ACTIONS = ['view', 'create', 'edit', 'delete'] as const
export type GridAction = (typeof GRID_ACTIONS)[number]

export const ACTION_LABELS: Record<GridAction, string> = { view: 'View', create: 'Create', edit: 'Edit', delete: 'Delete' }

/** One feature: its label and the key in each column it has (a feature may lack one). */
export type GridRow = { feature: string; label: string; keys: Partial<Record<GridAction, string>> }

export type GridValue = { ticked: ReadonlySet<string>; orgAdmin: boolean }

export type Preset = 'all' | 'read' | 'none'

/** The honoured catalog as rows, in catalog order; an unhonoured key is omitted (no route enforces it). */
export function gridRows(catalog: readonly CatalogAction[]): GridRow[] {
  const rows: GridRow[] = []
  for (const entry of catalog) {
    if (!entry.honoured || !(GRID_ACTIONS as readonly string[]).includes(entry.action)) continue
    let row = rows.find(r => r.feature === entry.feature)
    if (!row) {
      row = { feature: entry.feature, label: capitalize(entry.feature_label || entry.feature), keys: {} }
      rows.push(row)
    }
    row.keys[entry.action as GridAction] = entry.key
  }
  return rows
}

export function rowKeys(rows: readonly GridRow[], action?: GridAction): string[] {
  return rows.flatMap(row => GRID_ACTIONS.filter(a => action === undefined || a === action).flatMap(a => row.keys[a] ?? []))
}

export function preset(rows: readonly GridRow[], which: Preset): GridValue {
  if (which === 'all') return { ticked: new Set(rowKeys(rows)), orgAdmin: true }
  if (which === 'read') return { ticked: new Set(rowKeys(rows, 'view')), orgAdmin: false }
  return { ticked: new Set(), orgAdmin: false }
}

/** The preset a value equals, if any (the preset buttons show which one is in force). */
export function presetOf(rows: readonly GridRow[], value: GridValue): Preset | null {
  for (const which of ['all', 'read', 'none'] as const) {
    const p = preset(rows, which)
    if (p.orgAdmin === value.orgAdmin && p.ticked.size === value.ticked.size && [...p.ticked].every(k => value.ticked.has(k))) {
      return which
    }
  }
  return null
}

export function fromKey(rows: readonly GridRow[], key: Pick<ApiKey, 'grants_all' | 'permissions' | 'org_admin'>): GridValue {
  return { ticked: new Set(key.grants_all ? rowKeys(rows) : key.permissions), orgAdmin: key.org_admin }
}

export function toGrants(rows: readonly GridRow[], value: GridValue): KeyGrants {
  const all = rowKeys(rows)
  const grantsAll = all.every(k => value.ticked.has(k))
  return {
    grants_all: grantsAll,
    permissions: grantsAll ? [] : all.filter(k => value.ticked.has(k)),
    org_admin: value.orgAdmin,
  }
}

/** The list's read of a key's grants, as a permissions fact: "All permissions", "Read-only", "No permissions" or
 *  "N of M permissions" (Org administration counts). */
export function grantsSummary(rows: readonly GridRow[], key: Pick<ApiKey, 'grants_all' | 'permissions' | 'org_admin'>): string {
  const value = fromKey(rows, key)
  const which = presetOf(rows, value)
  if (which === 'all') return 'All permissions'
  if (which === 'read') return 'Read-only'
  if (which === 'none') return 'No permissions'
  const all = rowKeys(rows)
  const count = all.filter(k => value.ticked.has(k)).length + (value.orgAdmin ? 1 : 0)
  return `${count} of ${all.length + 1} permissions`
}
