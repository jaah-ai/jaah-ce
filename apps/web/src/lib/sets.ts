/* The ONE spelling of "a Set state's next value". React state holds a Set by reference, so every change is
 * a copy: `toggled` flips a key, `withAdded`/`withRemoved` set it one way. Each returns a new Set and never
 * mutates `prev`, so `setState(prev => toggled(prev, key))` always re-renders. Keep these the only copies —
 * the `const next = new Set(prev); next.delete(key) || next.add(key); return next` block was written per page before. */

/** `prev` with `key` present when it was absent, absent when it was present. */
export function toggled<K>(prev: ReadonlySet<K>, key: K): Set<K> {
  const next = new Set(prev)
  if (!next.delete(key)) next.add(key)
  return next
}

/** `prev` with `key` present (the same members when it already was). */
export function withAdded<K>(prev: ReadonlySet<K>, key: K): Set<K> {
  return new Set(prev).add(key)
}

/** `prev` without `key` (the same members when it was absent). */
export function withRemoved<K>(prev: ReadonlySet<K>, key: K): Set<K> {
  const next = new Set(prev)
  next.delete(key)
  return next
}

/** `prev` with `key` present when `on`, absent otherwise: a checkbox's `onChange(checked)`. */
export function withMember<K>(prev: ReadonlySet<K>, key: K, on: boolean): Set<K> {
  return on ? withAdded(prev, key) : withRemoved(prev, key)
}
