/* THE ORG'S PROVIDER GRID, off the identity payload (`/api/auth/me`'s `providers`, the plan term `providers`):
 * the cells `[row, auth]` the principal's org may use — `row` a built-in provider value,
 * `sdk`, or `acp:<registry id>`; `auth` `oauth` | `api_key` | `own`. The console's one reader of provider
 * availability. Missing `providers` (an older server, no `me` yet) is nothing on: fail closed. Presentation
 * only — the API refuses an off cell on its own. */

type HasGrid = { providers?: string[][] } | null | undefined

const cells = (p: HasGrid): string[][] => (Array.isArray(p?.providers) ? p.providers : [])

/** Is the cell `[row, auth]` on? */
export function cellOn(p: HasGrid, row: string, auth: string): boolean {
  return cells(p).some(([r, a]) => r === row && a === auth)
}

/** Is any cell of `row` on? */
export function providerOn(p: HasGrid, row: string): boolean {
  return cells(p).some(([r]) => r === row)
}

/** Are agents on: the SDK row or any ACP registry row? */
export function agentsOn(p: HasGrid): boolean {
  return cells(p).some(([r]) => r === 'sdk' || r.startsWith('acp:'))
}

/** Is any ACP registry row on? */
export function registryOn(p: HasGrid): boolean {
  return cells(p).some(([r]) => r.startsWith('acp:'))
}

/* EDITING A GRID (the plan editor's and an org's Overrides' Providers tab, `components/plans/ProviderGrid.tsx`): cells
 * compare as sets, so the order a toggle leaves them in is never a change. */

const cellKey = ([row, auth]: readonly string[]) => `${row}\u0000${auth}`

/** The same cells, in any order. */
export function sameCells(a: readonly (readonly string[])[], b: readonly (readonly string[])[]): boolean {
  const left = new Set(a.map(cellKey))
  const right = new Set(b.map(cellKey))
  return left.size === right.size && [...left].every(k => right.has(k))
}

/** `cells` with `[row, auth]` on or off; every other cell, a stored one the catalogue no longer lists included, kept. */
export function withCell(cells: readonly (readonly string[])[], row: string, auth: string, on: boolean): string[][] {
  const rest = cells.filter(([r, a]) => r !== row || a !== auth).map(c => [...c])
  return on ? [...rest, [row, auth]] : rest
}
