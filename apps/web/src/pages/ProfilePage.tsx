import { type ComponentType, type FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
import { put } from '../api'
import { useAuth, type Admin } from '../auth'
/* Git: a table of connected forges plus one add form (see its header). Self-service only, so /users/:id keeps
 * `components/GitAccessField`'s single select. */
import GitConnectionsCard, { type ConnectReturn } from '../components/GitConnectionsCard'
import GitReachNotice from '../components/GitReachNotice'
/* The app's one tab shell (`Tabs`/`TabPanel`). Keeping every panel mounted does NOT protect edits here
 * (all state lives in this component, above the panels); it makes the fetches run once. Its
 * cost: a mounted-but-`hidden` control still joins native validation while unfocusable — hence
 * `noValidate` and `firstValidationError`. */
import { TabPanel, Tabs, type TabDef } from '../components/Tabs'
import { useSubmit } from '../hooks/useSubmit'
import { useUrlFilters } from '../hooks/useUrlFilters'
import { useUrlTab } from '../hooks/useUrlTab'
/* The Notifications panel, shared with `/users/:id` (see its header). Here the draft seeds from the auth
 * context and rides this page's single `PUT /api/auth/me`. */
import NotificationsPanel, { EVENTS_TABLE_GUTTER } from '../components/NotificationsPanel'
import { channelWhoId } from '../components/channelRow'
import TrustedDevices from '../components/TrustedDevices'
import UserSettingsFields from '../components/UserSettingsFields'
import { isEnabled } from '../lib/editionFeatures'
import { getIpAllowlistStatus, ipAllowlistStatusKey } from '../lib/ipAllowlist'
import { methodsToSave, notificationsEqual, readNotifications } from '../lib/notificationPrefs'
import { CHOOSE_THEME_STEP, withOnboardingStepDone } from '../lib/onboarding'
import { mergedUserPreferences, readUserSettings, userSettingsToPreferences } from '../lib/userSettings'
import Banner from '../ui/Banner'
import { PHONE_QUERY } from '../ui/listStyles'
import FormActionBar, { FORM_BAR_GROUND } from '../ui/FormActionBar'
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
import { DetailPage, DetailSection } from '../ui/detail/DetailPage'
import { PASSWORD_MIN, passwordProblem } from './NewPasswordFields'
import { NameAndPasswordFields, useFocusableProjects, useUserDrafts } from './users/UserFields'
import { GIT_ADD_PARAM } from '../lib/gitAddLink'

// The one self-service surface: unguarded by design (see App.tsx's /profile route and
// RequireFeature.tsx), reachable from the Sidebar footer and /no-access, and wired to `PUT /api/auth/me`
// — no new endpoint, no permission key. Edits ONLY the signed-in account (no `:id`, no is_superadmin).
//
// Tabs in one `ui/detail` `DetailPage`, untitled — the sidebar footer names the page (operator decisions):
// Profile (email, first/last name, new password), Settings (the shared preference set), Alerts (notifications), Git
// (the forges runners push with — every other credential is Directory > Secrets, open to everyone, so this
// page keeps no second roster), Security (trusted devices — writes on its own, never through Save;
// `components/TrustedDevices`).
// A 480px measure (a form), overriding `DetailPage`'s 64rem cap; same as `UserEditPage`'s `FORM_CARD_SX`.
// Notifications alone keeps a wider one: its events table squeezed into 480px left most of a desktop empty.
//
// ONE SAVE, ONE REQUEST: a per-panel Save is unreachable from the other tabs and would silently lose
// edits made there. `DetailPage`'s `actionBar` puts `ui/FormActionBar` OUTSIDE every panel; the button's
// `form` attribute keeps Enter-to-save working. One `PUT`, since `MeUpdate` (routes/auth.py) takes every
// field here in one body — three writes would be last-writer-wins on one row. `buildBody` sends only
// changes.
// The theme flips on SAVE, not on click: this page does not route preferences through `auth.tsx`'s
// optimistic `updatePreferences` (right for the Sidebar fold, wrong for an explicit-save form).
// The settings controls, draft and wire shape are `components/UserSettingsFields.tsx` +
// `lib/userSettings.ts`'s; here the record comes from the auth context.

// Git is `components/GitConnectionsCard`: each write lands on its own (Connect, paste a token, use
// an existing credential, reconnect, disconnect) — never through the page's Save. The page's own
// `?mine=true` git rows are handed to it as the "existing credential" fallback.

/* Slack's Connect (`components/SlackConnect`), loaded only with `slack`: the community edition's tree leaves it out,
 * and `import.meta.glob` yields `{}` for an absent file. Without it the Slack row offers no Connect. */
type SlackConnectView = ComponentType<{ linked?: boolean; onLinked: (memberId: string, displayName: string) => void; disabled?: boolean }>
const SlackConnect: SlackConnectView | undefined = Object.values(isEnabled('slack')
  ? import.meta.glob<SlackConnectView>('../components/SlackConnect.tsx', { eager: true, import: 'default' })
  : {})[0]

/* Telegram's Connect and Turn on (`components/TelegramConnect`), loaded only with `telegram`, as Slack's above.
 * Without it the Telegram row offers neither. */
type TelegramConnectModule = {
  default: ComponentType<{ onLinked: () => void; onUnlinked: () => void; onRemove: () => void; whoId: string; disabled?: boolean }>
  TelegramEnableHere: ComponentType<{ onEnabled: () => void; onOffer?: (offered: boolean) => void; disabled?: boolean }>
}
const telegramModule: TelegramConnectModule | undefined = Object.values(isEnabled('telegram')
  ? import.meta.glob<TelegramConnectModule>('../components/TelegramConnect.tsx', { eager: true })
  : {})[0]
const TelegramConnect = telegramModule?.default
const TelegramEnableHere = telegramModule?.TelegramEnableHere

/* The tab model, declared once. The tab lives in the URL (hooks/useUrlTab.ts). */
type Tab = 'profile' | 'settings' | 'notifications' | 'git-accounts' | 'security'

/** The form every tab writes into; the action bar's button submits it from outside via the HTML `form`
 *  attribute (as `NodeDetailPage`). */
const PROFILE_FORM_ID = 'profile-form'

/** The page measure, narrower than `DetailPage`'s `read` default (merged after it, per its `sx`
 *  contract): 480px, as `UserEditPage`'s form column; the Notifications tab's events table gets 48rem.
 *  The action bar wears the PAGE's ground: this page has no panel behind its fields, so `ui/FormActionBar`'s
 *  `background.paper` read as a stray 480px box on `background.default`; `FORM_BAR_GROUND` moves
 *  its fill and the strip under it together. The gap keeps its rule
 *  off the last field. `idle` = a clean form, not saving. */
const pageSx = (tab: Tab, idle: boolean) => ({
  maxWidth: tab === 'notifications' ? '48rem' : 480,
  '& > [data-testid="profile-form-actions"]': {
    mt: 2,
    // On Alerts, Save ends where the events table's controls do, not past them in the Detail gutter.
    ...(tab === 'notifications' && { pr: EVENTS_TABLE_GUTTER }),
    [FORM_BAR_GROUND]: (theme: Theme) => (theme.vars ?? theme).palette.background.default,
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
/* The tabs that write into the one form. Git and Security hold no draft, so they show the save bar only while
 * another tab's edit is pending: a disabled Save under Trusted devices read as Security's own. */
const DRAFT_TABS: ReadonlySet<Tab> = new Set(['profile', 'settings', 'notifications'])

const PROFILE_TABS: readonly TabDef<Tab>[] = [
  { id: 'profile', label: 'Profile' },
  { id: 'settings', label: 'Settings' },
  { id: 'notifications', label: 'Alerts' },
  { id: 'git-accounts', label: 'Git' },
  { id: 'security', label: 'Security' },
]

/* The query params a Connect callback 303s back with (`routes/git_connect.py::_profile_redirect`,
 * `routes/forge_connect.py::_profile_redirect`): `?git_connect=connected|error`, a `reason` slug on failure,
 * and the `forge` (absent from GitHub's own callback, which is the default). The slugs' wording is
 * `components/GitConnectionsCard`'s. */
const GIT_CONNECT_PARAM = 'git_connect'
const GIT_CONNECT_REASON_PARAM = 'reason'
const GIT_CONNECT_FORGE_PARAM = 'forge'

export default function ProfilePage() {
  const { admin, refresh } = useAuth()

  const [tab, selectTab] = useUrlTab(PROFILE_TABS, 'profile')

  const drafts = useUserDrafts()
  const { password, setPassword, settings, setSettings, notifications, setNotifications, seedNames } = drafts

  /* One save triple for the page: one button, one request. */
  const { busy: saving, formError: saveError, setFormError: setSaveError, run, saved, clearSaved } = useSubmit()

  /* The signed-in user's own projects; a 403 (no `projects:view` anywhere) costs only that dropdown. */
  const { projects, unavailable: projectsUnavailable } = useFocusableProjects()

  /* The outcome of a Connect round trip (what happened while away). Read through the URL (`hooks/useUrlFilters`)
   * so the router owns it and tests can render it. */
  const [searchParams, setFilters] = useUrlFilters()
  const [connectReturn, setConnectReturn] = useState<ConnectReturn | null>(null)
  /* Bumped after a Git write, so GitHub reach is re-asked. */
  const [gitChangeTick, setGitChangeTick] = useState(0)
  /* Bumped on Disconnect: remounts `TelegramEnableHere`, which reads the link once, so it cannot offer the chat
   * just unlinked. */
  const [telegramUnlinks, setTelegramUnlinks] = useState(0)
  /* `TelegramEnableHere` offers Turn on: the panel then withholds Add Telegram. */
  const [telegramOffered, setTelegramOffered] = useState(false)

  /* Consume and strip the params in one pass (`replace: true`, as the deep-link strip in `pages/Tasks.tsx`),
   * or the banner returns on every reload/back. Also switch to the Git tab, where the answer
   * renders. The banner state outlives the strip (React state) until the operator leaves. */
  const gitConnectParam = searchParams.get(GIT_CONNECT_PARAM)
  const gitConnectReasonParam = searchParams.get(GIT_CONNECT_REASON_PARAM)
  const gitConnectForgeParam = searchParams.get(GIT_CONNECT_FORGE_PARAM)
  /* `?add=1` (`lib/gitAddLink.ts`): the Add Git account form opens — on arrival, or on a link followed while already
     here (the setup guide is on every page) — and the param is dropped once read. Each read bumps the request. */
  const gitAddParam = searchParams.get(GIT_ADD_PARAM)
  const [gitAddRequest, setGitAddRequest] = useState(0)
  useEffect(() => {
    if (gitAddParam === null) return
    if (gitAddParam === '1') setGitAddRequest(n => n + 1)
    setFilters({ [GIT_ADD_PARAM]: '' })
  }, [gitAddParam, setFilters])
  useEffect(() => {
    if (gitConnectParam === null) return
    // Anything but the two server spellings is a failure: saying nothing is the state to avoid.
    const connected = gitConnectParam === 'connected'
    setConnectReturn({
      result: connected ? 'connected' : 'error',
      reason: connected ? '' : (gitConnectReasonParam ?? ''),
      forge: gitConnectForgeParam || 'github',
    })
    // ONE write: the strip and the Git switch (`?tab=`), or one clobbers the other.
    setFilters({ tab: 'git-accounts', [GIT_CONNECT_PARAM]: '', [GIT_CONNECT_REASON_PARAM]: '', [GIT_CONNECT_FORGE_PARAM]: '' })
    // `setFilters` IS IN THE ARRAY even though react-router recreates it per location and the early
    // return above makes a re-run a no-op: this repo carries no eslint config, so nothing will ever tell
    // a later editor that the body calls something the array does not name.
  }, [gitConnectParam, gitConnectReasonParam, gitConnectForgeParam, setFilters])

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

  /* The bot stored a Slack member id from a Connect code (`components/SlackConnect`). It is already saved, so
   * fold it into the draft (other edits kept) and re-read the account so the draft compares clean. */
  const handleSlackLinked = useCallback(
    (memberId: string, displayName: string) => {
      const linked = { user_id: memberId, display_name: displayName || undefined }
      setNotifications(prev => ({
        ...prev,
        methods: prev.methods.some(method => method.type === 'slack')
          ? prev.methods.map(method => (method.type === 'slack' ? { ...method, ...linked } : method))
          : [...prev.methods, { type: 'slack', ...linked }],
      }))
      refresh().catch(() => {})
    },
    [refresh],
  )

  /* The bot bound a Telegram chat and stored the method (`components/TelegramConnect`), or Turn on stored it
   * for a chat linked in another org (`TelegramEnableHere`); Disconnect dropped it.
   * Both already saved, so mirror them into the draft (other edits kept) and re-read the account. */
  const handleTelegramLinked = useCallback(() => {
    setNotifications(prev => (prev.methods.some(method => method.type === 'telegram')
      ? prev
      : { ...prev, methods: [...prev.methods, { type: 'telegram', user_id: '' }] }))
    refresh().catch(() => {})
  }, [refresh])

  /* Disconnect before any chat is linked: only the draft's method goes (nothing to unlink). */
  const handleTelegramRemoved = useCallback(() => {
    setNotifications(prev => ({ ...prev, methods: prev.methods.filter(method => method.type !== 'telegram') }))
  }, [])

  const handleTelegramUnlinked = useCallback(() => {
    handleTelegramRemoved()
    setTelegramUnlinks(n => n + 1)
    setTelegramOffered(false)
    refresh().catch(() => {})
  }, [handleTelegramRemoved, refresh])

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
    /* The same hydrate rule as the superadmin's Notifications tab; its legacy arm matters here, since the auth
     * snapshot may predate the current shape. */
    setNotifications(readNotifications(admin.preferences))
  }, [admin, hydrateTick, seedNames, setSettings, setNotifications])

  /* The ONE body the ONE save sends: every field this page owns, only those that changed. `PUT
   * /api/auth/me` reads absent as "say nothing", so a Profile-tab save does not re-assert Settings values
   * the server may no longer accept. Git is not in it: each of its writes lands on its own
   * (`components/GitConnectionsCard`).
   * Preferences are merged over the stored document (`mergedUserPreferences` in `lib/userSettings.ts`): the
   * endpoint replaces `preferences` wholesale. The comparison below needs the bare four-key document too. */
  function buildBody(current: Admin): Record<string, unknown> {
    const body = drafts.nameChanges(current)
    const next = userSettingsToPreferences(settings)
    const stored = userSettingsToPreferences(readUserSettings(current.preferences))
    /* Key-wise over the draft's own keys (both built from the same exhaustive map).
     * The guide's theme step rides this document (one write per press; the endpoint replaces `preferences`
     * wholesale), recorded only when the settings actually change — see `lib/onboarding.ts` for why this step
     * is recorded rather than derived.
     * The notifications block joins the same document, sent only when it changed; otherwise the SERVED block
     * (`methods`/`events`; legacy fields are `exclude=True`) rides along in the merge. The server re-derives
     * the legacy mirror on write (`preferences_to_store`). Compared field-wise (`notificationsEqual`): event
     * order is not stable. A Slack row not yet connected is left out: only Connect fills its id,
     * and the server refuses an empty one, so sending it would block every other save until connected. */
    const settingsChanged = Object.keys(next).some(key => next[key] !== stored[key])
    const notificationsToSend = {
      ...notifications,
      methods: methodsToSave(notifications.methods.filter(method => method.type !== 'slack' || method.user_id !== '')),
    }
    const notificationsChanged = !notificationsEqual(notificationsToSend, readNotifications(current.preferences))
    if (settingsChanged || notificationsChanged) {
      body.preferences = {
        ...mergedUserPreferences(current.preferences, settings),
        /* The theme step rides the settings arm only. */
        ...(settingsChanged ? withOnboardingStepDone(current.preferences, CHOOSE_THEME_STEP) : {}),
        ...(notificationsChanged ? { notifications: notificationsToSend } : {}),
      }
    }
    return body
  }

  /* The first broken field, or `''`. One form spans three panels and hidden panels stay mounted: native
   * validation cannot focus a control in a `display:none` panel, so it ABORTS the submit ("An invalid form
   * control is not focusable") — no request, no banner, a dead button. So the form is `noValidate` and these
   * checks enforce the rule the inputs declare (the 8-char password floor); the attribute stays for
   * semantics. */
  function firstValidationError(): string {
    if (password && passwordProblem(password).password) return `The new password must be at least ${PASSWORD_MIN} characters.`
    return ''
  }

  async function handleSave(e: FormEvent) {
    e.preventDefault()
    if (!admin) return
    /* Reported on the tab that owns the field (Profile): switch there. */
    const invalid = firstValidationError()
    if (invalid) {
      selectTab('profile')
      setSaveError(invalid)
      clearSaved()
      return
    }
    const body = buildBody(admin)
    /* Backstop: an empty PUT must never report "Saved". Implicit submission already does nothing with the
     * button disabled. */
    if (Object.keys(body).length === 0) return
    // A rejected PUT is the server's sentence (a 422 on the git field names the problem) — each has its own fix.
    await run('Failed to save', async () => {
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
    })
  }

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
      {/* `noValidate` is load-bearing: see `firstValidationError`. */}
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
                id="profile-email" label="Email" help="Ask an admin to change it, or Support if you are one."
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
                projects={projects}
                projectsUnavailable={projectsUnavailable}
                disabled={saving}
                idPrefix="profile"
                defaultTheme={admin?.default_theme}
              />
            </DetailSection>
          </Box>
        </TabPanel>

        <TabPanel id="notifications" current={tab} idPrefix="profile">
          <Box sx={PANEL_SX}>
            {/* No title (the tab says "Alerts"); the panel draws its own subheads. */}
            <DetailSection>
              {/* The panel a superadmin gets; this surface supplies data and save. `notification_methods_available` is a
              deployment capability; `?? []` for an older snapshot (absent = no channel). `disabled={saving}`: the
              same silent-loss guard. */}
              <NotificationsPanel
                value={notifications}
                onChange={setNotifications}
                methodsAvailable={admin.notification_methods_available ?? []}
                events={admin.notification_events}
                disabled={saving}
                idPrefix="profile"
                email={admin.email}
                emailVerified={admin.email_verified}
                slackConnect={SlackConnect &&
                  <SlackConnect
                    linked={notifications.methods.some(method => method.type === 'slack' && !!method.user_id)}
                    onLinked={handleSlackLinked}
                    disabled={saving}
                  />
                }
                telegramConnect={TelegramConnect &&
                  <TelegramConnect
                    onLinked={handleTelegramLinked}
                    onUnlinked={handleTelegramUnlinked}
                    onRemove={handleTelegramRemoved}
                    whoId={channelWhoId('profile', 'telegram')}
                    disabled={saving}
                  />
                }
                telegramEnable={TelegramEnableHere &&
                  <TelegramEnableHere
                    key={telegramUnlinks}
                    onEnabled={handleTelegramLinked}
                    onOffer={setTelegramOffered}
                    disabled={saving}
                  />
                }
                telegramEnableOffered={telegramOffered}
              />
            </DetailSection>
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
                addRequest={gitAddRequest}
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
          </Box>
        </TabPanel>
      </Box>

    </DetailPage>
  )
}
