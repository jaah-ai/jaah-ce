// Best-effort Web Storage: the one home for the try/catch around view-preference reads and writes; a
// throw is an absence. Storage is named, not passed, so the `localStorage`/`sessionStorage` access itself
// is inside the try: private-mode Safari and blocked storage throw on ACCESS, not only on write.

export type StorageName = 'local' | 'session'

function storage(name: StorageName): Storage {
  return name === 'local' ? localStorage : sessionStorage
}

/** The parsed JSON under `key`, or `null` when it is absent, unreadable, or unparseable. */
export function readJson(name: StorageName, key: string): unknown {
  try {
    const raw = storage(name).getItem(key)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

/** `readJson`, narrowed to a plain (non-array) object — the payload shape a per-field reader indexes. */
export function readJsonObject(name: StorageName, key: string): Record<string, unknown> | null {
  const parsed = readJson(name, key)
  return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
    ? (parsed as Record<string, unknown>)
    : null
}

/** Store `value` as JSON under `key`. Returns whether the write landed (quota, blocked storage). */
export function writeJson(name: StorageName, key: string, value: unknown): boolean {
  try {
    storage(name).setItem(key, JSON.stringify(value))
    return true
  } catch {
    return false
  }
}

/** The string under `key`, or `null` when it is absent or unreadable. */
export function readString(name: StorageName, key: string): string | null {
  try {
    return storage(name).getItem(key)
  } catch {
    return null
  }
}

/** Store `value` under `key`, or remove the key for `null`. Returns whether the write landed. */
export function writeString(name: StorageName, key: string, value: string | null): boolean {
  try {
    if (value === null) storage(name).removeItem(key)
    else storage(name).setItem(key, value)
    return true
  } catch {
    return false
  }
}

/** Remove `key`. A store that throws is a store nothing was written to. */
export function removeKey(name: StorageName, key: string): void {
  writeString(name, key, null)
}

/** The entries of a stored list that pass `keep`, or `[]` for a non-array: a wrong-typed entry is dropped,
 *  never the whole list. */
export function validEntries<T>(value: unknown, keep: (entry: unknown) => entry is T): T[] {
  return Array.isArray(value) ? value.filter(keep) : []
}
