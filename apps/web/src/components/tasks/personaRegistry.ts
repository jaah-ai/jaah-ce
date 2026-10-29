// ── The persona vocabulary, as the SERVER spells it ──────────────────────────────────────────────────
// THE WEB KEEPS NO COPY: `apps/api/app/persona_registry.py` is the single home; `GET /api/tasks/personas`
// serves the WHOLE vocabulary (system personas included). This module is the client cache + accessors.
//
// THE ACCESSORS ARE SYNCHRONOUS — called from render paths everywhere; the roster is fetched ONCE per
// page load (`usePersonaRegistry`, on the authenticated shell) and read from this cache.
//
// A LEAF THAT IMPORTS NOTHING but `lib/humanize` (see `taskPersona.ts`'s header for the chunk-size reason):
// the `/tasks` route loads it unconditionally. The fetch lives in `usePersonaRegistry.ts`.
import { humanize } from '../../lib/humanize'

/** Wire shape of `persona_registry.Persona`. Every field is carried, even unrendered ones. */
export type PersonaRecord = {
  name: string
  /** The board/cell label — the word a row's Persona cell reads. */
  label: string
  /** The Persona picker's option text. `null` = this persona is not offered in the picker. */
  option_label: string | null
  hint: string | null
  operator_pickable: boolean
  pipeline: boolean
  dispatchable: boolean
  charter_key: string | null
  /** A model TIER id (`app/model_catalog.py`), never a vendor model; label via `lib/models.ts::fmtTier`. */
  default_model: string | null
  mode: string | null
  delivery: string | null
  done_criterion: string | null
  terminal_types: string[]
  settle_states: string[]
  creation_shapes: string[]
  /** May a project's Default persona name it (`persona_registry.PROJECT_DEFAULT_PERSONAS`). */
  project_default: boolean
}

/** The payload itself: the roster plus the picker field's one caption. */
export type PersonaRegistryPayload = { personas: PersonaRecord[]; picker_hint: string }

/** A pair, because `option_label` is nullable on the wire and the picker's contract is that it is not. */
export type PersonaOption = { value: string; label: string }

/** Matches nothing, before the roster lands. Not a permissive `^\[\w+\]`: that would strip a legitimate
 *  `[ctrl-256]` prefix. No `g` flag, so the shared instance carries no `lastIndex`. */
const NEVER_MATCHES = /(?!)/

let roster: readonly PersonaRecord[] = []
let byName: ReadonlyMap<string, PersonaRecord> = new Map()
let options: readonly PersonaOption[] = []
let pickerHint = ''
/* Derived with the roster, never lazily (a lazy set would keep the pre-load answer). Empty before load. */
let systemPersonas: ReadonlySet<string> = new Set()
/* Rebuilt with the roster, never memoised on first call. */
let prefixRe: RegExp = NEVER_MATCHES

function escapeForPattern(literal: string): string {
  return literal.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Seed the cache (from `usePersonaRegistry` and tests). Never filter or reorder: the served order IS the vocabulary. */
export function setPersonaRegistry(rows: readonly PersonaRecord[], hint: string): void {
  roster = rows
  byName = new Map(rows.map(row => [row.name, row]))
  /* Picker options in served order. `operator_pickable` is the server's dispatchable-and-settleable set. A
     pickable persona with no `option_label` is not offered (never fall back to the board label). */
  options = rows
    .filter(row => row.operator_pickable && row.option_label)
    .map(row => ({ value: row.name, label: row.option_label as string }))
  pickerHint = hint
  /* `pipeline: false` IS the system-persona flag (producers whose rows are created hidden). */
  systemPersonas = new Set(rows.filter(row => !row.pipeline).map(row => row.name))
  /* `_create_epic_with_children` (apps/api routes/tasks/create.py) stores child titles as `[<persona>] <title>`;
     strip that at display. The alternation IS the live vocabulary (no legacy aliases exist).
     ESCAPED: the names are wire data, so regex-safety must not depend on today's vocabulary. */
  prefixRe = rows.length
    ? new RegExp(`^\\[(${rows.map(row => escapeForPattern(row.name)).join('|')})\\]\\s*`)
    : NEVER_MATCHES
}

/** `taskDefaults` needs it: "not in the vocabulary" and "no vocabulary yet" are different answers, and only
 *  the first justifies overriding a stored value. */
export function isPersonaRegistryLoaded(): boolean {
  return roster.length > 0
}

/** No null arm: the column is NOT NULL, and an unknown slug still reads as words (`business_analysis` →
 *  "Business analysis"). MECHANICAL — the miss arm must never become a second table. Reached before load and
 *  for personas newer than this bundle. */
export function personaLabel(persona: string): string {
  return byName.get(persona)?.label ?? humanize(persona)
}

/** What a card or detail names a row's role by: its playbook stage's label when it runs one
 *  (`PipelineTask.playbook_stage`), else the persona's. */
export function taskRoleLabel(task: { persona: string, playbook_stage?: { label: string } | null }): string {
  return task.playbook_stage?.label || personaLabel(task.persona)
}

/** Is this persona fleet machinery (rows created hidden) rather than a pipeline persona? Reads the served
 *  `pipeline` flag; `shared.tsx` re-exports this.
 *  FALSE BEFORE THE ROSTER LANDS: a missing badge for one paint beats badging a `coder` row wrongly. The
 *  real authority is the `hidden` COLUMN (`isHiddenSystemRow`). Never throws — this runs in render. */
export function isSystemPersona(persona: string): boolean {
  return systemPersonas.has(persona)
}

/** May a project's Default persona name it, as served; false before load. */
export function projectDefaultPersona(persona: string): boolean {
  return byName.get(persona)?.project_default === true
}

/** EMPTY before load; the picker then renders the stored value as its own option. */
export function personaOptions(): readonly PersonaOption[] {
  return options
}

/** As served. Empty before load. */
export function personaPickerHint(): string {
  return pickerHint
}

/** A function: the vocabulary is unknown at import time. */
export function rawPersonaPrefixRe(): RegExp {
  return prefixRe
}
