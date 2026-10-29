import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { get } from '../../api'
import { ACCOUNTS_KEY, listAccounts } from '../../pages/credentials/api'
import { useAuth, useCan } from '../../auth'
import {
  CREATE_FIRST_TASK_STEP,
  isOnboardingDismissed,
  ONBOARDING_STEPS,
  readOnboardingStepsDone,
  withOnboardingDismissed,
  withOnboardingShown,
  withOnboardingStepDone,
  type OnboardingStep,
} from '../../lib/onboarding'
import type { GuideAnswer } from './guide'

/* THE NEW-USER GUIDE. A new user cannot tell which page is MANDATORY: without a connected GitHub credential
 * their runners cannot push. Minimum words: a segmented meter plus rows of a glyph and a 2–4 word label that
 * is itself the link to the step's page (drawn by `setupChecklist/SetupChecklist.tsx` inside `OnboardingWidget.tsx`).
 * IT LINKS, IT DOES NOT DO — each step's work has one home (`/profile` Git and Settings tabs, the
 * new-task form); re-hosting any would drift. A MAP, never a workshop.
 * The ✕ HIDES it, stored in `admin.preferences` (cross-device — see `lib/onboarding.ts`), and the sidebar's
 * Setup button shows it again; it ends for good on completion — after a done state (and the celebration) for
 * the rest of the load when this mount watched it finish. It never lies about progress: GitHub reads `git_credential_id`, first-task asks
 * the server — and once the server has said yes, that answer is recorded and the probe ends.
 * Per-step reasoning: `lib/onboarding.ts`.
 * `enabled` is the widget's: false for a viewer the company guide is for (who never gets this one), and
 * while that verdict is still out — so the own-task probe is never paid for a guide that will not draw.
 */

/** ONE row: the question is existence, reduced to a boolean in the query function. `include_hidden` and
 *  `include_archived` are ON (see `ownTaskQuery`): a hidden or archived task was still created. */
const OWN_TASK_PROBE_LIMIT = 1

/** The own-task probe's key head (the widget re-asks it on navigation until it answers yes; `queryClient.ts` never
 *  persists it). */
export const OWN_TASKS_QUERY = 'onboarding-own-tasks'

/* The user ids whose yes THIS PAGE LOAD already sent for recording. Module scope, not a ref: the shell
 * mounts this hook once per surface that shows guide progress (the widget, the Setup button, its badge, the sidebar's
 * folded line), and a per-mount guard sent one identical `PUT /api/auth/me` per mount — four to six a load, and as
 * many again on every load where the write is refused. A refused write still only means the next load asks again. */
const recordSentThisLoad = new Set<string>()

/** Test seam: forget what this load sent, so each test starts as a fresh load. */
export function resetRecordSentForTests(): void {
  recordSentThisLoad.clear()
}

export function useGetStartedGuide(enabled: boolean): GuideAnswer {
  const { admin, updatePreferences } = useAuth()
  const can = useCan()

  const hidden = isOnboardingDismissed(admin?.preferences)
  /* Whether this mount drew it unfinished: only then is "all done" worth a done state. */
  const sawUnfinished = useRef(false)
  const recorded = readOnboardingStepsDone(admin?.preferences)
  const firstTaskRecorded = recorded.has(CREATE_FIRST_TASK_STEP)

  /* Steps gated behind a key the operator lacks are DROPPED, not disabled (e.g. `/tasks/new` needs
     `tasks:create`); the meter's denominator stays honest. */
  const steps = ONBOARDING_STEPS.filter(step => !step.requires || can(step.requires))

  /* HAS THIS OPERATOR EVER CREATED A TASK — asked while the guide could still count (hidden too: the sidebar's
     Setup button shows its progress), never retried
     (a decoration must not hammer the API); failure reads as "not created", the safe direction.
     GATED ON THE STEP SURVIVING THE FILTER: otherwise an operator without `tasks:create` pays for the
     request and the hold below withholds the guide on a slow `/api/tasks`.
     ENDS ONCE RECORDED: `staleTime: 0` re-asked on every navigation forever, long after the user's
     first task. A yes is recorded below and never turns false, so a recorded step needs no question; the whole
     guide being finished needs none either, since a finished guide's first-task step is recorded. */
  const wantsOwnTaskProbe =
    enabled && Boolean(admin) && !firstTaskRecorded && steps.some(step => step.id === CREATE_FIRST_TASK_STEP)
  /* HAS THIS ORG AN AI ACCOUNT — the roster the Accounts page reads (same key, so one fetch serves both). Asked
     only while the step is drawn; a failed read counts as "none", the safe direction. */
  const wantsAccounts = enabled && Boolean(admin) && steps.some(step => step.id === 'connect_account')
  const accountsQuery = useQuery({ queryKey: ACCOUNTS_KEY, queryFn: listAccounts, enabled: wantsAccounts, retry: false })
  const ownTaskQuery = useQuery({
    queryKey: [OWN_TASKS_QUERY, admin?.id],
    /* Reduced to a boolean INSIDE the query function, not a `select`, so the cache never holds full task
       rows. This key is excluded from persistence (`queryClient.ts`'s `shouldPersistQuery`). */
    queryFn: async () => {
      const rows = await get<{ id: number }[]>(
        `/api/tasks?created_by_id=${encodeURIComponent(admin?.id ?? '')}` +
          `&limit=${OWN_TASK_PROBE_LIMIT}&include_hidden=true&include_archived=true`,
      )
      return rows.length > 0
    },
    enabled: wantsOwnTaskProbe,
    retry: false,
    /* `staleTime: 0`: a stale "no" (default 15s) would show the step outstanding right after creating it. */
    staleTime: 0,
  })

  /* RECORD THE YES — only an answered yes, never a guess, so the record cannot lie. Merged against
     the current `preferences` (`withOnboardingStepDone`'s contract); a refused write only means the probe asks
     again on the next load. */
  const probeSaidYes = ownTaskQuery.data === true
  const adminId = admin?.id ?? null
  useEffect(() => {
    if (!probeSaidYes || firstTaskRecorded || adminId === null || recordSentThisLoad.has(adminId)) return
    recordSentThisLoad.add(adminId)
    void updatePreferences(prefs => withOnboardingStepDone(prefs, CREATE_FIRST_TASK_STEP))
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `admin.preferences` is read at send time only
  }, [probeSaidYes, firstTaskRecorded, adminId, updatePreferences])

  if (!enabled || !admin) return { pending: false, guide: null }

  /* NOTHING IS DRAWN UNTIL EVERY STEP CAN BE ANSWERED — otherwise the guide flashes and auto-hides.
     THE HOLD ONLY HOLDS BECAUSE THIS KEY IS NEVER PERSISTED: a rehydrated entry is `isPending: false` on
     first paint, so the `queryClient.ts` exclusion is part of this rule. */
  if ((wantsOwnTaskProbe && ownTaskQuery.isPending) || (wantsAccounts && accountsQuery.isPending)) {
    return { pending: true, guide: null }
  }

  const hasOwnTask = firstTaskRecorded || probeSaidYes

  /* PER-STEP COMPLETION — derived first, recorded only where nothing can be derived. `choose_theme` also
     accepts any stored `theme`: an unpicked theme is stored absent (the operator's default applies), so a
     stored one was chosen. */
  const isDone = (step: OnboardingStep): boolean => {
    switch (step.id) {
      case 'connect_account':
        return (accountsQuery.data?.length ?? 0) > 0
      case 'connect_github':
        return Boolean(admin.git_credential_id)
      case 'create_first_task':
        return hasOwnTask
      case 'choose_theme':
        return recorded.has(step.id) || admin.preferences?.theme === 'dark' || admin.preferences?.theme === 'light'
    }
  }

  /* Arrived finished: nothing. Finished while watched: the done state. `steps` is never empty (Git and theme
     rows are ungated). */
  if (steps.every(isDone)) {
    if (!sawUnfinished.current) return { pending: false, guide: null }
  } else if ((!wantsOwnTaskProbe || ownTaskQuery.isSuccess) && (!wantsAccounts || accountsQuery.isSuccess)) {
    /* Only an ANSWERED "not yet" counts: a failed probe reads as "not created", and a later success would
       otherwise celebrate a step finished long ago. */
    sawUnfinished.current = true
  }

  return {
    pending: false,
    guide: {
      title: 'Get started',
      testIdPrefix: 'get-started',
      dismissLabel: 'Hide the Get started guide',
      steps: steps.map(step => ({ ...step, done: isDone(step) })),
      highlightNext: false,
      labelLinks: true,
      hidden,
      onDismiss: () => updatePreferences(withOnboardingDismissed()),
      onShow: () => updatePreferences(withOnboardingShown()),
    },
  }
}
