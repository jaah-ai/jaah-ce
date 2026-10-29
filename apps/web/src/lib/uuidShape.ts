/**
 * The one place this app decides whether a loose value is shaped like a uuid (an entity id). The feature-scope
 * readers (`lib/taskScope.ts`, `pages/taskDetail/useTaskDetailNavState.ts`) must agree, since a value one accepts is handed to
 * the other; `pages/Nodes.tsx` also reads it to spot a uuid-named unregistered node, and the credential, audit
 * and guard-activity pages read it for their path and filter values.
 * Shape only, never existence.
 */

// Canonical uuid spelling (8-4-4-4-12 hex). Case-insensitive: a URL round trip or hand-edit can
// upper-case it, and the server compares parsed ids.
const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** Is this loose value spelled like a uuid? `false` for every non-string, including `null`. */
export function isUuidShaped(value: unknown): value is string {
  return typeof value === 'string' && UUID_SHAPE.test(value)
}
