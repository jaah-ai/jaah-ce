// The Personas surface's API client, shared by the roster tab and the detail page (as `pages/cron/api.ts`
// and `pages/workflows/api.ts`). The row is the server's projection of `apps/api/app/dispatch_compose/roles.py`,
// `apps/api/app/task_manifest.py` and `apps/api/app/task_settle.py`; this file adds, filters, summarises
// and renames nothing — the page exists so nobody has to read the code.

import { get } from '../../api'

/* Every string vocabulary on this wire is typed `string` deliberately (`pages/cron/api.ts`'s argument):
 * the closed sets live in Python tables, and a persona from an older/newer API must render as itself.
 * Readers use a lookup with a raw-token fallback. (`'build' | 'analysis' | string` collapses to `string`.) */

/** One roster row, as `GET /api/personas` projects it. A strict prefix of `PersonaDetail`, so the two
 *  screens cannot disagree about a shared field. */
export type PersonaSummary = {
  /** The dispatch discriminator: a Task's `persona` value and the detail route's key. */
  name: string
  /** What an operator reads; never the identity. Mechanically title-cased from the key
   *  (`test_optimizer` → "Test Optimizer"), not a curated name table. */
  label: string
  /** The charter the runner actually runs, not always the persona name: `coder` runs under `implementer`. */
  charter_key: string
  /** The fallback model TIER (label via `lib/models.ts::fmtTier`). There is no per-persona pin to report;
   *  the resolution order that beats it is single-homed in `dispatch_compose.compose._resolve_model`. */
  default_model: string
  /** `build` or `analysis`: which charter flow governs the finish.
   *  Nullable on purpose: the tables are keyed with different populations and read with `.get()`, so a
   *  half-registered persona shows as null, not a 500. Screens must word each nullable field, never blank. */
  mode: string | null
  /** The result channel: `db`, `pr` or `outcome`. Selects the delivery doctrine, not the settle
   *  call. Nullable as `mode`. */
  delivery: string | null
  /** What must be true before `done`. Null means the persona cannot settle — worth wording. */
  done_criterion: string | null
  /** Whether it can reach a runner at all; absent from the charter map is an `error` verdict. */
  dispatchable: boolean
  /** Operator may pick it in the console; narrower than `dispatchable`. */
  operator_selectable: boolean
  /** Control-plane machinery (issueless dispatch). A marker, not a permission. */
  system_persona: boolean
  /** Every terminal type this persona may end with (`PersonaDetail.terminals` adds references). Empty is
   *  real: an interactive persona settled by the operator's Complete, or one absent from the table. */
  terminal_types: string[]
  /** The role's own paragraph, verbatim (the brief opens with it). Prose that wraps. Nullable as `mode`. */
  focus: string | null
}

/** A document this persona must upload before it may settle; the shape of a
 *  `app/persona_registry.py::PERSONA_MANIFEST` row. An empty manifest is a real answer. */
export type PersonaManifestItem = {
  /** The document kind (`spec`, `plan`), keyed in `KIND_MEDIA`. */
  kind: string
  /** Operator-facing name, or null; the page words the absence. */
  label: string | null
  /** Accepted extensions. Empty means not format-pinned (`file`), not "nothing allowed". */
  extensions: string[]
  /** `level === "hard"`, a derived convenience; read `level` for the fact. */
  required: boolean
  /** `hard` or `advisory`. Both travel because `required: false` reads as optional, while an advisory
   *  artifact's absence is recorded for the operator. Typed `string` so a new level renders as itself. */
  level: string
}

/** One ending this persona may claim and its required references
 *  (`app/task_settle.py::TERMINAL_REFERENCES`), e.g. `merged_pr` needs `pr_url`. */
export type PersonaTerminal = {
  type: string
  /** Reference parameters the settle demands; empty is a real answer. */
  required_references: string[]
}

/** What this persona may create, or null (the common case, to be worded): `shapes` it may file and the
 *  `constraint` bounding them. */
export type PersonaTaskCreationLicence = {
  shapes: string[]
  constraint: string
}

/** One section of the role brief the runner is handed: `key` is the role-table field, `title` its
 *  heading, `body` verbatim. An array because the composer's order must be preserved. */
export type PersonaRoleSection = {
  key: string
  title: string
  body: string
}

/** One persona, whole — `GET /api/personas/{name}`. Extends the roster row so shared fields cannot
 *  disagree. */
export type PersonaDetail = PersonaSummary & {
  /** States a runner may settle from (every terminal lands `done`). Several is normal: DB-direct personas
   *  may report from `awaiting_response`, and `finish` re-runs from a parked or failed row. */
  settle_states: string[]
  /** Whether the runner asks the operator a question at its gate and waits. */
  interviews: boolean
  /** Whether that interview happens in the Web Console, live, with a human at the other end. */
  console_interview: boolean
  /** Whether the session closes automatically; false for personas parked at the prompt. */
  autoclose: boolean
  /** Default autonomy, or null (the task's own setting stands). Three states; null is not "No". */
  autonomous_default: boolean | null
  /** What it must upload before it may settle. Possibly empty. */
  manifest: PersonaManifestItem[]
  /** How it may end, and what each ending must reference. */
  terminals: PersonaTerminal[]
  /** What it may create, or null for a persona that may create nothing. */
  task_creation_licence: PersonaTaskCreationLicence | null
  /** The role brief, in the composer's own order. */
  role_sections: PersonaRoleSection[]
}

/** `GET /api/personas` in the control plane's declaration order (`apps/api/app/persona_registry.py`:
 *  build first, then analysis stages, then system personas). Not re-sorted here. */
export type PersonaListPayload = { personas: PersonaSummary[] }

export const listPersonas = () => get<PersonaListPayload>('/api/personas')

/** One persona. 404 for a name not dispatched, rendered as a named not-found (stale links are normal). */
export const getPersona = (name: string) =>
  get<PersonaDetail>(`/api/personas/${encodeURIComponent(name)}`)
