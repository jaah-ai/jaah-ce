// Persistence for the ONE LINE typed into the `/tasks` board's inline create row, so any navigation
// (the row unmounts; there is no router blocker) finds it still there. Pure of React, like
// `taskFilterStorage.ts`. The full task editor auto-saves to the server instead
// (`lib/taskAutosaveInterval.ts`).
//
// `sessionStorage`, NOT `localStorage`: a restore puts words back on screen, and a week-old sentence in
// a fresh tab would come from nowhere. Accepted: a draft does not follow the operator to another tab, and
// closing the tab discards it.

import { readJson, readJsonObject, removeKey, writeJson } from './safeStorage'

/** The prefix every key shares, spelled once so `clearAllTaskDrafts` sweeps exactly what is built. */
const DRAFT_KEY_PREFIX = 'cockpit.tasks.draft.'

/**
 * The key the inline create text is stored under, scoped to the logged-in admin's id.
 *
 * ONE key for the create row's draft (`useInlineTaskCreate`, drawn by `FeedNewTaskRow`).
 *
 * SCOPED because a browser profile can be shared: inheriting ANOTHER PERSON'S PROSE would put their task
 * one Start press away from being filed under this operator's name. `null` gets its own `anon` bucket.
 */
export function inlineTaskDraftStorageKey(adminId: string | null): string {
  return `${DRAFT_KEY_PREFIX}inline:${adminId ?? 'anon'}`
}

/**
 * The text the inline create row was left holding, or `''` (the empty box). The caller
 * (`useInlineTaskCreate`) clears the key rather than storing `''`; both read back the same.
 */
export function loadInlineTaskDraft(key: string): string {
  const stored = readJson('session', key)
  return typeof stored === 'string' ? stored : ''
}

/**
 * Persist the inline create row's text. Returns whether it landed; failing to REMEMBER must never cost
 * the operator the sentence itself.
 */
export function saveInlineTaskDraft(key: string, text: string): boolean {
  return writeJson('session', key, text)
}

/**
 * Forget EVERY draft in this tab, whoever wrote it — on an identity change (`auth.tsx`), by the same rule
 * that clears the react-query cache; unsaved prose is more sensitive than cached rows.
 *
 * PREFIX-SWEPT: by then the leaving admin's id can no longer build its key, and older builds' buckets are
 * swept too.
 */
export function clearAllTaskDrafts(): void {
  try {
    // Snapshot the names first: `sessionStorage` is live, so removing while walking skips entries.
    for (const key of Object.keys(sessionStorage)) {
      if (key.startsWith(DRAFT_KEY_PREFIX)) sessionStorage.removeItem(key)
    }
  } catch {
    /* A store we cannot enumerate is a store nothing was written to. */
  }
}

/**
 * Forget one draft. A draft ends when the box is emptied, when ⤢ moves it into the full editor, or when
 * a create lands on that editor (`components/tasks/useInlineTaskCreate`'s `expandTyped`,
 * `pages/taskDetail/TaskCreateView`'s `onSaved`); a cleared key must read back like one never written.
 */
export function clearTaskDraft(key: string): void {
  removeKey('session', key)
  /* A row still on screen (the board beside an editor that took its words: ＋, a create's Save/Go) drops them
     too — `useInlineTaskCreate` listens. Storage events reach only OTHER tabs, hence an event of our own. */
  try {
    window.dispatchEvent(new CustomEvent(TASK_DRAFT_CLEARED, { detail: key }))
  } catch {
    /* No window (a non-DOM caller): nothing on screen to tell. */
  }
}

/** The window event `clearTaskDraft` fires, its `detail` the cleared key. */
export const TASK_DRAFT_CLEARED = 'jaah:task-draft-cleared'

/**
 * A create row's press held for an Act as (`useInlineTaskCreate`): the operator pressed ▶ on another org's
 * project, was asked to act as that org first, and the press goes through once this tab acts as it. Beside the
 * draft (same admin scoping, same prefix, so `clearAllTaskDrafts` sweeps it): the words stay in the draft; this
 * records only where and how they were to be filed.
 */
export type PendingInlineCreate = { projectId: number; intent: 'start' | 'chat'; orgId: string; armed?: boolean }

/* A held press is an answer to ONE question: unanswered for this long, it lapses — a later Act as taken for
   another reason never files it by surprise. */
const PENDING_TTL_MS = 2 * 60_000

const pendingKey = (draftKey: string) => `${draftKey}:pending`

export function loadPendingInlineCreate(draftKey: string): PendingInlineCreate | null {
  const stored = readJsonObject('session', pendingKey(draftKey))
  const fresh = typeof stored?.at === 'number' && Date.now() - stored.at < PENDING_TTL_MS
  return stored && fresh && typeof stored.projectId === 'number' && (stored.intent === 'start' || stored.intent === 'chat')
    && typeof stored.orgId === 'string'
    ? { projectId: stored.projectId, intent: stored.intent, orgId: stored.orgId, armed: stored.armed === true }
    : null
}

/** The operator answered the question with this row's Act as: the held press may now go through. */
export function armPendingInlineCreate(draftKey: string): void {
  const pending = loadPendingInlineCreate(draftKey)
  if (pending !== null) writeJson('session', pendingKey(draftKey), { ...pending, armed: true, at: Date.now() })
}

export function savePendingInlineCreate(draftKey: string, pending: PendingInlineCreate): void {
  writeJson('session', pendingKey(draftKey), { ...pending, armed: false, at: Date.now() })
}

export function clearPendingInlineCreate(draftKey: string): void {
  removeKey('session', pendingKey(draftKey))
}
