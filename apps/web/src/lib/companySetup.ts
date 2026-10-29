// The company-setup status's wire vocabulary and its one stored flag. The five steps every surface draws are
// `lib/welcomeWizard.ts`'s; `components/onboarding/companySetupGuide.tsx` words them per viewer.
// React-free. Completion and visibility are the SERVER's (`GET /api/org/setup`, `routes/org_setup.py`):
// every step is derived from org state and org-wide, so nothing here decides whether a step is done or
// whether the guide is for this viewer. Only the ✕ (hide) is per-user, stored like the Get started guide's
// (`lib/onboarding.ts`): a side-effect flag written via `auth.tsx`'s `updatePreferences`. The key must
// match `app/preferences.py`'s `UserPreferences.company_setup_dismissed` (`extra="forbid"` 422s an unknown
// key) — rename both together.
import { scopeHref } from './taskScope'

/** The setup status query's key head. Excluded from persistence (`queryClient.ts`'s `shouldPersistQuery`): a
 *  rehydrated answer would paint yesterday's progress, or a card to someone who no longer has the authority. */
export const COMPANY_SETUP_QUERY = 'company-setup'

/** The stored key holding "this user hid the Set up your company card" (its ✕). */
export const COMPANY_SETUP_DISMISSED_PREF = 'company_setup_dismissed'

/** The step ids, spelled as `routes/org_setup.py`'s `OPERATOR_STEPS` and `SELF_SERVICE_STEPS`. The server
 *  sends the list and its order per org; `lib/welcomeWizard.ts` folds it into the five steps drawn. `add_compute` is an operator-created
 *  org's; a self-service org's `compute` is the same destination, fed by the node its plan includes. */
export type CompanySetupStepId =
  | 'connect_github'
  | 'add_compute'
  | 'add_account'
  | 'add_project'
  | 'onboard_repo'
  | 'first_task'
  | 'add_slots'
  | 'choose_plan'
  | 'approval'
  | 'compute'

/** A step's state, as the server derives it. `blocked` waits on the operator's approval. */
export type CompanySetupStepStatus = 'done' | 'todo' | 'blocked' | 'in_progress' | 'failed'

/** What a step's destination needs. `org_admin` is the org's Owner/Admin (`auth.tsx::isOrgAdminSeat`):
 *  those screens are the org owner's. */
export type CompanySetupRequirement = 'tasks:create' | 'org_admin'

/** The Billing page (`pages/org/BillingPage.tsx`) — an Owner/Admin's: products, payment history, methods, details.
 *  The Billing rail row owns it and every page under it. Route constants are plain literals, never template
 *  strings: the docs coverage check reads them by value. */
export const BILLING_PAGE = '/org/billing'

/** The subscription page (`pages/org/billing/SubscriptionPage.tsx`): the current plan, its limits, the catalogue. */
export const SUBSCRIPTION_PAGE = '/org/billing/subscription'

/** The change-plan wizard (`pages/org/billing/ChangePlanWizard.tsx`): `?plan=<key>` preselects a plan and starts at step 2. */
export const CHANGE_PLAN_PAGE = '/org/billing/subscription/change'

/** Where "choose a plan" leads — the subscription page. */
export const PLAN_PAGE = SUBSCRIPTION_PAGE

/** The old plan page's address: bookmarks and old links hop to `SUBSCRIPTION_PAGE` (`App.tsx`). */
export const LEGACY_PLAN_PAGE = '/org/plan'

/** The Jaah AI page (`pages/org/billing/JaahAiPage.tsx`): balance, funding, usage. */
export const JAAH_AI_PAGE = '/org/billing/jaah-ai'

/** One hosting's page (`pages/org/billing/HostingDetailPage.tsx`). */
export const HOSTING_PAGE_PATTERN = '/org/billing/hosting/:id'

export function HOSTING_PAGE(id: string | number): string {
  return `${BILLING_PAGE}/hosting/${encodeURIComponent(String(id))}`
}

/** One invoice's page (`pages/org/billing/InvoiceDetailPage.tsx`). */
export const INVOICE_PAGE_PATTERN = '/org/billing/invoices/:id'

export function INVOICE_PAGE(id: string): string {
  return `${BILLING_PAGE}/invoices/${encodeURIComponent(id)}`
}

/** One saved payment method's page (`pages/org/billing/MethodDetailPage.tsx`). */
export const METHOD_PAGE_PATTERN = '/org/billing/methods/:id'

export function METHOD_PAGE(id: string): string {
  return `${BILLING_PAGE}/methods/${encodeURIComponent(id)}`
}

/** The org's billing details page (`pages/org/billing/BillingDetailsPage.tsx`). */
export const BILLING_DETAILS_PAGE = '/org/billing/details'

/** The onboarding persona, and the `/tasks/new` search params that seed it (`components/tasks/taskPrefill.ts`). */
export const ONBOARD_PERSONA = 'onboard'

/** "Prepare repo": `projectId`'s task list, where the onboarding task its create filed and started
 *  (`apps/api/app/project_onboarding.py`) runs — or where one is filed by hand when that create was refused. */
export function prepareRepoHref(projectId: number): string {
  return scopeHref({ kind: 'project', projectId })
}

/** `/tasks/new` on `projectId` (the `project_id` seed), so the form opens on the setup's project, not the first. */
export function newTaskHref(projectId: number | null): string {
  return projectId === null ? '/tasks/new' : `/tasks/new?project_id=${projectId}`
}

/** Has this user hidden the card (its ✕)? Only a literal `true` counts — the safe direction. The sidebar's
 *  Setup button shows it again (`withCompanySetupShown`). */
export function isCompanySetupDismissed(preferences: Record<string, unknown> | null | undefined): boolean {
  return preferences?.[COMPANY_SETUP_DISMISSED_PREF] === true
}

/** The patch that hides the card for this user. Merge it (`updatePreferences` merges, then PUTs the whole
 *  document), never send it alone. */
export function withCompanySetupDismissed(): Record<string, unknown> {
  return { [COMPANY_SETUP_DISMISSED_PREF]: true }
}

/** The patch that shows a hidden card again (the sidebar's Setup button). Same merge contract. */
export function withCompanySetupShown(): Record<string, unknown> {
  return { [COMPANY_SETUP_DISMISSED_PREF]: false }
}
