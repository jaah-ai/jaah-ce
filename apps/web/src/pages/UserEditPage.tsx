import { type FormEvent, useCallback, useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { get, patch, post } from '../api'
import { useAuth } from '../auth'
import { isOrgAdminSeat } from '../lib/orgAdmin'
import Breadcrumb from '../components/Breadcrumb'
/* The Git Access select, shared with /profile (see its header). Here the roster must be what the EDITED
 * account can reach, never the superadmin's view. */
import GitAccessField from '../components/GitAccessField'
/* The Notifications panel, shared with `/profile` (see its header); this tab PATCHes `/api/users/{id}`. */
import NotificationsPanel from '../components/NotificationsPanel'
import CheckAccessPanel from '../components/access/CheckAccessPanel'
import ReachesView from '../components/access/ReachesView'
import { TabPanel, Tabs, type TabDef } from '../components/Tabs'
import { useApiRead } from '../hooks/useApiRead'
import { useEditRecord } from '../hooks/useEditRecord'
import { useSubmit } from '../hooks/useSubmit'
import { useUrlFilters } from '../hooks/useUrlFilters'
import { useUrlTab } from '../hooks/useUrlTab'
/* The settings controls, one component shared with `/profile` (see it and `lib/userSettings.ts`). */
import UserSettingsFields from '../components/UserSettingsFields'
// The notifications hydrate rule, shared with `/profile` so both read missing/legacy keys alike.
import { fullName, memberName } from '../lib/memberLabel'
import { fmtTimestamp } from '../lib/relativeTime'
import { methodsToSave, readNotifications } from '../lib/notificationPrefs'
import { mergedUserPreferences, readUserSettings } from '../lib/userSettings'
import type { UserDetail } from '../types'
import Banner from '../ui/Banner'
import DetailStatus from '../ui/detail/DetailStatus'
import FormActionBar from '../ui/FormActionBar'
import FormTextField from '../ui/FormTextField'
/* The 44px touch floors from their single home, `ui/listStyles`. */
import { ELLIPSIS_SX, tapTargetSx } from '../ui/listStyles'
/* The read-only meta strip, shared with `GroupEditPage` (em-dash for none; provenance renders a NAME). */
import { MetaActors, MetaField, MetaRow } from '../ui/MetaRow'
import PageHeader from '../ui/PageHeader'
import SectionCard from '../ui/SectionCard'
import SettingCheckbox from '../ui/SettingCheckbox'
import SubmitButton from '../ui/SubmitButton'
import { listUserGitCredentials, type GitCredential } from './credentials/api'
import { NameAndPasswordFields, useFocusableProjects, useUserDrafts } from './users/UserFields'

/** An address breaks anywhere rather than overflow: it has no spaces to wrap at. */
const WHOLE_ADDRESS_SX = { overflowWrap: 'anywhere' } as const
/* THE CRUMB REPEATS THE TITLE: the title below states it whole, so the trail's last crumb keeps one line —
   a nameless user's address otherwise wrapped over two lines above the same address over three (390px). */
const CRUMB_ONE_LINE_SX = { ...ELLIPSIS_SX, display: 'block' } as const

// Edit User page at /users/:id. An Org Owner/Admin (operator or tenant; the server scopes the rows) edits;
// a `users:view` holder sees every tab read-only, with no save controls. Tabs: Profile, Access, Check
// access, Settings, Notifications, and — for the operator alone — System: the bypass flag and the sign-in
// overrides (password door, two-factor exemption). Profile, Settings and Notifications PATCH only their own fields via
// PATCH /api/users/{id}; Access lists what the user holds and, for a viewer who may manage their org, adds
// and removes bindings; Check is a read (both `components/access`). Tab shell: the shared
// `components/Tabs`.
// Settings and Notifications are the admin half of shared surfaces (`components/UserSettingsFields` +
// `lib/userSettings`, `components/NotificationsPanel` + `lib/notificationPrefs`); this page keeps only the
// card, the banners and the PATCH.

// `access` then `check`: what the user holds, then why one thing is (or is not) allowed.
// `system` last: the operator's alone (`offeredTabs`).
const TABS = ['profile', 'access', 'check', 'settings', 'notifications', 'system'] as const
type Tab = (typeof TABS)[number]

const USER_TABS: readonly TabDef<Tab>[] = [
  { id: 'profile', label: 'Profile' },
  { id: 'access', label: 'Access' },
  { id: 'check', label: 'Check' },
  { id: 'settings', label: 'Settings' },
  { id: 'notifications', label: 'Notifications' },
  { id: 'system', label: 'System' },
]

/* The password-door override as the select's value: `null` (follow the role rule) needs a string of its own. */
const PASSWORD_DOOR_OPTIONS = [
  { value: 'role', label: 'Default for role', wire: null },
  { value: 'on', label: 'Allowed', wire: true },
  { value: 'off', label: 'Off', wire: false },
] as const

function passwordDoorValue(override: boolean | null | undefined): string {
  return override === true ? 'on' : override === false ? 'off' : 'role'
}

/* Parity with the 480px form-card cap: the Settings and Notifications forms. */
const FORM_CARD_SX = { mt: 1, maxWidth: 480 } as const

/* The profile and access tabs' width: a chain or a project list reads at desktop without wrapping every line, and the
   Email field holds the whole address the title above it shows (at 480px a long one read
   "…@engineering.exa"). */
const WIDE_CARD_SX = { mt: 1, maxWidth: 720 } as const

/* What `PATCH /api/users/{id}` actually answers: `response_model=UserOut`
 * (the api's users routes), so `effective_permission_keys` is ABSENT, not empty.
 * `patch<T>` is an assertion, not a check: treating absent as "holds nothing" once cost an audited
 * revocation. This type makes reading such a field off a PATCH result a compile error; the runtime fix is
 * `mergeHeldUser` (the fields are optional on `UserDetail`, so this still assigns).
 * Derived with `Omit`, never hand-listed, so a new such field cannot silently join. */
type UserPatchResponse = Omit<
  UserDetail,
  'effective_permission_keys'
  /* `accessible_project_ids` too: not on `UserOut`, so a PATCH is silent about it. */
  | 'accessible_project_ids'
  /* Likewise `notification_events`. */
  | 'notification_events'
>

/* Which drafts a landed write may re-seed. Every panel stays mounted, so re-seeding every draft from one
 * save's response would discard unsaved edits on other cards. Each handler names its own draft group.
 * `'all'` is the initial GET; `'access'` re-seeds none. */
type HydrateScope = 'all' | 'profile' | 'access' | 'settings' | 'notifications'

/* Merge a write's response ONTO the held record, so an absent field says nothing (belt to
 * `UserPatchResponse`'s braces). */
function mergeHeldUser(prev: UserDetail | null, incoming: UserDetail, scope: HydrateScope): UserDetail {
  if (prev === null || scope === 'all') return incoming
  return {
    ...prev,
    ...incoming,
    // `??`: an empty ARRAY is a real "holds nothing"; only null/undefined means "not said".
    effective_permission_keys: incoming.effective_permission_keys ?? prev.effective_permission_keys,
    // Same `??`: a PATCH is silent about the roster; `[]` still lands.
    accessible_project_ids: incoming.accessible_project_ids ?? prev.accessible_project_ids,
    notification_events: incoming.notification_events ?? prev.notification_events,
  }
}

/* No per-user credential picker or `GET /api/credentials` roster: one `slack` credential serves the whole
 * deployment (Settings, `notifications_slack_credential_id`). DO NOT REINTRODUCE EITHER — it
 * would offer the SUPERADMIN's view of credentials as the edited account's routing. Availability is
 * `notification_methods_available` on the identity payload. */

export default function UserEditPage() {
  const { id } = useParams<{ id: string }>()
  const { admin, refresh } = useAuth()
  // Writes are `@org_admin` (`routes/users.py`). The bypass flag itself is the operator's: only a superadmin
  // may change it, and a tenant org can hold none (`_refuse_superadmin_subject`, `update_user`).
  const canWrite = isOrgAdminSeat(admin)
  // The System tab (the bypass flag, the sign-in overrides) is the operator's alone; `routes/users.py` refuses
  // its writes to anyone else.
  const isOperator = admin?.is_operator === true
  const [user, setUser] = useState<UserDetail | null>(null)
  const isSelf = user !== null && admin?.id === user.id
  const emailLocked = !canWrite || isSelf
  // Access and Check answer only the user themself or an Org Owner/Admin
  // (`routes/access.py::_self_or_org_admin`); anyone else would get a bare refusal.
  const offeredTabs = USER_TABS.filter(t =>
    (t.id !== 'system' || isOperator) && (canWrite || isSelf || (t.id !== 'access' && t.id !== 'check')))
  // The tab lives in the URL (hooks/useUrlTab.ts).
  const [tab, selectTab] = useUrlTab(offeredTabs, 'profile')
  // `?tab=reaches` is the Access tab's old address: a saved link lands on Access.
  const [searchParams, setFilters] = useUrlFilters()
  const legacyReaches = searchParams.get('tab') === 'reaches'
  useEffect(() => {
    if (legacyReaches) setFilters({ tab: 'access' })
  }, [legacyReaches, setFilters])
  // Sticky once opened, so switching tabs keeps what was loaded (and a Check result). An effect, not
  // onSelect: a link can land on the tab.
  const [accessOpened, setAccessOpened] = useState(false)
  const [checkOpened, setCheckOpened] = useState(false)
  useEffect(() => {
    if (tab === 'access') setAccessOpened(true)
    if (tab === 'check') setCheckOpened(true)
  }, [tab])

  // Profile form
  const [email, setEmail] = useState('')
  const [description, setDescription] = useState('')
  const drafts = useUserDrafts()
  const { seedNames, setSettings, setNotifications } = drafts
  /* Git Access: the draft and its roster (`null` not told, `[]` none, `gitCredentialsUnavailable` could not
   * ask). A separate fetch from `GET /api/credentials`, which is the SUPERADMIN's view. */
  const [gitCredentialId, setGitCredentialId] = useState<string | null>(null)
  const { busy: profileSaving, formError: profileError, setFormError: setProfileError, run: runProfile, saved: profileSaved, clearSaved: clearProfileSaved } = useSubmit()

  // Access
  const { busy: accessSaving, formError: accessError, setFormError: setAccessError, run: runAccess, saved: accessSaved } = useSubmit()

  // Account (disable/enable): `POST /api/users/{id}/disable|enable`, `@org_admin` like the other user writes.
  const { busy: accountSaving, formError: accountError, run: runAccount } = useSubmit()

  const { busy: settingsSaving, formError: settingsError, setFormError: setSettingsError, run: runSettings, saved: settingsSaved } = useSubmit()

  /* The SOURCE roster (the viewer's), narrowed to the edited user's `accessible_project_ids` at `settingsProjects`. */
  const { projects: allProjects, unavailable: projectsUnavailable } = useFocusableProjects()
  const { busy: notifSaving, formError: notifError, setFormError: setNotifError, run: runNotif, saved: notifSaved } = useSubmit()

  /* Seed form fields from a loaded or saved record; `scope` names which draft groups it owns
   * (`HydrateScope`). */
  const hydrate = useCallback((u: UserDetail, scope: HydrateScope = 'all') => {
    setUser(prev => mergeHeldUser(prev, u, scope))
    if (scope === 'all' || scope === 'profile') {
      setEmail(u.email)
      setDescription(u.description)
      seedNames(u)
      // `?? null` for an older payload: absent means "connected nothing", never "leave the draft".
      setGitCredentialId(u.git_credential_id ?? null)
    }
    if (scope === 'all' || scope === 'settings') {
      setSettings(readUserSettings(u.preferences))
    }
    if (scope === 'all' || scope === 'notifications') {
      setNotifications(readNotifications(u.preferences))
    }
  }, [seedNames, setSettings, setNotifications])

  /* No pre-fetch id-shape guard: FastAPI answers a malformed uuid with a 422, mapped to not-found alongside
   * 404, so a local regex would only add a second authority on the id's shape. */
  const { confirm, confirmDialog, loading, notFound, loadError: error } = useEditRecord(
    id ?? null, userId => get<UserDetail>(`/api/users/${userId}`), hydrate,
    { loadFallback: 'Failed to load user', fixedLoadError: true, notFoundStatuses: [404, 422] },
  )

  /* The Notifications tab fetches nothing (it needs the stored document and one identity-payload boolean). */

  /* The edited account's git-capable credentials, fetched on MOUNT (Profile is the default tab).
   * `GET /api/users/{id}/git-credentials`, NOT `GET /api/credentials`: a superadmin's roster would allow a
   * selection `git_access.resolve_for_user` refuses at the first push. The server re-checks on write; this
   * keeps the FORM honest. A failed roster disables only this control (hint, no banner). */
  const gitRoster = useApiRead(
    ['user-git-credentials', id],
    () => listUserGitCredentials(id as string).then(data => (Array.isArray(data.credentials) ? data.credentials : [])),
    'Failed to load git credentials',
    { enabled: !!id },
  )
  const gitCredentials: GitCredential[] | null = gitRoster.data
  const gitCredentialsUnavailable = gitRoster.error !== ''

  /* If the edited user IS the logged-in admin, refresh the auth context so a theme change applies now. Its
   * failure is reported beside the tab's "Saved", never as the save's own refusal: the PATCH has landed. */
  const refreshSelfIfNeeded = useCallback(async (report: (message: string) => void) => {
    if (!(user && admin && user.id === admin.id)) return
    try {
      await refresh()
    } catch {
      report('Saved, but this page could not reload your account. Reload the page to see the stored values.')
    }
  }, [user, admin, refresh])

  async function handleProfileSave(e: FormEvent) {
    e.preventDefault()
    if (!user) return
    // A minimal patch: only changed fields, password only when provided.
    const body = drafts.nameChanges(user)
    if (email !== user.email) body.email = email
    if (description !== user.description) body.description = description
    /* Diffed, and `null` is a value: clearing sends an explicit `null`, which the server reads from the
     * PROVIDED SET as disconnect. A truthiness guard would make disconnecting impossible. */
    if (gitCredentialId !== (user.git_credential_id ?? null)) body.git_credential_id = gitCredentialId
    await runProfile('Failed to save profile', async () => {
      const updated = await patch<UserPatchResponse>(`/api/users/${user.id}`, body)
      hydrate(updated, 'profile')
      await refreshSelfIfNeeded(setProfileError)
    })
  }

  async function handleSuperadminToggle() {
    if (!user) return
    await runAccess('Failed to update admin access', async () => {
      const updated = await patch<UserPatchResponse>(`/api/users/${user.id}`, { is_superadmin: !user.is_superadmin })
      hydrate(updated, 'access')
      await refreshSelfIfNeeded(setAccessError)
    })
  }

  /* The sign-in overrides (`password_login`, `mfa_exempt`): one field per write, applied on change like the
   * admin toggle. Shares the System card's banners with it. */
  async function saveSignInOverride(body: { password_login?: boolean | null; mfa_exempt?: boolean }) {
    if (!user) return
    await runAccess('Failed to save sign-in setting', async () => {
      hydrate(await patch<UserPatchResponse>(`/api/users/${user.id}`, body), 'access')
    })
  }

  async function handleMfaExemptToggle(exempt: boolean) {
    if (!user) return
    if (exempt) {
      const ok = await confirm({
        title: 'Skip two-factor?',
        identity: user.email,
        message: 'They sign in without a code.',
        confirmLabel: 'Skip',
        destructive: true,
      })
      if (!ok) return
    }
    await saveSignInOverride({ mfa_exempt: exempt })
  }

  async function handleDisabledToggle() {
    if (!user) return
    const disabling = user.disabled_at === null
    if (disabling) {
      const ok = await confirm({
        title: 'Disable account',
        identity: user.email,
        message: 'They are signed out and cannot sign in until enabled.',
        confirmLabel: 'Disable',
        destructive: true,
      })
      if (!ok) return
    }
    await runAccount(disabling ? 'Failed to disable account' : 'Failed to enable account', async () => {
      hydrate(await post<UserPatchResponse>(`/api/users/${user.id}/${disabling ? 'disable' : 'enable'}`), 'access')
    })
  }

  // Reset two-factor: `POST /api/users/{id}/mfa/reset`, guarded like disable; it also ends their sessions.
  async function handleMfaReset() {
    if (!user) return
    const ok = await confirm({
      title: 'Reset two-factor',
      message: `Reset two-factor for ${user.email}? They set it up again at next sign-in, and their sessions end.`,
      confirmLabel: 'Reset',
      destructive: true,
    })
    if (!ok) return
    await runAccount('Failed to reset two-factor', async () => {
      hydrate(await post<UserPatchResponse>(`/api/users/${user.id}/mfa/reset`), 'access')
    })
  }

  // Reset SSO link: `POST /api/users/{id}/sso/reset`, guarded like disable. Their next SSO sign-in links again.
  async function handleSsoLinkReset() {
    if (!user) return
    const ok = await confirm({
      title: 'Reset SSO link',
      message: `Reset the SSO link for ${user.email}? Only if their identity provider account changed: whoever next signs in with their email is linked.`,
      confirmLabel: 'Reset',
      destructive: true,
    })
    if (!ok) return
    await runAccount('Failed to reset the SSO link', async () => {
      hydrate(await post<UserPatchResponse>(`/api/users/${user.id}/sso/reset`), 'access')
    })
  }

  async function handleSettingsSave(e: FormEvent) {
    e.preventDefault()
    if (!user) return
    /* The merge is load-bearing: `PATCH /api/users/{id}` replaces `preferences` WHOLESALE and
     * `userSettingsToPreferences` is PARTIAL, so sending it alone would drop `notifications` and
     * `nav_collapsed_groups`. `mergedUserPreferences` is the shared merge.
     * Every draft key is written every time: `readUserSettings` degrades legacy/unreadable values to
     * defaults, so any save also repairs them. */
    const preferences = mergedUserPreferences(user.preferences, drafts.settings)
    await runSettings('Failed to save settings', async () => {
      const updated = await patch<UserPatchResponse>(`/api/users/${user.id}`, { preferences })
      hydrate(updated, 'settings')
      await refreshSelfIfNeeded(setSettingsError)
    })
  }

  async function handleNotificationsSave(e: FormEvent) {
    e.preventDefault()
    if (!user) return
    /* The draft IS the wire document (no sentinel translation). An empty `methods` list means "deliver
     * nothing". Spread stored preferences first: PATCH replaces `preferences` wholesale (R2.5). */
    const notifications = { ...drafts.notifications, methods: methodsToSave(drafts.notifications.methods) }
    const preferences = { ...(user.preferences ?? {}), notifications }
    await runNotif('Failed to save notifications', async () => {
      const updated = await patch<UserPatchResponse>(`/api/users/${user.id}`, { preferences })
      hydrate(updated, 'notifications')
      await refreshSelfIfNeeded(setNotifError)
    })
  }

  if (loading || notFound || error || !user) {
    return (
      <DetailStatus
        trail={<Breadcrumb items={[{ label: 'People', to: '/users' }, { label: 'User' }]} />}
        loading={loading}
        notFound={notFound && 'User not found'}
        error={error || 'Failed to load user'}
      />
    )
  }

  // Self-demotion would hit the server's 409 guard; disabled with a visible reason (server stays
  // authoritative).
  const selfDemoteBlocked = isSelf && user.is_superadmin
  // An invitation not (or never to be) accepted: the state line says which instead of "Active". Disable and
  // enable work as for any account (an admin's disable outlasts the accept).
  const invitation = user.invitation_status ?? null

  /* The projects the EDITED user can see. An absent `accessible_project_ids` (a PATCH response) is
   * UNAVAILABLE — never `[]` (claims none) nor the viewer's whole roster (offers unreachable projects). */
  const accessibleProjectIds = user.accessible_project_ids
  const settingsProjects = allProjects === null || accessibleProjectIds === undefined
    ? null
    : allProjects.filter(p => accessibleProjectIds.includes(p.id))
  const settingsProjectsUnavailable = projectsUnavailable || accessibleProjectIds === undefined

  return (
    <Box>
      <PageHeader
        breadcrumb={(
          <Breadcrumb
            items={[
              { label: 'People', to: '/users' },
              {
                label: <Box component="span" data-testid="user-crumb" title={memberName(user)} sx={CRUMB_ONE_LINE_SX}>{memberName(user)}</Box>,
                text: memberName(user),
              },
            ]}
          />
        )}
        title={
          <Box component="span" sx={WHOLE_ADDRESS_SX}>
            {memberName(user)}
            {isSelf && (
              <Typography component="span" variant="body2" sx={{ color: 'text.secondary' }}> (you)</Typography>
            )}
          </Box>
        }
        /* THE WHOLE ADDRESS: the roster cuts it to one line and a phone has no hover, so the open user
           states it, wrapped, under a name (a nameless user's title is the address already). */
        subtitle={fullName(user) ? <Box component="span" data-testid="user-email" sx={WHOLE_ADDRESS_SX}>{user.email}</Box> : undefined}
      />

      {/* `overflowMenu`: one row at every width, the tabs that fit and the rest behind More. A wrapped strip spent a
          second row at 390px; a scrolled one hid tabs behind its chevron beside the People list. */}
      <Tabs
        tabs={offeredTabs}
        tab={tab}
        onSelect={selectTab}
        label="User sections"
        idPrefix="user"
        overflowMenu
        sticky="default"
      />

      <TabPanel id="profile" current={tab} idPrefix="user">
        <Box component="form" onSubmit={e => void handleProfileSave(e)} sx={WIDE_CARD_SX}>
          <SectionCard>
            {/* `spacing={2}` clears the 9px a SHRUNK MUI label overhangs its box (at 0.5 a populated field collided with
            the one above by 6px). `ProfilePage`'s form still has 0.5; the app-wide fix belongs in `ui/FormField`. */}
            <Stack spacing={2}>
              {profileError && <Banner severity="error">{profileError}</Banner>}
              {profileSaved && <Banner severity="success">Saved</Banner>}
              {/* `required` rides `htmlInput`, never MUI's `required` prop (asterisk). */}
              {/* Nobody changes their OWN login email, an org admin included (the server 403s it). */}
              {/* Locked, it WRAPS (a textarea) and carries the address as its `title`: an input cuts a long one
              ("…@engineering.example-enterp" beside the roster) and nobody can scroll a disabled field. */}
              <FormTextField
                label="Email" hint={isSelf ? 'Contact Support to change your own email.' : undefined}
                id="edit-email"
                disabled={emailLocked}
                multiline={emailLocked}
                type="email"
                value={email}
                onChange={setEmail}
                slotProps={{ htmlInput: { required: true, title: emailLocked ? email : undefined } }}
              />
              <NameAndPasswordFields idPrefix="edit" disabled={!canWrite} drafts={drafts}>
                {/* Free text, so `multiline` (as `CredentialEditPage`'s Description). */}
                <FormTextField label="Description" id="edit-description" disabled={!canWrite} multiline rows={3} value={description} onChange={setDescription} />
              </NameAndPasswordFields>

              {/* Git Access, after identity and before the provenance strip: it is about what the runners DO.
              No "Connect GitHub" here: the App authorization runs through the SIGNED-IN person's GitHub, so it would
              attach the ADMIN's account to someone else's row. */}
              <GitAccessField
                value={gitCredentialId}
                onChange={next => { setGitCredentialId(next); clearProfileSaved() }}
                credentials={gitCredentialsUnavailable ? null : gitCredentials}
                unavailable={gitCredentialsUnavailable}
                disabled={profileSaving || !canWrite}
                // `'user'`: this page's control-id convention, distinct from `/profile`'s.
                idPrefix="user"
              />

              <MetaRow>
                <MetaField label="Created" iso={user.created_at} />
                <MetaField label="Activated" iso={user.activated_at} />
                <MetaField label="Last seen" iso={user.last_seen_at} />
                {/* Provenance ("who") after the lifecycle triple ("when"), so the grouping survives a wrap. */}
                <MetaActors createdBy={user.created_by} updatedBy={user.updated_by} />
              </MetaRow>

              {/* A plain `FormActionBar`, so the list-detail pane keeps its right end clear of the Help launcher
                  (`components/ListDetail.tsx`); full width on a phone, as `/profile`'s Save. */}
              {canWrite && (
                <FormActionBar plain testId="user-profile-actions" stretchTestIds={['user-profile-save']}>
                  <SubmitButton busy={profileSaving} testId="user-profile-save">Save profile</SubmitButton>
                </FormActionBar>
              )}
            </Stack>
          </SectionCard>
        </Box>
        {/* Disable/enable: an org admin's, but a superadmin's account only an operator's (`_refuse_superadmin_subject`). */}
        {canWrite && (isOperator || !user.is_superadmin) && (
          <Box sx={WIDE_CARD_SX}>
            <SectionCard>
              <Stack spacing={1}>
                {accountError && <Banner severity="error">{accountError}</Banner>}
                <Box>
                  <Typography variant="body2" component="span" sx={{ display: 'block', mb: 0.5 }}>
                    Account
                  </Typography>
                  <Typography variant="body2" data-testid="user-account-state" sx={{ mb: 1, color: user.disabled_at ? 'error.main' : 'text.secondary' }}>
                    {invitation === 'pending'
                      ? "Invited — hasn't accepted yet"
                      : invitation === 'declined'
                        ? 'Declined the invitation'
                        : user.disabled_at
                          ? `Disabled — by ${user.disabled_by ?? 'unknown'}, ${fmtTimestamp(user.disabled_at)}`
                          : 'Active'}
                  </Typography>
                  <Button
                    type="button"
                    variant="outlined"
                    color={user.disabled_at ? 'primary' : 'error'}
                    disabled={accountSaving || isSelf}
                    onClick={() => void handleDisabledToggle()}
                    sx={tapTargetSx}
                  >
                    {user.disabled_at ? 'Enable account' : 'Disable account'}
                  </Button>
                  {isSelf && (
                    <Typography variant="caption" sx={{ display: 'block', mt: 0.5, color: 'text.secondary' }}>
                      You can't disable your own account.
                    </Typography>
                  )}
                </Box>
                <Box>
                  <Typography variant="body2" component="span" sx={{ display: 'block', mb: 0.5 }}>
                    Two-factor
                  </Typography>
                  <Typography variant="body2" data-testid="user-mfa-state" sx={{ color: 'text.secondary' }}>
                    {user.mfa_enrolled ? 'On' : 'Off'}
                  </Typography>
                  {user.mfa_enrolled && !isSelf && (
                    <Button
                      type="button"
                      variant="outlined"
                      color="error"
                      disabled={accountSaving}
                      onClick={() => void handleMfaReset()}
                      sx={[tapTargetSx, { mt: 1 }]}
                    >
                      Reset two-factor
                    </Button>
                  )}
                </Box>
                {/* The org's SAML/OIDC link (`sso_signin.bind_subject`): shown once linked, the repair for a member
                    whose IdP account changed (the login page's `sso_subject_mismatch`). */}
                {user.sso_linked && (
                  <Box>
                    <Typography variant="body2" component="span" sx={{ display: 'block', mb: 0.5 }}>
                      SSO link
                    </Typography>
                    <Typography variant="body2" data-testid="user-sso-link-state" sx={{ color: 'text.secondary' }}>
                      Linked to an identity provider account
                    </Typography>
                    {!isSelf && (
                      <Button
                        type="button"
                        variant="outlined"
                        color="error"
                        disabled={accountSaving}
                        onClick={() => void handleSsoLinkReset()}
                        sx={[tapTargetSx, { mt: 1 }]}
                      >
                        Reset SSO link
                      </Button>
                    )}
                  </Box>
                )}
              </Stack>
            </SectionCard>
          </Box>
        )}
      </TabPanel>

      <TabPanel id="access" current={tab} idPrefix="user">
        <Box sx={WIDE_CARD_SX}>
          {/* Mounted on first open: a read the Profile tab does not need. */}
          {(tab === 'access' || accessOpened) && (
            <ReachesView
              userId={user.id}
              userName={memberName(user)}
              orgId={user.org_id}
              groups={[...new Map(user.memberships.map(m => [m.group, { name: m.group, is_everyone: m.is_everyone }])).values()]}
            />
          )}
        </Box>
      </TabPanel>

      <TabPanel id="check" current={tab} idPrefix="user">
        <Box sx={WIDE_CARD_SX}>
          {(tab === 'check' || checkOpened) && <CheckAccessPanel userId={user.id} />}
        </Box>
      </TabPanel>

      <TabPanel id="settings" current={tab} idPrefix="user">
        <Box component="form" onSubmit={e => void handleSettingsSave(e)} sx={FORM_CARD_SX}>
          <SectionCard>
            <Stack spacing={1}>
              {settingsError && <Banner severity="error">{settingsError}</Banner>}
              {settingsSaved && <Banner severity="success">Saved</Banner>}

              {/* Fields from the shared component; the card, banners and save stay here (the legitimate difference is
              endpoint and chrome, never which settings). */}
              <UserSettingsFields
                value={drafts.settings}
                onChange={setSettings}
                projects={settingsProjects}
                projectsUnavailable={settingsProjectsUnavailable}
                disabled={!canWrite}
                idPrefix="user"
                defaultTheme={admin?.default_theme}
              />

              {canWrite && (
                <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <SubmitButton busy={settingsSaving}>Save settings</SubmitButton>
                </Box>
              )}
            </Stack>
          </SectionCard>
        </Box>
      </TabPanel>

      {/* The operator's controls: the bypass flag and the sign-in overrides. Offered to the operator alone. */}
      {isOperator && (
        <TabPanel id="system" current={tab} idPrefix="user">
          <Box sx={FORM_CARD_SX}>
            <SectionCard>
              <Stack spacing={2}>
                {accessError && <Banner severity="error">{accessError}</Banner>}
                {accessSaved && <Banner severity="success">Saved</Banner>}
                <Box>
                  <Typography variant="body2" component="span" sx={{ display: 'block', mb: 0.5 }}>
                    Admin access
                  </Typography>
                  <Button
                    type="button"
                    variant={user.is_superadmin ? 'contained' : 'outlined'}
                    color={user.is_superadmin ? 'primary' : 'inherit'}
                    disabled={accessSaving || selfDemoteBlocked}
                    aria-pressed={user.is_superadmin}
                    onClick={() => void handleSuperadminToggle()}
                    sx={tapTargetSx}
                  >
                    {user.is_superadmin ? 'admin' : 'standard'}
                  </Button>
                  {selfDemoteBlocked && (
                    <Typography variant="caption" sx={{ display: 'block', mt: 0.5, color: 'text.secondary' }}>
                      You can't remove your own admin access.
                    </Typography>
                  )}
                </Box>
                {/* The hint reads the server's answer (`password_door`), never the role rule re-derived here. */}
                <FormTextField
                  label="Password sign-in"
                  hint={`Currently ${user.password_door === false ? 'off' : 'allowed'}.`}
                  id="user-password-sign-in"
                  disabled={accessSaving}
                  value={passwordDoorValue(user.password_login)}
                  onChange={v => {
                    const picked = PASSWORD_DOOR_OPTIONS.find(o => o.value === v)
                    if (picked) void saveSignInOverride({ password_login: picked.wire })
                  }}
                  options={PASSWORD_DOOR_OPTIONS}
                />
                <SettingCheckbox
                  control="switch"
                  id="user-mfa-exempt"
                  name="mfa_exempt"
                  label="Skip two-factor"
                  checked={user.mfa_exempt === true}
                  disabled={accessSaving}
                  onChange={v => void handleMfaExemptToggle(v)}
                />
              </Stack>
            </SectionCard>
          </Box>
        </TabPanel>
      )}

      <TabPanel id="notifications" current={tab} idPrefix="user">
        <Box component="form" onSubmit={e => void handleNotificationsSave(e)} sx={FORM_CARD_SX}>
          <SectionCard>
            <Stack spacing={2}>
              {notifError && <Banner severity="error">{notifError}</Banner>}
              {notifSaved && <Banner severity="success">Saved</Banner>}

              {/* Panel from the shared component; card, banners and save here. `methodsAvailable` is the reading admin's
              own identity payload — one `slack` credential serves every account. `?? []` for a snapshot predating
              the field: absent means "no channel available". */}
              <NotificationsPanel
                value={drafts.notifications}
                onChange={setNotifications}
                methodsAvailable={admin?.notification_methods_available ?? []}
                // The EDITED user's events: billing and the node alerts follow their org role, not the viewer's.
                events={user.notification_events}
                // Locked while saving: this tab re-seeds from the PATCH response, which would destroy a mid-save keystroke.
                disabled={notifSaving || !canWrite}
                // `'user'`: this page's control-id convention, distinct from `/profile`'s.
                idPrefix="user"
                // The edited user's login email: where an email method delivers.
                email={user.email}
                emailVerified={user.email_verified}
              />

              {canWrite && (
                <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <SubmitButton busy={notifSaving}>Save notifications</SubmitButton>
                </Box>
              )}
            </Stack>
          </SectionCard>
        </Box>
      </TabPanel>
      {/* One dialog for every tab's confirms (Profile's account actions, System's two-factor skip). */}
      {confirmDialog}
    </Box>
  )
}
