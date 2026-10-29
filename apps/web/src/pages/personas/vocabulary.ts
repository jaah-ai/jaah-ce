/* ── The persona vocabularies, as people read them ───────────────────────────────────────────────────
 * Read by the persona page (`PersonaDetailPage.tsx`) and the roster's Delivery column (`PersonasTab.tsx`), so both
 * screens name a token alike. The closed sets are Python's: `persona_registry.py` (MODE_*, DELIVERY_*,
 * DONE_CRITERION_*), `task_manifest.py` (TERMINAL_TYPES, REQUIRED_*), `task_settle.py::TERMINAL_REFERENCES`. On the persona page every fact token renders through `Term`: its label first, the raw id beside
 * it in small mono (greppable), and a token no map knows renders as itself. */

export const MODE_LABEL: Readonly<Record<string, string>> = { build: 'Build', analysis: 'Analysis' }

export const DELIVERY_LABEL: Readonly<Record<string, string>> = {
  db: 'Control-plane record',
  pr: 'Pull request',
  outcome: 'Outcome report',
}

export const DONE_CRITERION_LABEL: Readonly<Record<string, string>> = {
  artifact: 'Artifact uploaded',
  artifact_with_coders: 'Artifact and coder tasks',
  merged_pr: 'Pull request merged',
  outcome: 'Outcome recorded',
  operator: 'Operator completes it',
}

export const TERMINAL_LABEL: Readonly<Record<string, string>> = {
  analysis: 'Analysis settled',
  coders: 'Coder tasks filed',
  merged_pr: 'Merged pull request',
  no_pr: 'No pull request',
  finish: 'Finished',
  outcome: 'Outcome reported',
}

export const REFERENCE_LABEL: Readonly<Record<string, string>> = {
  pr_url: 'PR link',
  no_pr_reason: 'Reason',
  no_pr_evidence: 'Evidence',
}

export const LICENCE_SHAPE_LABEL: Readonly<Record<string, string>> = {
  draft_top_level: 'Draft tasks',
  edit_filed: 'Edit its drafts',
  delete_filed: 'Delete its drafts',
}

/** A hand-over item's level (`task_manifest.py`): a HARD item's absence REFUSES the settle; an ADVISORY one's is
 *  accepted and recorded. */
export const MANIFEST_LEVEL_LABEL: Readonly<Record<string, string>> = { hard: 'Required', advisory: 'Advisory' }

export const MANIFEST_LEVEL_NOTE: Readonly<Record<string, string>> = {
  hard: 'Completion is refused without it.',
  advisory: 'Completion is accepted without it; the gap is recorded.',
}

/** What each licence shape lets the persona do. */
export const LICENCE_SHAPE_NOTE: Readonly<Record<string, string>> = {
  draft_top_level: 'Files new tasks as drafts; only an operator starts one.',
  edit_filed: 'Edits a draft it filed itself.',
  delete_filed: 'Deletes a draft it filed itself.',
}

/** A token's label, or the token itself when no map knows it (a newer server's). */
export const labelOf = (labels: Readonly<Record<string, string>>, id: string): string =>
  Object.hasOwn(labels, id) ? labels[id] : id
