import { useRef, type ComponentType } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import { get } from '../../api'
import { useAuth, useCan } from '../../auth'
import { useEventStream } from '../../hooks/useEventStream'
import { useLiveRefetch } from '../../hooks/useLiveRefetch'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import {
  COMPANY_SETUP_QUERY,
  isCompanySetupDismissed,
  newTaskHref,
  prepareRepoHref,
  withCompanySetupDismissed,
  withCompanySetupShown,
  type CompanySetupRequirement,
  type CompanySetupStepId,
  type CompanySetupStepStatus,
} from '../../lib/companySetup'
import {
  SETUP_STEP_DESTINATIONS,
  accountStepHref,
  isSignInNote,
  NODE_WAITING_NOTE,
  TASK_QUEUED_NOTE,
  WAITING_NOTE,
  WIZARD_STEP_TITLES,
  wizardSteps,
  type WizardStep,
  type WizardStepId,
  type WizardStepState,
} from '../../lib/welcomeWizard'
import { isEnabled } from '../../lib/editionFeatures'
import type { SetupChecklistStatus, SetupChecklistStep } from '../setupChecklist/SetupChecklist'
import type { GuideAnswer } from './guide'

/* THE COMPANY-SETUP GUIDE — the org owner's map of what a new tenant needs before its first task can run;
 * each row LINKS to the screen that does it, never re-hosting a form — except the plan, whose picker opens
 * in place. Drawn by the docked onboarding widget (`OnboardingWidget.tsx`) and counted by the sidebar's
 * Setup button: THE ONE PLACE SETUP IS DRAWN, for every org (`lib/welcomeWizard.ts::wizardSteps` folds
 * the server's list). The SERVER decides whether this viewer is the guide's audience (`visible`,
 * decision D2) and each step's status (`GET /api/org/setup`, `routes/org_setup.py`); nothing is guessed
 * client-side, so nothing is drawn until the answer lands. The ✕ HIDES, per user (`lib/companySetup.ts`).
 * The one step to do next — the first neither done nor waiting — is highlighted.
 * FINISHED: a guide this mount saw unfinished stays up once every step is done, in its done state (✓ Done),
 * for the rest of the load; one that ARRIVES finished (an established org) is never drawn.
 *
 * THE AUDIENCE ANSWER IS PART OF THE CONTRACT: a viewer this guide is for never gets "Get started" (operator
 * decision: that guide is no longer needed once the company is set up, and not for the same user), so the
 * hook answers `forViewer` even when it draws nothing — finished, dismissed, or a failed request.
 */

/** One step of `routes/org_setup.py::SetupStatus.steps`. */
/** The account and node a sign-in link names, as `accountLoginPath` takes them. */
export type AccountLogin = { slug: string; node_id: string }

export type CompanySetupStepStatusOut = {
  id: CompanySetupStepId
  status: CompanySetupStepStatus
  note: string | null
  /** `add_account`'s one sign-in, when one account needs it and one node is live (`routes/org_setup.py::LoginTarget`). */
  login?: AccountLogin | null
}

/** The wire shape of `routes/org_setup.py::SetupStatus`. */
export type CompanySetupStatus = {
  visible: boolean
  steps: CompanySetupStepStatusOut[] | null
  github?: { app: boolean; user_connected: boolean } | null
  project_id?: number | null
  /** The project `add_slots` links to: the org's first one, assigned or not. */
  assign_project_id?: number | null
  /** The org signed itself up (an `org_signups` row). */
  self_service?: boolean
  /** The org's onboarding latch is still open: setup is neither finished nor closed (`setupRefetchInterval`
   *  polls node-reported steps only while it is). */
  onboarding_open?: boolean
  /** The org's qualifying first task — the most recent `done` one, else the most recent (the `first_task`
   *  signal's criteria); null for none. */
  first_task?: CompanySetupFirstTask | null
  /** A task that step would count is filed and queued but not started yet. */
  task_queued?: boolean
}

/** `routes/org_setup.py::SetupStatus.first_task`: the org's qualifying first task, live. */
export type CompanySetupFirstTask = { id: number; title: string; state: string }

/** The status read, cached under `[COMPANY_SETUP_QUERY, admin.id]`. */
export const fetchCompanySetup = (): Promise<CompanySetupStatus> => get<CompanySetupStatus>('/api/org/setup')

/** The hook's answer: the usual guide answer, plus whether this viewer is the company guide's audience. */
export type CompanySetupAnswer = GuideAnswer & { forViewer: boolean }

const ASK_AN_ADMIN = 'Ask an admin'

/** The checklist's glyph for each step state. */
const STATUS_OF: Record<WizardStepState, SetupChecklistStatus> = {
  done: 'done',
  todo: 'todo',
  in_progress: 'in_progress',
  failed: 'failed',
  waiting: 'blocked',
}

/** The in-place plan picker's widest, in px. */
const PLAN_PICKER_MAX_WIDTH = 360
/* The plan step's picker (`components/PlanPicker.tsx`): billing's (`billing`), loaded where present — the community
   edition's tree leaves it out, and `import.meta.glob` yields `{}` for an absent file. */
const PlanPicker: ComponentType<{ canChoose: boolean }> | undefined = Object.values(isEnabled('billing')
  ? import.meta.glob<ComponentType<{ canChoose: boolean }>>('../PlanPicker.tsx', { eager: true, import: 'default' })
  : {})[0]

/** How often the status is re-asked while a step is under way (a plan's node coming up, approval landing) or
 *  waits on a node or a sign-in: the widget sits on screen, so such a step must tick over without a navigation. */
export const SETUP_POLL_MS = 10_000

/** The steps a node's own reports finish — it turning live, a sign-in on it — which no console event announces,
 *  so they are polled while open. */
const NODE_REPORTED_STEPS: readonly CompanySetupStepId[] = ['compute', 'add_compute', 'add_account']

/** The console events that can move a step: a node added, deleted or changing state; an account changed. */
const SETUP_EVENT_ENTITIES = ['node', 'account'] as const

/** `refetchInterval` for the status: poll while any step is `in_progress`, or — while onboarding is open — a
 *  node-reported step is open (not `blocked`: that one waits on approval, polled through its own `in_progress`
 *  step, or on the operator). Otherwise never: an established org with no node is a steady state, and progress
 *  made on another screen is picked up by the widget's refetch on navigation and on live events instead. */
export function setupRefetchInterval(status: CompanySetupStatus | undefined): number | false {
  const open = status?.steps?.some(step =>
    step.status === 'in_progress'
    || (status.onboarding_open === true && NODE_REPORTED_STEPS.includes(step.id)
      && step.status !== 'done' && step.status !== 'blocked'))
  return open ? SETUP_POLL_MS : false
}

/** `passive`: a second reader beside the widget (the Tasks board's setup diagram) — it shares the widget's cached
 *  answer and never re-asks itself (no poll, no focus or live refetch), so the status is asked once, not per mount. */
export function useCompanySetupGuide({ passive = false }: { passive?: boolean } = {}): CompanySetupAnswer {
  const { admin, updatePreferences } = useAuth()
  const can = useCan()

  /* ✕ HIDES, it does not end the guide: the stored flag keeps the widget away until the
     sidebar's Setup button shows it again, and the guide itself lives on until every step is done. */
  const hidden = isCompanySetupDismissed(admin?.preferences)
  /* Whether this mount has drawn the guide unfinished: only then is "all done" news worth a done state. The
     widget mounts once in the shell, so this survives navigation. */
  const sawUnfinished = useRef(false)

  const statusQuery = useQuery({
    queryKey: [COMPANY_SETUP_QUERY, admin?.id],
    queryFn: fetchCompanySetup,
    /* Asked while hidden too: the sidebar's Setup button shows the progress of a guide the ✕ put away. */
    enabled: Boolean(admin),
    retry: false,
    /* Progress changes on other screens. The widget mounts ONCE (the app shell), so a mount is not a page
       change: `OnboardingWidget` re-asks on every navigation, this re-asks on return to the tab, and a step
       under way is polled. `staleTime: 0` so each of those really asks. */
    staleTime: 0,
    refetchOnWindowFocus: !passive,
    refetchInterval: query => passive ? false : setupRefetchInterval(query.state.data),
  })
  /* A node added, deleted or turning live, an account changed: re-ask at once, not on the next page. */
  useLiveRefetch(
    useEventStream(admin && !passive ? '/api/stream' : null, SETUP_EVENT_ENTITIES),
    () => { void statusQuery.refetch() },
    null,
  )

  if (!admin) return { pending: false, forViewer: false, guide: null }
  /* NOTHING IS DRAWN UNTIL THE SERVER ANSWERS: a decoration must not guess, and Get started must not flash
     first for a viewer the company guide is for. */
  if (statusQuery.isPending) return { pending: true, forViewer: false, guide: null }
  const status = statusQuery.data
  /* A failed request with no answer ever: unknown audience, so NEITHER guide (`forViewer` keeps Get started
     away too). A failed REFETCH keeps the last answer on screen. */
  if (status === undefined) return { pending: false, forViewer: true, guide: null }
  if (!status.visible || !status.steps) return { pending: false, forViewer: false, guide: null }

  const serverSteps = status.steps
  const projectId = status.project_id ?? null
  const onboarded = serverSteps.some(s => s.id === 'onboard_repo' && s.status === 'done')
  /* `add_slots`: a project can take work. Read only to explain a First task whose project cannot run yet. */
  const hasProject = serverSteps.some(s => s.id === 'add_project' && s.status === 'done')
  const assigned = !hasProject || !serverSteps.some(s => s.id === 'add_slots' && s.status !== 'done')

  const holds = (requirement: CompanySetupRequirement): boolean =>
    requirement === 'org_admin' ? isOrgAdminSeat(admin) : can(requirement)

  /* Each row's destination for THIS viewer: a link where they can act, a note where they cannot. The row
     itself is never dropped, so every viewer counts the same steps. */
  const toRow = (step: WizardStep): SetupChecklistStep => {
    const id = step.id as Exclude<WizardStepId, 'done'>
    const done = step.state === 'done'
    const row: SetupChecklistStep = {
      id,
      label: WIZARD_STEP_TITLES[id],
      ...SETUP_STEP_DESTINATIONS[id],
      done,
      status: STATUS_OF[step.state],
    }
    /* A finished row links nowhere: its destination begins ANOTHER add. */
    if (done) return { ...row, to: '' }
    /* Waiting: never a link. On the node, no words either (operator decision): the dashed glyph and
       muted title say "not yet", and the open rows above are plainly the ones to do first. A wait the page shows
       no other way (approval, a plan to choose, a refused add) keeps its note. */
    if (step.state === 'waiting') {
      const nodeGate = step.note === NODE_WAITING_NOTE || step.note === TASK_QUEUED_NOTE
      return { ...row, note: nodeGate ? '' : step.note ?? WAITING_NOTE }
    }
    if (step.note !== null) row.statusNote = step.note
    /* A node coming up has nothing for the tenant to do; the note says where it stands. One that failed keeps its
       link: the steps after it wait on a node, so adding one is the way on. */
    if (step.state === 'in_progress') return { ...row, to: '' }
    switch (id) {
      case 'forge':
        /* Any org admin's connection on any forge finishes it, and a token can be pasted for any
           forge without the operator's GitHub app, so an org admin always goes. */
        return isOrgAdminSeat(admin) ? row : { ...row, note: 'Ask an admin to connect Git' }
      case 'plan':
        if (!holds('org_admin')) return { ...row, note: ASK_AN_ADMIN }
        /* The plan brings the node: chosen in place. */
        if (!PlanPicker) return row
        return { ...row, expand: <Box sx={{ maxWidth: PLAN_PICKER_MAX_WIDTH }}><PlanPicker canChoose /></Box> }
      case 'account':
        if (!holds('org_admin')) return { ...row, note: ASK_AN_ADMIN }
        /* Added but signed in nowhere (the server's note names which): straight to its sign-in when there is one. */
        return isSignInNote(step.note) ? { ...row, to: accountStepHref(step), cta: 'Sign in' } : row
      case 'task':
        if (!holds('tasks:create')) return { ...row, note: ASK_AN_ADMIN }
        /* A project exists but none can run yet: assignment is usually automatic, so say what is missing. */
        if (projectId === null) return assigned ? row : { ...row, statusNote: 'Assign nodes first', to: '/projects', cta: 'Projects' }
        /* The repo is prepared first while it still needs it. */
        return onboarded
          ? { ...row, to: newTaskHref(projectId) }
          : { ...row, to: prepareRepoHref(projectId), cta: 'Prepare repo', statusNote: 'Prepare the repo first' }
      default:
        return holds('org_admin') ? row : { ...row, note: ASK_AN_ADMIN }
    }
  }

  const steps = wizardSteps(status).filter(step => step.id !== 'done').map(toRow)
  /* Arrived finished: hidden — still the audience, so still no Get started. Finished while watched: the
     done state (and the celebration), until dismissed or the next load. */
  if (steps.every(step => step.done)) {
    if (!sawUnfinished.current) return { pending: false, forViewer: true, guide: null }
  } else {
    sawUnfinished.current = true
  }

  return {
    pending: false,
    forViewer: true,
    guide: {
      title: 'Set up your company',
      testIdPrefix: 'company-setup',
      dismissLabel: 'Hide the Set up your company guide',
      steps,
      highlightNext: true,
      labelLinks: true,
      startOpen: true,
      hidden,
      onDismiss: () => updatePreferences(withCompanySetupDismissed()),
      onShow: () => updatePreferences(withCompanySetupShown()),
    },
  }
}
