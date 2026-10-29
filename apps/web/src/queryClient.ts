// react-query cache + localStorage persistence for fleet data (worktrees, nodes, tasks, etc).
// Persists only query results (fleet status, not credentials) so a reload paints from cache before
// revalidating. Auth stays on its own separate snapshot (authStore.ts) and is never routed through
// this cache — queries never key on session/credential data.
import { QueryClient, defaultShouldDehydrateQuery, type Query } from '@tanstack/react-query'
import { createSyncStoragePersister } from '@tanstack/query-sync-storage-persister'
import type { Persister } from '@tanstack/react-query-persist-client'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      gcTime: 5 * 60_000,
      retry: false,
    },
  },
})

const STORAGE_KEY = 'cockpit.query-cache'

/* THE PERSISTED CACHE IS ONE ORG'S. After an org switch (`components/account/OrgSwitcherList.tsx`) it must not
 * be restored into the new org's console, and a tab the switch left behind (`components/StaleTabDialog.tsx`)
 * must not write its old org's cache back over the key every tab shares. `dropPersistedQueries` removes the
 * stored copy and stops this page writing it again; the next page load starts persisting afresh. */
let persistHalted = false

const storagePersister = createSyncStoragePersister({
  storage: {
    getItem: key => window.localStorage.getItem(key),
    setItem: (key, value) => {
      if (!persistHalted) window.localStorage.setItem(key, value)
    },
    removeItem: key => window.localStorage.removeItem(key),
  },
  key: STORAGE_KEY,
})

/* A TAB ACTING AS AN ORG (`lib/actAs.ts`) neither restores nor saves: the stored copy is shared by every tab and
 * is the operator's own. Checked when a save is SCHEDULED, not when the throttled write lands, so an answer the
 * acting tab dehydrated cannot be written a second after it left act-as. */
let persistPaused = false

export function pauseQueryPersistence(paused: boolean): void {
  persistPaused = paused
}

export const queryPersister: Persister = {
  persistClient: client => (persistPaused ? undefined : storagePersister.persistClient(client)),
  restoreClient: () => (persistPaused ? undefined : storagePersister.restoreClient()),
  removeClient: () => storagePersister.removeClient(),
}

/* Removes the stored copy NOW, without halting later saves. `queryClient.clear()` rewrites it only after the persister's
 * throttle (1s), and a reload inside that window restores the OUTGOING identity's cache into the next one's console
 * (a project list the new user may not read, painting a page their own load would refuse). Called on an identity change
 * (`auth.tsx::setAdmin`); the new identity's own answers are saved afresh. */
export function removePersistedQueries(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* storage unavailable: nothing was persisted */
  }
}

export function dropPersistedQueries(): void {
  persistHalted = true
  removePersistedQueries()
}

/* Keys that must NOT survive a reload, wired into `PersistQueryClientProvider`'s `dehydrateOptions`
 * (`main.tsx`). `defaultShouldDehydrateQuery` first and unconditionally — it limits persistence to
 * SUCCESSFUL queries; this predicate may only subtract.
 * `onboarding-own-tasks`: `onboarding/getStartedGuide.ts` holds its render until that probe answers, and a rehydrated
 * entry would skip the hold and flash a 24h-old "no" (cheap to re-ask).
 * `company-setup`: `onboarding/companySetupGuide.tsx` holds on the same kind of answer, and a rehydrated one would paint an
 * old verdict — progress, or the card itself to a viewer who has since lost the authority.
 * `org-plan`: the Subscription page's usage counts (`lib/orgPlan.ts`); a rehydrated one would paint old usage.
 * `org-billing`: the Billing page (`lib/billing.ts`) — billing details and saved cards are PII that must not sit in
 * localStorage past the page, and a rehydrated card list would paint a stale default.
 * `hosting-catalog`: the Add node dialog's prices (`components/addNode/api.ts`) carry the saved card's brand and last
 * digits — the same PII — and a rehydrated price would be stale.
 * `operator-dashboard`: the operator dashboard's cross-tenant figures (`pages/dashboard/`) — a year of every
 * section is large for localStorage, and a rehydrated answer would paint an old "as of".
 * `health-components`: the Live badge (`hooks/useHealth.ts`); a rehydrated one would claim an old "connected".
 * `trial-agents`: the rail's trial meter (`components/TrialAgentsMeter.tsx`) — as `org-plan`, a rehydrated one would paint
 * an old peak, or a trial since converted.
 * `deployed-version`: `hooks/useVersionCheck.ts` latches a `true`; rehydrated into the NEW bundle it would offer
 * an update forever.
 * `credentials`, `git-reach`, `git-connections`: /profile's Git access state (`pages/ProfilePage.tsx`,
 * `components/GitConnectionsCard.tsx`) — account-scoped, and need not outlive the page.
 * `settings`: the knob payload (`pages/settings/KnobSection.tsx`) carries the viewer's `editable` verdicts; a rehydrated
 * one would offer a save the viewer may since have lost.
 * `git-repo-owners`: the owners a person's git connections may create a repository in
 * (`components/projects/NewRepoFields.tsx`) — account-scoped, and need not outlive the page.
 * `accounts`, `cloud-connections`: runner-account records and AWS network ids — need not outlive the page.
 * `credential-owners`: people's names (`pages/credentials/CredentialFilterStrip.tsx`). `legal-doc`: may be an unpublished
 * draft (`pages/LegalPage.tsx`), and a rehydrated "not found" would hide a page published since.
 * `agent-choices`, `agents-list`, `org-members`, `subject-attachments`, `task-dependencies`: the task form's cached reads
 * (`lib/cachedRead.ts`) — a rehydrated entry inside its `staleTime` would answer a reload with no GET, and a reload is
 * how an operator asks for the truth; `org-members` and attachment names are people's names and files besides.
 * `access-roles`: the role catalogue, read by `pages/NoAccess.tsx` and the access tabs (`hooks/useApiRead.ts`) under one key —
 * the latter never persists, so the key is listed here too, whichever reader's options are the live ones.
 * (`project-members`, `task-models` and `agent-providers` stay persisted: the first two are `ProjectEditPage`'s keys,
 * persisted there already, and a catalogue of providers holds nothing of anyone's.) */
const UNPERSISTED_QUERIES: ReadonlySet<unknown> = new Set(['onboarding-own-tasks', 'company-setup', 'org-plan', 'trial-agents', 'customer-plan', 'org-billing', 'hosting-catalog', 'operator-dashboard', 'health-components', 'deployed-version', 'credentials', 'git-reach', 'git-connections', 'git-repo-owners', 'settings', 'credential-owners', 'legal-doc', 'accounts', 'cloud-connections', 'agent-choices', 'agents-list', 'org-members', 'subject-attachments', 'task-dependencies', 'own-org', 'access-roles'])

/* A query may also opt out by `meta: { persist: false }` — the page-scoped reads of `hooks/useApiRead.ts`. */
export const shouldPersistQuery = (query: Query): boolean =>
  defaultShouldDehydrateQuery(query) && !UNPERSISTED_QUERIES.has(query.queryKey[0]) && query.meta?.persist !== false
