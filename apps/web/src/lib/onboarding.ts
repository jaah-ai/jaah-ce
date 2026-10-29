// The new-user guide's steps and stored flags; `components/onboarding/getStartedGuide.ts` words
// them and `components/onboarding/OnboardingWidget.tsx` draws them. React-free.
// Completion is derived wherever a real signal exists: GitHub from `admin.git_credential_id`, an AI account
// from the account roster, first task from the server. `choose_theme` is recorded (the wire returns full defaults, so "never chose" is
// unobservable), on any save of the Settings card — deliberately, so keeping the default counts.
// `create_first_task` is recorded too, but only AFTER the server answered yes: "ever created" never
// turns false, so the record ends the probe rather than re-asking `/api/tasks` on every navigation forever.
// The flags are side effects, not settings: absent from `UserSettingsDraft`, written via `auth.tsx`'s
// `updatePreferences`. Stored server-side so dismissal follows the user across devices. Keys must match
// `app/preferences.py`'s `UserPreferences` (`extra="forbid"` 422s an unknown key) — rename both together.

/** The stored key holding "this operator hid the Get started card" (its ✕). Mirrors `UserPreferences.onboarding_dismissed`. */
export const ONBOARDING_DISMISSED_PREF = 'onboarding_dismissed'

/** The stored key holding the completed step ids. Mirrors `UserPreferences.onboarding_steps_done`. */
export const ONBOARDING_STEPS_DONE_PREF = 'onboarding_steps_done'

/** Step ids are frontend vocabulary; the server stores opaque strings, so a new step needs no API deploy. */
export type OnboardingStepId = 'connect_account' | 'connect_github' | 'choose_theme' | 'create_first_task'

/** Every id this file records: the guide's rows plus flags that are not rows. */
export type RecordedStepId = OnboardingStepId | 'name_prompt_dismissed'

/**
 * The one step recorded rather than derived (written by `pages/ProfilePage.tsx`'s settings save). A
 * named constant so recorder and reader cannot drift.
 */
export const CHOOSE_THEME_STEP: OnboardingStepId = 'choose_theme'

/**
 * Recorded by `components/onboarding/getStartedGuide.ts` once its own-task probe answers yes — never before, so the
 * record cannot lie — and read there to stop probing.
 */
export const CREATE_FIRST_TASK_STEP: OnboardingStepId = 'create_first_task'

/**
 * Recorded when a nameless viewer closes the name prompt (`components/NamePromptBanner.tsx`), so it asks once.
 * Not a guide row: absent from `ONBOARDING_STEPS`.
 */
export const NAME_PROMPT_DISMISSED_STEP: RecordedStepId = 'name_prompt_dismissed'

export type OnboardingStep = {
  id: OnboardingStepId
  /** Two to four words. */
  label: string
  /** Where the step is done; the card links there rather than duplicating the affordance. */
  to: string
  /** Link text naming the destination, one or two words. */
  cta: string
  /** Permission the destination needs. An unreachable step is dropped, not drawn dead. `undefined` means
   *  ungated (`/profile` is unguarded in `App.tsx`). */
  requires?: 'tasks:create' | 'accounts:view'
}

/**
 * The steps in draw order: by cost of getting wrong. An AI account first (without one no task runs), GitHub
 * second (its absence breaks pushes), theme third (a cheap win), first task last.
 */
export const ONBOARDING_STEPS: readonly OnboardingStep[] = [
  { id: 'connect_account', label: 'Add an AI account', to: '/accounts/new', cta: 'Accounts', requires: 'accounts:view' },
  { id: 'connect_github', label: 'Connect Git', to: '/profile?tab=git-accounts', cta: 'Git' },
  { id: 'choose_theme', label: 'Choose theme', to: '/profile?tab=settings', cta: 'Settings' },
  { id: 'create_first_task', label: 'Create your first task', to: '/tasks/new', cta: 'New task', requires: 'tasks:create' },
]

/**
 * Has this operator hidden the card (its ✕)? Only a literal `true` counts — the safe direction.
 */
export function isOnboardingDismissed(preferences: Record<string, unknown> | null | undefined): boolean {
  return preferences?.[ONBOARDING_DISMISSED_PREF] === true
}

/**
 * Recorded step ids. Tolerates any stored shape (browser-written JSONB, replayed snapshot); junk reads as
 * nothing recorded.
 */
export function readOnboardingStepsDone(
  preferences: Record<string, unknown> | null | undefined,
): Set<string> {
  const raw = preferences?.[ONBOARDING_STEPS_DONE_PREF]
  return new Set(Array.isArray(raw) ? raw.filter((id): id is string => typeof id === 'string') : [])
}

/**
 * Partial preferences patch recording `step` — merge it, never send it alone: `updatePreferences`
 * merges then PUTs, and `PUT /api/auth/me` replaces `preferences` wholesale. Deduped, since every
 * Settings save calls it.
 */
export function withOnboardingStepDone(
  preferences: Record<string, unknown> | null | undefined,
  step: RecordedStepId,
): Record<string, unknown> {
  const done = readOnboardingStepsDone(preferences)
  done.add(step)
  return { [ONBOARDING_STEPS_DONE_PREF]: [...done] }
}

/** The patch that hides the card (its ✕) until the sidebar's Setup button shows it again. Same merge contract
 *  as `withOnboardingStepDone`. */
export function withOnboardingDismissed(): Record<string, unknown> {
  return { [ONBOARDING_DISMISSED_PREF]: true }
}

/** The patch that shows a hidden card again (the sidebar's Setup button). Same merge contract. */
export function withOnboardingShown(): Record<string, unknown> {
  return { [ONBOARDING_DISMISSED_PREF]: false }
}

/* THE NAME PROMPT'S LOCAL COPY. The server record is the cross-device one, but it is a shared
   `onboarding_steps_done` list that any writer holding an older copy of `preferences` replaces wholesale (another
   tab's Settings save, a concurrent write from this one), and a refused write is rolled back — either brought the
   closed prompt back. This per-user, per-browser flag makes the ✕ stick here regardless. Storage can throw
   (private mode, blocked site data): that reads as "not closed here", leaving the server record to answer. */
const namePromptKey = (userId: string): string => `jaah.namePromptDismissed.${userId}`

/** Has this user closed the name prompt in this browser? */
export function isNamePromptDismissedLocally(userId: string): boolean {
  try {
    return window.localStorage.getItem(namePromptKey(userId)) === '1'
  } catch {
    return false
  }
}

/** Remember this user's close of the name prompt in this browser. Best effort. */
export function rememberNamePromptDismissed(userId: string): void {
  try {
    window.localStorage.setItem(namePromptKey(userId), '1')
  } catch {
    /* Storage unavailable: the server record still carries it. */
  }
}
