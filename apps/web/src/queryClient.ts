// react-query cache + localStorage persistence for fleet data (worktrees, nodes, tasks, etc).
// Persists only query results (fleet status, not credentials) so a reload paints from cache before
// revalidating. Auth stays on its own separate snapshot (authStore.ts) and is never routed through
// this cache — queries never key on session/credential data.
import { QueryClient, defaultShouldDehydrateQuery, type Query } from '@tanstack/react-query'
import { createSyncStoragePersister } from '@tanstack/query-sync-storage-persister'
import type { PersistedClient, Persister } from '@tanstack/react-query-persist-client'
import { TASK_LIST_SEED_GC_MS, TASK_LIST_SEED_KEY } from './lib/taskListSeed'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      gcTime: 5 * 60_000,
      retry: false,
    },
  },
})

// Before the restore, so the restored seed keeps its own lifetime (`lib/taskListSeed.ts`).
queryClient.setQueryDefaults(TASK_LIST_SEED_KEY, { gcTime: TASK_LIST_SEED_GC_MS })

const STORAGE_KEY = 'cockpit.query-cache'

/* THE PERSISTED CACHE IS ONE ORG'S. After an org switch (`components/account/OrgSwitcherList.tsx`) it must not
 * be restored into the new org's console, and a tab the switch left behind (`components/StaleTabDialog.tsx`)
 * must not write its old org's cache back over the key every tab shares. `dropPersistedQueries` removes the
 * stored copy and stops this page writing it again; the next page load starts persisting afresh. */
let persistHalted = false
// The snapshot handed to the throttled persister and not yet written (see the `pagehide` flush below).
let unsaved: PersistedClient | undefined

const storage = {
  getItem: (key: string) => window.localStorage.getItem(key),
  setItem: (key: string, value: string) => {
    unsaved = undefined // the throttle writes the latest snapshot, the one `unsaved` holds
    if (!persistHalted) window.localStorage.setItem(key, value)
  },
  removeItem: (key: string) => window.localStorage.removeItem(key),
}

const storagePersister = createSyncStoragePersister({ storage, key: STORAGE_KEY })

/* A TAB ACTING AS AN ORG (`lib/actAs.ts`) neither restores nor saves: the stored copy is shared by every tab and
 * is the operator's own. Checked when a save is SCHEDULED, not when the throttled write lands, so an answer the
 * acting tab dehydrated cannot be written a second after it left act-as. */
let persistPaused = false

export function pauseQueryPersistence(paused: boolean): void {
  persistPaused = paused
}

/* THE LATEST SNAPSHOT IS WRITTEN AS THE PAGE GOES. The persister writes on a 1s trailing throttle, so a reload inside
 * that second (a save, then F5) restored the copy from BEFORE the save's invalidation: a pre-save record younger than
 * `staleTime`, which `hooks/useEditRecord.ts`'s `fetchQuery` answered with no GET. On `pagehide` — or the page going
 * hidden, the last event a phone's discarded tab gets — the snapshot the throttle still holds is written at once; it carries the save's `isInvalidated` (or the re-read's answer), and
 * react-query refetches a restored invalidated entry on its first read. One place for every page's save. */
let flushOnHide = false

/* Writes the snapshot the throttle has not written yet, synchronously. */
function flushPersistedQueries(): void {
  const client = unsaved
  unsaved = undefined
  if (!client) return
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(client))
  } catch {
    /* storage full or unavailable: the throttled write meets the same and is dropped the same way */
  }
}

function persistClient(client: PersistedClient) {
  if (persistPaused) return undefined
  unsaved = client
  if (!flushOnHide && typeof window !== 'undefined') {
    flushOnHide = true
    window.addEventListener('pagehide', flushPersistedQueries)
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') flushPersistedQueries()
    })
  }
  return storagePersister.persistClient(client)
}

export const queryPersister: Persister = {
  persistClient,
  restoreClient: () => (persistPaused ? undefined : storagePersister.restoreClient()),
  removeClient: () => storagePersister.removeClient(),
}

/* Removes the stored copy NOW, without halting later saves. `queryClient.clear()` rewrites it only after the persister's
 * throttle (1s), and a reload inside that window restores the OUTGOING identity's cache into the next one's console
 * (a project list the new user may not read, painting a page their own load would refuse). Called on an identity change
 * (`auth.tsx::setAdmin`); the new identity's own answers are saved afresh. */
export function removePersistedQueries(): void {
  unsaved = undefined // the outgoing identity's snapshot must not be flushed back on `pagehide`; the caller clears the cache first
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
 * `exec-dashboard`: the command center (`pages/command-center/`) — the same "as of" reason, and one org's
 * figures must not outlive the session that read them.
 * `health-components`: the Live badge (`hooks/useHealth.ts`); a rehydrated one would claim an old "connected".
 * `trial-agents`: the rail's agents throttle (`components/TrialAgentsMeter.tsx`) — as `org-plan`, a rehydrated one would paint
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
 * `access-roles`, `project-members`: catalogues read under one key by a persisting reader (`pages/NoAccess.tsx` and the
 * access tabs; `ProjectEditPage` and `TaskSpecFields`' cached read) AND by `hooks/useApiRead.ts` (the access tabs; the
 * assignee lookup, `components/tasks/TaskPeople.tsx`), which never persists — listed here so the key behaves one way
 * whichever reader's options are the live ones; a project's members are people's names besides.
 * (`task-models` and `agent-providers` stay persisted: `task-models` is `hooks/useTaskModels`' key, and a catalogue of
 * providers holds nothing of anyone's.) */
const UNPERSISTED_QUERIES: ReadonlySet<unknown> = new Set(['onboarding-own-tasks', 'company-setup', 'org-plan', 'trial-agents', 'customer-plan', 'org-billing', 'hosting-catalog', 'operator-dashboard', 'exec-dashboard', 'health-components', 'deployed-version', 'credentials', 'git-reach', 'git-connections', 'git-repo-owners', 'settings', 'credential-owners', 'legal-doc', 'accounts', 'cloud-connections', 'agent-choices', 'agents-list', 'org-members', 'subject-attachments', 'task-dependencies', 'own-org', 'access-roles', 'project-members', 'node-groups'])

/* A query may also opt out by `meta: { persist: false }` — the page-scoped reads of `hooks/useApiRead.ts`. */
export const shouldPersistQuery = (query: Query): boolean =>
  defaultShouldDehydrateQuery(query) && !UNPERSISTED_QUERIES.has(query.queryKey[0]) && query.meta?.persist !== false
