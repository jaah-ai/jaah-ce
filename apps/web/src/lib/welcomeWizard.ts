// THE ONE SETUP STEP LIST — every org's, every surface's. The docked onboarding checklist
// (`components/onboarding/companySetupGuide.tsx`, drawn by `components/onboarding/OnboardingWidget.tsx`) and the
// sidebar's Setup button (`components/onboarding/SetupNavButton.tsx`) both draw THESE steps and count them the same
// way (one onboarding, one count) — Choose plan -> Add node -> AI account -> Connect Git -> Add project ->
// First task, then Done. Choose plan is a self-service org's only: an operator-created org has no plan
// step, so it counts five. React-free.
// It folds the server's list (`GET /api/org/setup`: seven steps for an operator-created org, nine for a
// self-service one) and decides nothing the server has not: Assign nodes is automatic (onboarding assigns it),
// Prepare repo rides First task, and First task is done once one has started (the server's `first_task` step).
// THE NODE GATE: every step after Add node waits until the node is ready — a project's create starts its
// onboarding task, and that task, an account's sign-in and the first task all need a node. While the org awaits
// the operator's approval (D8) every step after Choose plan waits on it instead.
// Nothing here is stored: the checklist's own hide is `lib/companySetup.ts`'s (`company_setup_dismissed`).
// THE COMMUNITY EDITION counts neither Choose plan (no billing) nor Add node (its one node is the install's, never
// added); the node still gates the steps after it, and its link is This machine's Settings area.

import type { AccountLogin, CompanySetupStatus, CompanySetupStepStatusOut } from '../components/onboarding/companySetupGuide'
import { accountLoginPath } from '../pages/credentials/accountModel'
import { NODES_ADD_PATH } from '../components/addNode/deepLink'
import { isEnabled } from './editionFeatures'
import { GIT_ADD_PATH } from './gitAddLink'
import { PLAN_PAGE, type CompanySetupStepId } from './companySetup'

export type WizardStepId = 'plan' | 'node' | 'account' | 'forge' | 'project' | 'task' | 'done'

/** `waiting`: the step offers nothing yet — the org awaits approval, or the node is not ready; its note says which. */
export type WizardStepState = 'done' | 'todo' | 'in_progress' | 'failed' | 'waiting'

export type WizardStep = {
  id: WizardStepId
  state: WizardStepState
  /** Where the step stands, in words (an included node's error, what a waiting step waits on). */
  note: string | null
  /** The note is the server's refusal to add a node, which only an operator clears: the step offers support. */
  refused?: true
  /** AI account's one sign-in to do (the server's `login`): the step links straight to it. */
  signIn?: AccountLogin
}

/** Each step's title, on every surface that draws the list. */
export const WIZARD_STEP_TITLES: Record<WizardStepId, string> = {
  plan: 'Choose plan',
  node: 'Add compute',
  account: 'AI account',
  forge: 'Connect Git',
  project: 'Add a project',
  task: 'First task',
  done: 'Done',
}

/** Each counted step's link, where the checklist sends its viewer: the screen where the adding BEGINS (a dialog or
 *  form already open, never a list to find the button on), and the link's one or two words. Refined per viewer by
 *  `components/onboarding/companySetupGuide.tsx`. */
export const SETUP_STEP_DESTINATIONS: Record<Exclude<WizardStepId, 'done'>, { to: string; cta: string }> = {
  plan: { to: PLAN_PAGE, cta: 'Choose' },
  node: !isEnabled('cloud_nodes') ? { to: '/settings?area=this-machine', cta: 'This machine' } : { to: NODES_ADD_PATH, cta: 'Nodes' },
  account: { to: '/accounts/new', cta: 'Accounts' },
  /* My Profile's Git tab (`pages/ProfilePage.tsx`'s `git-accounts`), its Add Git account form open. */
  forge: { to: GIT_ADD_PATH, cta: 'Git' },
  project: { to: '/projects/new', cta: 'New project' },
  task: { to: '/tasks/new', cta: 'New task' },
}

/** A step waiting on the operator's approval, in words — on every surface. */
export const WAITING_NOTE = 'Waiting for approval'

/** A step waiting on the node (the node gate), in words. */
export const NODE_WAITING_NOTE = 'Waiting for your node'

/** First task's words under the node gate once a task is already filed and a node is coming or still to add: it
 *  counted, and starts on its own. Not for a failed or refused node, which no task waits out. */
export const TASK_QUEUED_NOTE = 'Filed. Starts once your node is ready'

/** How AI account's note starts while accounts are added but signed in on no live node, before their names
 *  (`routes/org_setup.py::SIGN_IN_PREFIX`). */
export const SIGN_IN_PREFIX = 'Sign in: '

/** The note asks for a sign-in (`SIGN_IN_PREFIX`). */
export function isSignInNote(note: string | null): boolean {
  return note?.startsWith(SIGN_IN_PREFIX) === true
}

/** Where AI account's sign-in link goes: the one sign-in the server named, on its node, else the Accounts list
 *  (several to sign in: each is signed in from its own row). */
export function accountStepHref(step: WizardStep): string {
  return step.signIn ? accountLoginPath(step.signIn.slug, step.signIn.node_id, true) : '/accounts'
}

/** Add node's words while the plan that brings it is unchosen. */
export const PLAN_WAITING_NOTE = 'Choose a plan first'

/** The org still awaits the operator's approval: its approval step is not done. An org with no approval
 *  step (an operator-created one) was never pending. */
export function isAwaitingApproval(status: CompanySetupStatus): boolean {
  const approval = status.steps?.find(step => step.id === 'approval')
  return approval !== undefined && approval.status !== 'done'
}

/** The steps in order, each in its state: Choose plan only where the server sends it, then Done. */
export function wizardSteps(status: CompanySetupStatus): WizardStep[] {
  const server = new Map<CompanySetupStepId, CompanySetupStepStatusOut>((status.steps ?? []).map(s => [s.id, s]))
  const isDone = (id: CompanySetupStepId) => server.get(id)?.status === 'done'
  const awaiting = isAwaitingApproval(status)
  const compute = server.get('compute') ?? server.get('add_compute')
  /* A node already under way (provisioning, live or failed) answers the plan question too. */
  const started = compute?.status === 'in_progress' || compute?.status === 'done' || compute?.status === 'failed'
  const plan = server.get('choose_plan')
  const planStep: WizardStep[] = plan === undefined || !isEnabled('billing')
    ? []
    : [{ id: 'plan', state: plan.status === 'done' || started ? 'done' : 'todo', note: null }]
  const planPending = planStep.some(step => step.state !== 'done')

  const node = (): WizardStep => {
    if (planPending && !started) return { id: 'node', state: 'waiting', note: PLAN_WAITING_NOTE }
    const note = compute?.note ?? null
    switch (compute?.status) {
      case 'done': return { id: 'node', state: 'done', note: null }
      case 'in_progress': return { id: 'node', state: 'in_progress', note }
      case 'failed': return { id: 'node', state: 'failed', note }
      // Blocked past approval: the server's reason no node can be added, never a link to that refusal.
      case 'blocked': return note ? { id: 'node', state: 'waiting', note, refused: true } : { id: 'node', state: 'waiting', note: WAITING_NOTE }
      default: return { id: 'node', state: 'todo', note: null }
    }
  }
  const nodeStep = awaiting && !planPending ? { id: 'node' as const, state: 'waiting' as const, note: WAITING_NOTE } : node()

  /* After the node: each step's own signal — First task is done once one has started (the server's step). A step
     not done yet waits while approval or the node is outstanding (the node gate); a done one stays done. Connect
     Git needs no node, so only approval holds it. */
  const gate = awaiting ? WAITING_NOTE : nodeStep.state === 'done' ? null : NODE_WAITING_NOTE
  const nodeComing = nodeStep.state === 'in_progress' || nodeStep.state === 'todo'
  const after = ([['account', 'add_account'], ['forge', 'connect_github'], ['project', 'add_project'], ['task', 'first_task']] as const)
    .map(([id, serverId]): WizardStep =>
      isDone(serverId) ? { id, state: 'done', note: null }
        : gate === NODE_WAITING_NOTE && id === 'task' && status.task_queued && nodeComing ? { id, state: 'waiting', note: TASK_QUEUED_NOTE }
        : gate !== null && !(id === 'forge' && gate === NODE_WAITING_NOTE) ? { id, state: 'waiting', note: gate }
        // The server's own words for an open step: AI account's "Sign in: team-max" (added, signed in nowhere).
        : todoStep(id, server.get(serverId)))

  const counted = [...planStep, ...(isEnabled('cloud_nodes') ? [nodeStep] : []), ...after]
  const finished = counted.every(step => step.state === 'done')
  const done: WizardStep = { id: 'done', state: finished ? 'done' : awaiting ? 'waiting' : 'todo', note: null }
  return [...counted, done]
}

/** An open step in the server's words, AI account's sign-in target with them. */
function todoStep(id: WizardStepId, server: CompanySetupStepStatusOut | undefined): WizardStep {
  const step: WizardStep = { id, state: 'todo', note: server?.note ?? null }
  if (server?.login) step.signIn = server.login
  return step
}
