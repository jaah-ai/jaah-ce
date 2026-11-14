import { type FormEvent, useEffect, useRef, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { put } from '../api'
import { useAuth, useCan, type Admin } from '../auth'
/* Git: a table of connected forges plus one add form (see its header). Self-service only, so /users/:id keeps
 * `components/GitAccessField`'s single select. */
import GitConnectionsCard, { type ConnectReturn } from '../components/GitConnectionsCard'
import GitReachNotice from '../components/GitReachNotice'
/* The app's one tab shell (`Tabs`/`TabPanel`). Keeping every panel mounted does NOT protect edits here
 * (all state lives in this component, above the panels); it makes the fetches run once. Its
 * cost: a mounted-but-`hidden` control still joins native validation while unfocusable — hence
 * `noValidate` and `submitUserForm`'s own checks (`pages/users/UserFields.tsx`). */
import { TabPanel, Tabs, type TabDef } from '../components/Tabs'
import { useConnectReturn } from '../hooks/useConnectReturn'
import { useSubmit } from '../hooks/useSubmit'
import { useUrlTab } from '../hooks/useUrlTab'
/* The Alerts panel, shared with `/users/:id` (see its header): it loads and saves itself, outside this page's Save. */
import NotificationsPanel from '../components/NotificationsPanel'
import TrustedDevices from '../components/TrustedDevices'
import UserSettingsFields from '../components/UserSettingsFields'
import { isEnabled } from '../lib/editionFeatures'
import { getIpAllowlistStatus, ipAllowlistStatusKey } from '../lib/ipAllowlist'
import { CHOOSE_THEME_STEP, withOnboardingStepDone } from '../lib/onboarding'
import { preferencesPatch } from '../lib/preferencesPatch'
import { readUserSettings } from '../lib/userSettings'
import { grid } from '../theme'
import Banner from '../ui/Banner'
import { PHONE_QUERY } from '../ui/listStyles'
import FormActionBar from '../ui/FormActionBar'
import FormTextField from '../ui/FormTextField'
import SubmitButton from '../ui/SubmitButton'
import {
  getGitReach,
  GIT_CREDENTIAL_TYPES,
  listCredentials,
  PER_PAGE as CREDENTIALS_PER_PAGE,
} from './credentials/api'
/* The console's one detail-page control (as `/tasks/:id`, `/nodes/:id`). `DetailPage` owns the header and
 * the action-bar slot; `DetailSection` is the BORDERLESS grouping (no frame inside a framed tab panel). */
import { BAR_ON_PAGE_GROUND_SX, DetailPage, DetailSection } from '../ui/detail/DetailPage'
import { NameAndPasswordFields, passwordFloorError, submitUserForm, useUserDrafts } from './users/UserFields'
/* Settings › API keys' list, for a member who cannot open Settings (no `system:view`). */
import ApiKeysSection from './settings/apiKeys/ApiKeysSection'

// The one self-service surface: unguarded by design (see App.tsx's /profile route and
// RequireFeature.tsx), reachable from the Sidebar footer and /no-access, and wired to `PUT /api/auth/me`
// — no new endpoint, no permission key. Edits ONLY the signed-in account (no `:id`, no is_superadmin).
//
// Tabs in one `ui/detail` `DetailPage`, untitled — the sidebar footer names the page (operator decisions):
// Profile (email, first/last name, new password), Settings (the shared preference set), Alerts (notifications), Git
// (the forges runners push with — every other credential is Directory > Secrets, open to everyone, so this
// page keeps no second roster), Security (trusted devices — writes on its own, never through Save;
// `components/TrustedDevices`; and, for a member without `system:view`, their API keys — Settings is their home
// for everyone else, so it is one home per viewer).
// A 480px measure (a form), overriding `DetailPage`'s 64rem cap; same as `UserEditPage`'s `FORM_CARD_SX`.
// Alerts alone keeps a wider one: its switches squeezed into 480px left most of a desktop empty.
//
// ONE SAVE, ONE REQUEST: a per-panel Save is unreachable from the other tabs and would silently lose
// edits made there. `DetailPage`'s `actionBar` puts `ui/FormActionBar` OUTSIDE every panel; the button's
// `form` attribute keeps Enter-to-save working. One `PUT`, since `MeUpdate` (routes/auth.py) takes every
// field here in one body — three writes would be last-writer-wins on one row. `buildBody` sends only
// changes. Alerts is not in it: `components/NotificationsPanel` saves each switch at once.
// The theme flips on SAVE, not on click: this page does not route preferences through `auth.tsx`'s
// optimistic `updatePreferences` (right for the Sidebar fold, wrong for an explicit-save form).
// The settings controls, draft and wire shape are `components/UserSettingsFields.tsx` +
// `lib/userSettings.ts`'s; here the record comes from the auth context.

// Git is `components/GitConnectionsCard`: each write lands on its own (Connect, paste a token, use
// an existing credential, reconnect, disconnect) — never through the page's Save. The page's own
// `?mine=true` git rows are handed to it as the "existing credential" fallback.

/* The tab model, declared once. The tab lives in the URL (hooks/useUrlTab.ts). */
type Tab = 'profile' | 'settings' | 'notifications' | 'git-accounts' | 'security'

/** The form every tab writes into; the action bar's button submits it from outside via the HTML `form`
 *  attribute (as `NodeDetailPage`). */
const PROFILE_FORM_ID = 'profile-form'

/** The page measure, narrower than `DetailPage`'s `read` default (merged after it, per its `sx`
 *  contract): 480px, as `UserEditPage`'s form column; the Alerts tab gets 48rem.
 *  The bar wears the PAGE's ground (`BAR_ON_PAGE_GROUND_SX`): no panel sits behind the fields. The gap keeps
 *  its rule off the last field. `idle` = a clean form, not saving. */
const pageSx = (tab: Tab, idle: boolean) => ({
  maxWidth: tab === 'notifications' ? grid(128) : grid(80),
  '& > [data-testid="profile-form-actions"]': {
    mt: 2,
    ...BAR_ON_PAGE_GROUND_SX,
    // Nothing to save: the bar stops riding over the page (a sticky dead Save covered an Events row) and sits
    // in flow under the fields, as Settings' knob bar (`pages/settings/KnobSection.tsx`). On a phone it goes:
    // a disabled full-width Save read as a dead input and cost ~70px on every tab.
    ...(idle && { position: 'static', boxShadow: 'none', [`@media ${PHONE_QUERY}`]: { display: 'none' } }),
  },
}) as const

/** A panel's stack, INSIDE `TabPanel`'s bare `<div>`, so a hidden panel contributes no layout. `pt` keeps
 *  the borderless lead section off the tab strip's hairline. */
const PANEL_SX = { pt: 1.5, display: 'flex', flexDirection: 'column', gap: 1.5 } as const

/* Notifications follows Settings (both "how this console behaves"; same order as `/users/:id`).
 * Git and Security close it: the non-preference tabs. Notifications is labelled "Alerts" so all five fit
 * one row at 375px ("Notifications" wrapped "Security" alone onto a second row). */
/* The tabs that write into the one form. Alerts, Git and Security hold no draft (each saves on its own), so they
 * show the save bar only while another tab's edit is pending: a disabled Save under Trusted devices read as
 * Security's own. */
const DRAFT_TABS: ReadonlySet<Tab> = new Set(['profile', 'settings'])

const PROFILE_TABS: readonly TabDef<Tab>[] = [
  { id: 'profile', label: 'Profile' },
  { id: 'settings', label: 'Settings' },
  { id: 'notifications', label: 'Alerts' },
  { id: 'git-accounts', label: 'Git' },
  { id: 'security', label: 'Security' },
]

/* The query params a forge Connect callback 303s back with (`routes/forge_connect.py::CONNECT_HOOKS`, its
 * `finish`): `?git_connect=connected|error`, a `reason` slug on failure,
 * and the `forge` (absent from GitHub's own callback, which is the default). Consumed and stripped in one write with
 * the switch to the Git tab, where the answer renders (`hooks/useConnectReturn`); the slugs' wording is
 * `components/GitConnectionsCard`'s. */
const GIT_CONNECT_RETURN = { param: 'git_connect', carries: ['forge'], set: { tab: 'git-accounts' } } as const

export default function ProfilePage() {
  const { admin, refresh } = useAuth()
  const can = useCan()

  const [tab, selectTab] = useUrlTab(PROFILE_TABS, 'profile')

  const drafts = useUserDrafts()
  const { password, setPassword, settings, setSettings, seedNames } = drafts

  /* One save triple for the page: one button, one request. */
  const { busy: saving, formError: saveError, setFormError: setSaveError, run, saved, clearSaved } = useSubmit()

  /* The outcome of a Connect round trip (what happened while away). */
  const [gitReturn] = useConnectReturn(GIT_CONNECT_RETURN)
  const connectReturn: ConnectReturn | null = gitReturn && { ...gitReturn, forge: gitReturn.params.forge || 'github' }
  /* Bumped after a Git write, so GitHub reach is re-asked. */
  const [gitChangeTick, setGitChangeTick] = useState(0)
  /* The operator's own credentials — only as Git' "existing credential" fallback (its per-root
   * roster is behind `users:view`). `?mine=true`: `CredentialOut.created_by` is a display NAME, so "rows I
   * created" can only be answered server-side. One page; a failure keeps the last list (none: empty) —
   * the fallback is a convenience, and the pick form says when it lists nothing. */
  const credentialsQuery = useQuery({
    queryKey: ['credentials', 'mine'],
    queryFn: async () => (await listCredentials(1, CREDENTIALS_PER_PAGE, { mine: true })).items,
  })
  const credentials = credentialsQuery.data ?? []

  /* Which GitHub orgs block the connected token (`components/GitReachNotice.tsx` is the single
   * home). Fetched here because /profile owns every fetch on the page. No error state: the server collapses
   * "cannot tell" into `state: 'unknown'` (renders nothing), and a transport failure means the same — the
   * old answer is cleared rather than left stale, as it can no longer be confirmed.
   * Only while the Git tab is open (the only panel it renders in; it costs a GitHub round trip), and
   * re-asked on every return (`staleTime: 0`). Keyed on `connectReturn` (a future client-side return, and
   * the return-leg tab switch), `admin.git_credential_id` and `gitChangeTick` (a Git write). */
  const reachQuery = useQuery({
    queryKey: ['git-reach', admin?.git_credential_id, connectReturn, gitChangeTick],
    queryFn: getGitReach,
    enabled: tab === 'git-accounts',
    staleTime: 0,
    refetchOnWindowFocus: false,
    placeholderData: keepPreviousData,
  })
  const reach = reachQuery.isError ? null : reachQuery.data ?? null

  /* Whether the org's IP allowlist binds this member: a one-line notice on Security, nothing while unknown. */
  const allowlistQuery = useQuery({
    queryKey: ipAllowlistStatusKey,
    queryFn: getIpAllowlistStatus,
    enabled: tab === 'security',
  })

  /* After a Git write: a pasted token is a new own credential (the fallback list), a GitHub write moves
   * `git_credential_id` (the onboarding cards read it off the account), and GitHub reach may change.
   * A failed account re-read is not the row's failure (it landed), so it is swallowed; a reload shows it. */
  function handleGitAccessChanged() {
    setGitChangeTick(tick => tick + 1)
    void credentialsQuery.refetch()
    refresh().catch(() => {})
  }

  /* Seed the drafts on a new identity or on this page's own save, NOTHING else. `admin` changes whenever
   * `auth.tsx`'s `updatePreferences` runs (e.g. the Sidebar nav fold), and re-seeding then silently reset
   * every draft across tabs. A tick bumped only after a successful save-refresh gives that case an edge an
   * unrelated mutation cannot forge; the ref makes it idempotent. */
  const hydratedKeyRef = useRef<string | null>(null)
  const [hydrateTick, setHydrateTick] = useState(0)

  useEffect(() => {
    if (!admin) return
    const key = `${admin.id}:${hydrateTick}`
    if (hydratedKeyRef.current === key) return
    hydratedKeyRef.current = key
    seedNames(admin)
    /* The same hydrate rule as the superadmin's Settings tab (`readUserSettings`). */
    setSettings(readUserSettings(admin.preferences))
  }, [admin, hydrateTick, seedNames, setSettings])

  /* The ONE body the ONE save sends: every field this page owns, only those that changed. `PUT
   * /api/auth/me` reads absent as "say nothing", so a Profile-tab save does not re-assert Settings values
   * the server may no longer accept. Git is not in it: each of its writes lands on its own
   * (`components/GitConnectionsCard`). The preferences document is `lib/preferencesPatch.ts`'s (shared with
   * `/users/:id`); the guide's theme step rides it, recorded only when the settings actually changed — see
   * `lib/onboarding.ts` for why this step is recorded rather than derived. */
  function buildBody(current: Admin): Record<string, unknown> {
    const body = drafts.nameChanges(current)
    const preferences = preferencesPatch(current.preferences, settings)
    /* The theme step rides the settings change. */
    if (preferences) body.preferences = { ...preferences, ...withOnboardingStepDone(current.preferences, CHOOSE_THEME_STEP) }
    return body
  }

  /* The save glue (validation, the empty backstop) is `submitUserForm`'s; this page's own part is the PUT.
   * A rejected PUT is the server's sentence (a 422 on the git field names the problem) — each has its own fix. */
  const handleSave = (e: FormEvent) => submitUserForm(e, admin, {
    selectTab, setSaveError, clearSaved, run,
    validate: () => passwordFloorError(password),
    buildBody,
    write: async body => {
      await put<Admin>('/api/auth/me', body)
      /* The write has landed; the re-read is separate (its own `try`). `refresh()` is a second round trip, and
       * reporting its failure as "Failed to save" invites a retry that, on a password change, rotates
       * `session_epoch` again. So a re-read fault is reported as such, with the save still a success. */
      /* Clear the password NOW, not on the re-seed (which only runs if `refresh()` succeeds), so a retry after a
       * failed re-read cannot resend it. The server never echoes it. */
      setPassword('')
      try {
        // The identity payload carries every field written; this re-seeds the drafts so the action bar goes clean.
        await refresh()
        // The edge the seeding effect waits for; only a save may produce it.
        setHydrateTick(tick => tick + 1)
      } catch {
        setSaveError('Your changes were saved, but this page could not reload your account. Reload the page to see the stored values.')
      }
    },
  })

  // Transient: RequireAuth guards this route; `admin` is null only between logout and redirect.
  if (!admin) return null

  /* Dirty = the save body is non-empty: one rule for the button. */
  const isDirty = Object.keys(buildBody(admin)).length > 0

  /* "Saved" is DERIVED from `isDirty`, not latched: any edit makes it untrue, for all eight controls alike.
   * The error banner is deliberately not symmetric: it survives editing (the operator needs the reason while
   * fixing it) and is cleared by the next submit. */
  const showSaved = saved && !isDirty

  /* The git-capable subset (`GIT_CREDENTIAL_TYPES`, single-homed in `pages/credentials/api.ts`). Not
   * memoised: a small filter, and the consumer re-seeds nothing on identity. */
  const gitCredentials = credentials.filter(row => GIT_CREDENTIAL_TYPES.has(row.type))

  return (
    <DetailPage
      /* A handle so the page measure can be asserted. */
      testId="profile-page"
      sx={pageSx(tab, !isDirty && !saving)}
      /* The page's save bar, outside every panel (see header); the `form` attribute makes it the submit. */
      actionBar={(DRAFT_TABS.has(tab) || isDirty) && (
        <FormActionBar testId="profile-form-actions" stretchTestIds={['profile-form-submit']}>
          {/* A withheld Save says why, in words beside it (a hover is no phone's). */}
          {!isDirty && !saving && (
            <Typography variant="body2" sx={{ color: 'text.secondary', alignSelf: 'center' }} data-testid="profile-form-no-changes">
              No changes
            </Typography>
          )}
          <SubmitButton form={PROFILE_FORM_ID} testId="profile-form-submit" busy={saving} disabled={!isDirty}>
            Save
          </SubmitButton>
        </FormActionBar>
      )}
    >
      {/* Above the tab strip: the verdict is about the one submit, which spans all tabs. */}
      {saveError && <Banner severity="error">{saveError}</Banner>}
      {showSaved && <Banner severity="success">Saved</Banner>}

      <Tabs
        tabs={PROFILE_TABS}
        tab={tab}
        onSelect={selectTab}
        label="Profile sections"
        idPrefix="profile"
        wrapOnPhone
        sticky="default"
      />

      {/* One form across all three panels (three slices of one record, one submit). */}
      {/* `noValidate` is load-bearing: see `submitUserForm`. */}
      <Box component="form" id={PROFILE_FORM_ID} noValidate onSubmit={e => void handleSave(e)}>
        <TabPanel id="profile" current={tab} idPrefix="profile">
          <Box sx={PANEL_SX}>
            {/* The saved record, not the draft: a nameless account (a seeded operator's) reads as its raw email in
                every Changed-by and Created-by cell until it saves one. The community edition has one user:
                nobody else sees it. */}
            {!admin.first_name?.trim() && !admin.last_name?.trim() && (
              <Banner severity="info" testId="profile-name-missing">{isEnabled('users_invites') ? 'Add your name. Others see your email.' : 'Add your name.'}</Banner>
            )}
            {/* No title (the tab says "Profile"). `DetailSection` for its 1.75 gap, which clears the ~9px a SHRUNK MUI
            label overhangs its box. */}
            <DetailSection>
              {/* Read-only: nobody changes their own login email; the server 403s it on `PUT
              /api/auth/me`, so the save never sends it. */}
              <FormTextField
                id="profile-email" label="Email" help="Your sign-in address; an admin or Support changes it."
                readOnly type="email" value={admin.email} onChange={() => {}}
              />
              {/* `disabled={saving}` on the rest: a keystroke between the PUT and the re-seed is silently destroyed. */}
              <NameAndPasswordFields idPrefix="profile" disabled={saving} drafts={drafts} />
            </DetailSection>
          </Box>
        </TabPanel>

        <TabPanel id="settings" current={tab} idPrefix="profile">
          <Box sx={PANEL_SX}>
            <DetailSection>
              {/* `disabled={saving}` too: the same silent-loss guard. */}
              <UserSettingsFields
                value={settings}
                onChange={setSettings}
                disabled={saving}
                defaultTheme={admin?.default_theme}
              />
            </DetailSection>
          </Box>
        </TabPanel>

        <TabPanel id="notifications" current={tab} idPrefix="profile">
          <Box sx={PANEL_SX}>
            {/* No title (the tab says "Alerts"); the panel draws its own. Mounted only while open: its fetch is
                this tab's alone, and it holds no draft to lose. */}
            {tab === 'notifications' && <NotificationsPanel />}
          </Box>
        </TabPanel>

        <TabPanel id="git-accounts" current={tab} idPrefix="profile">
          <Box sx={PANEL_SX}>
            {/* No title (the tab says "Git"). Every other credential lives in Directory > Secrets, open to
            every signed-in user (one surface, no second roster here). */}
            <DetailSection>
              <GitConnectionsCard
                userId={admin.id}
                ownGitCredentials={gitCredentials}
                returned={connectReturn}
                onChanged={handleGitAccessChanged}
              />
              {/* Under the table: GitHub reach is a consequence of the GitHub row. */}
              <Box sx={{ mt: 1 }}>
                <GitReachNotice reach={reach} />
              </Box>
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                Other credentials are in{' '}
                <Link component={RouterLink} to="/credentials">Directory &gt; Secrets</Link>.
              </Typography>
            </DetailSection>
          </Box>
        </TabPanel>

        <TabPanel id="security" current={tab} idPrefix="profile">
          <Box sx={PANEL_SX}>
            {allowlistQuery.data?.on === true && (
              <Banner severity="info" testId="ip-allowlist-notice">
                Sign-in is allowed only from approved networks.
              </Banner>
            )}
            <DetailSection title="Trusted devices">
              {/* Mounted only while open: its fetch is this tab's alone, and it holds no draft to lose. */}
              {tab === 'security' && <TrustedDevices />}
            </DetailSection>
            {/* `ApiKeysSection`'s own Settings rule (`apiKeys` in `pages/Settings.tsx`): a member of an org. */}
            {Boolean(admin.org?.id) && !can('system:view') && (
              <DetailSection title="API keys">
                {tab === 'security' && <ApiKeysSection />}
              </DetailSection>
            )}
          </Box>
        </TabPanel>
      </Box>

    </DetailPage>
  )
}
