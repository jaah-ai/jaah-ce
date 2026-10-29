import { useAuth } from '../../auth'
import { usePlanSwitch } from '../../lib/planSwitches'
import type { TaskCreateDefaults } from '../../types'
// The picker's vocabulary comes from the served registry (`personaRegistry.ts`); a LEAF like
// `taskPersona`, keeping this module leaf-only.
import { isPersonaRegistryLoaded, personaOptions } from './personaRegistry'
import { DEFAULT_PERSONA } from './taskPersona'

// ── What a NEW task's parameters START AT ────────────────────────────────────────────────────
// Operator settings `task_default_autonomous`, `task_default_complex`, `task_default_persona`
// (Settings, "Tasks" › "Defaults") seed the `/tasks` CREATE surfaces; this module is the one place turning
// the server's answer into a form seed, so the full editor and the inline row agree.
//
// The answer arrives on the IDENTITY payload (`admin.task_create_defaults`, resolved by
// `operator_settings.defaults.task_create_defaults()` on `GET /api/auth/me` and siblings) — not `GET /api/settings`,
// which is `system:view`-gated and would fail OPEN for a `tasks:create` holder (autonomy showing ON after
// the operator set it OFF). `operator_settings.catalog.CATALOG` is the only place the keys are spelled.
//
// A PREFILL, nothing else:
//   · server-side creation paths are untouched by these org knobs (plan fan-out children, compliance/janitor
//     sweeps, the agent-token route) — a "New task" knob must not re-tune the fleet's machinery. A project's own
//     `default_persona` (`withProjectPersona`) is the exception, reaching unnamed agent filings and plan children;
//   · operator-created tasks only (the two create surfaces);
//   · EDIT MODE NEVER READS IT (it would overwrite stored answers).

export type { TaskCreateDefaults }

// Today's behaviour as a value, for anyone the payload has not answered for yet. `persona` comes from the
// picker's own single home; `autonomous` ON per R4; a create is not an epic.
export const TASK_CREATE_DEFAULT_FALLBACKS: TaskCreateDefaults = {
  autonomous: true,
  complex: false,
  persona: DEFAULT_PERSONA,
}

// A boolean the server sent, else today's behaviour — typed, not coerced (`Boolean('false')` is `true`). A
// non-boolean means this file and `TaskCreateDefaultsOut` disagree about the shape.
function boolOr(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback
}

// The payload's three values, fenced PER FIELD: one renamed field must not cost the other two.
export function resolveTaskCreateDefaults(
  raw: Partial<TaskCreateDefaults> | null | undefined,
): TaskCreateDefaults {
  if (!raw) return TASK_CREATE_DEFAULT_FALLBACKS
  return {
    autonomous: boolOr(raw.autonomous, TASK_CREATE_DEFAULT_FALLBACKS.autonomous),
    complex: boolOr(raw.complex, TASK_CREATE_DEFAULT_FALLBACKS.complex),
    /* Checked against the PICKER's vocabulary: a persona outside it cannot be seen or corrected, and the
       pickable set is what dispatches AND SETTLES (else a row can wedge `active`). The server's CLOSED
       `choices` are a subset, so a server-written value always passes. */
    persona: resolvePersona(raw.persona),
  }
}

/* The target project's own `default_persona` over the org's, fenced like the org's. `epic` and the advisor are not
   project defaults (`persona_registry.PROJECT_DEFAULT_PERSONAS`), so the org's "Epic" default still outranks it. */
export function withProjectPersona(
  defaults: TaskCreateDefaults,
  projectPersona: string | null | undefined,
): TaskCreateDefaults {
  return projectPersona ? { ...defaults, persona: resolvePersona(projectPersona) } : defaults
}

/* `project_memory_default`, for the editor's "Default (On)" option — DISPLAY ONLY, never a prefill: a row left on
   Default stays null and is resolved at dispatch (`app/project_memory/switch.py`), in edit mode too. The
   knob ships On, hence the fallback. */
export function resolveProjectMemoryDefault(raw: Partial<TaskCreateDefaults> | null | undefined): boolean {
  return boolOr(raw?.project_memory, true)
}

/* The persona knob, fenced against the SERVED picker vocabulary (`GET /api/tasks/personas`). "Not in the
   vocabulary" differs from "vocabulary not loaded yet": before the roster lands, the server's CLOSED
   `choices` are the fence (else everyone gets `coder` in that window — a fail-open). Loaded, membership
   is the fence. A non-string or empty value is refused either way. */
function resolvePersona(raw: unknown): string {
  if (typeof raw !== 'string' || !raw) return TASK_CREATE_DEFAULT_FALLBACKS.persona
  if (!isPersonaRegistryLoaded()) return raw
  return personaOptions().some(o => o.value === raw) ? raw : TASK_CREATE_DEFAULT_FALLBACKS.persona
}

// The create surfaces' read of the operator's defaults from the identity payload, fenced by the resolver;
// no request of its own (`AuthProvider` already made it).
// `loaded` means "STOP WAITING" (`resolving` inverted), so a principal whose `/api/auth/me` was refused
// still seeds. It stays false while the first `me` is in flight — even with a persisted snapshot showing,
// since a stale snapshot may predate the operator's last edit.
// `enabled`: `TaskDetailPage` hosts both create and edit and cannot call a hook conditionally; disabled,
// `loaded` stays false ("nothing to seed from").
export function useTaskCreateDefaults(enabled = true): { defaults: TaskCreateDefaults; loaded: boolean } {
  const { admin, resolving } = useAuth()
  return {
    defaults: resolveTaskCreateDefaults(admin?.task_create_defaults),
    loaded: enabled && !resolving,
  }
}

// The org's Playbooks plan switch (`lib/planSwitches.ts`). Gates only the OFFER of the Playbook depth; the
// server re-checks every write that names a playbook.
export function usePlaybooksEnabled(): boolean {
  return usePlanSwitch('playbooks_enabled')
}
