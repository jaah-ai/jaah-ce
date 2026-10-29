// Single-spelled persona values. A LEAF that imports nothing but types: `taskDefaults` needs
// `DEFAULT_PERSONA` at runtime and is reached from `pages/Tasks.tsx`, so importing from `TaskEditForm`
// put the editor tree (ProseMirror etc.) into the chunk `/tasks` loads unconditionally. No chunk-size
// guard exists, so keep this module a leaf — no React, no api, no component.
// The vocabulary itself (options, caption, labels) is served by `GET /api/tasks/personas`
// (`apps/api/app/persona_registry.py`) and cached in `./personaRegistry`; do not copy it here.

// The persona a chat carries: `autonomous=false`, settled only by the operator's Complete. Involvement
// `chat` at the `task` depth resolves to it (`TaskAxisControls`); every New Chat surface must name it.
export const ADVISOR_PERSONA = 'advisor'

/* Is this row a chat. A chat parks `awaiting_response` at every turn boundary; on it the `?` must open
   the console — it has no question to answer, and answering would dispatch a second runner into the
   live chat's slot. Used by `shared.tsx`'s `answerTargetId`, `rowActions.tsx`'s `consoleSlotFor` and the
   detail page's Answer tab. Structural parameter, not `PipelineTask`, to keep the leaf. */
export function isChatTask(task: { persona?: string | null }): boolean {
  return task.persona === ADVISOR_PERSONA
}

// The persona the `sketch` Depth position resolves (`personaForDepth`, `depthForPersona`, draft restore).
export const PROTOTYPER_PERSONA = 'prototyper'

// Not here: the chat mark (`./ChatGlyph`) and the picker caption (`PERSONA_PICKER_HINT`, read via
// `personaPickerHint()` in `./personaRegistry`).

// Is this `persona` an epic — the one spelling of the `epic` Depth position. `persona` is NOT NULL.
export function isEpicPersona(persona: string): boolean {
  return persona === 'epic'
}

// Non-epic fallback persona, shared by the picker default and the mode control's restore path.
export const DEFAULT_PERSONA = 'coder'
