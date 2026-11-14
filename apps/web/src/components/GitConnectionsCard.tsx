import { type KeyboardEvent, useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { apiDetail, ApiError } from '../api'
import { useAuth } from '../auth'
import { useApiRead } from '../hooks/useApiRead'
import { useBusyIds } from '../hooks/useBusyIds'
import { useSubmit } from '../hooks/useSubmit'
import { leaveForAuthorize } from '../lib/leaveForAuthorize'
import { connectForgeInPopup, openConnectPopup } from './projects/githubOAuthPopup'
import {
  deleteGitConnection,
  gitConnectionsQuery,
  listGitCredentialsForRoot,
  postGitConnectionPat,
  putGitConnection,
  startForgeConnect,
  type GitConnection,
  type GitCredential,
  type GitPatBody,
} from '../pages/credentials/api'
import { integrationPath } from '../pages/credentials/integrations/api'
import Banner from '../ui/Banner'
import { credentialMark } from '../ui/credentialMarks'
import type { Explain } from '../ui/explain'
import FeedList from '../ui/feed/FeedList'
import type { FeedRowSpec, OverflowItem } from '../ui/feed/types'
import FormTextField from '../ui/FormTextField'
import { ROW_ACTION_SX } from '../ui/listStyles'
import LoadingState from '../ui/LoadingState'
import SectionHeader from '../ui/SectionHeader'
import { monoSx } from '../ui/listStyles'
import StatusChip from '../ui/StatusChip'
import { FORGE_LABELS, type Forge } from '../types'
import { isEnabled } from '../lib/editionFeatures'
import { FONT_SIZE, FONT_WEIGHT, SIZE } from '../theme'
import Tip from '../ui/Tip'

/* Whether a forge's sign-in (its OAuth app) is offered. Never without forge Connect (D22): no Jaah OAuth app to
   sign in through, so every forge takes a pasted token. */
const connectOffered = (row: GitConnection): boolean => isEnabled('forge_connect') && row.connect.available

/* Where an operator sets up a forge's sign-in app: its Settings > Integrations section, mirroring the server's
   `ForgeOAuthSpec.settings_section` (apps/api/app/forge_oauth.py) until `connect` carries it. */
const CONNECT_SETUP_SECTION: Readonly<Record<string, string>> = {
  github: 'github', gitlab: 'gitlab', bitbucket: 'bitbucket', azure_devops: 'microsoft',
}

/** The Method hint while a forge's sign-in is not set up. Everyone else reads the server's one line (ask an
 *  operator); the operator reads one short line linking to the setting, the server's full why on hover. */
function ConnectUnavailableHint({ target }: { target: GitConnection }) {
  const { admin } = useAuth()
  const reason = target.connect.reason
  if (!reason) return null
  const section = CONNECT_SETUP_SECTION[target.forge]
  if (admin?.is_operator !== true || !section) return <>{reason}</>
  return (
    <Tip title={reason}>
      <Box component="span" data-testid="git-add-connect-setup">
        Sign-in is not set up.{' '}
        <Link component={RouterLink} to={`/settings?area=integrations&section=${section}`}>Set it up</Link>
      </Box>
    </Tip>
  )
}

/* ── PROFILE > GIT ACCOUNTS: A LIST OF WHAT IS CONNECTED, PLUS ONE ADD FORM ──────────────
 *
 * The server's `GET /api/users/{id}/git-connections` is the whole model: the four fixed roots plus each
 * self-managed GitLab the person connected — one entry per connected account (any number per root, one of
 * them the root's default) — each with its state and whether Connect works. The LIST (`ui/feed/FeedList`) holds
 * only the accounts (connected or needing a reconnect) — a forge nobody connected is not a row (operator decision:
 * no itemised "Not connected" list). The `+` in the header opens ONE form: pick the type (any forge, again
 * for another account), then how (Connect, paste a token, an existing credential). A row's ⋯ offers Reconnect and
 * Disconnect, and Make default on a root holding several accounts.
 *
 * Every rule — which credential fits which root, whether a token works — is the server's, so a write's
 * refusal is shown where it was made, in the server's words.
 *
 * Self-service only (the routes 404 anyone else), so /users/:id keeps `components/GitAccessField`.
 *
 * QUALITY BAR: forge labels, roots and account names WRAP, never clip — no `noWrap`, no ellipsis,
 * `overflowWrap: anywhere` for a long unbroken account or host.
 */

/** The OAuth rows are named `<Forge> (<account>)` (`routes/forge_connect.py::_connected_credential`,
 *  `routes/git_connect.py::_credential_name`); the account is the part a person recognises. A pasted
 *  token's row (`<Forge> token (<root>)`, `routes/git_connections.py::post_git_connection_pat`) names no
 *  account, so it is `null` and the row says it is connected with a token. Any other name is shown whole. */
export function accountName(connection: GitConnection): string | null {
  const name = connection.credential?.name
  if (!name) return null
  const { forge_label: label, root } = connection
  if (name === `${label} token (${root})`) return null
  const prefix = `${label} (`
  return name.startsWith(prefix) && name.endsWith(')') ? name.slice(prefix.length, -1) : name
}

/** Wrap anywhere rather than overflow: a deep sub-path host has no spaces. */
const WRAP_SX = { overflowWrap: 'anywhere', whiteSpace: 'normal', minWidth: 0 } as const

/** The add form's and the Reconnect panel's frame: a flat column under the list. */
const FORM_SX = { display: 'flex', flexDirection: 'column', gap: 1.5, pt: 0.5 } as const

/** `host[/sub-path]` from what a person types (`https://git.example.org/gitlab/` → `git.example.org/gitlab`),
 *  the root shape the server keys on; the server validates it. */
export function selfManagedRoot(baseUrl: string): string {
  return baseUrl.trim().replace(/^https?:\/\//i, '').replace(/\/+$/, '').toLowerCase()
}

/** A root holding an account — the table's rows. `not_connected` is the add form's, never a row. */
const isListed = (row: GitConnection) => row.state !== 'not_connected'

/** One account's identity among the rows: its credential (a `not_connected` entry, the root itself). */
const rowKey = (row: GitConnection) => row.credential?.id ?? row.root


/* The card renders INSIDE the page's one form (`ProfilePage`), so its own forms are not `<form>`s: a nested
 * form's submit bubbles to the page's Save. Enter in one of these inputs runs the form's own action instead,
 * and never reaches the page (implicit submission would save the profile). */
function enterRuns(action: () => void) {
  return (e: KeyboardEvent<HTMLElement>) => {
    if (e.key !== 'Enter' || !(e.target instanceof HTMLInputElement)) return
    e.preventDefault()
    action()
  }
}

/** The catalog vendor Connect screen a fixed root signs in through, or null where the forge has no catalog card
 *  (Bitbucket, Azure DevOps) and the card starts Connect itself. A self-managed GitLab is never one. */
function catalogConnectPath(row: GitConnection): string | null {
  if (row.self_managed || (row.forge !== 'github' && row.forge !== 'gitlab')) return null
  return integrationPath(row.forge, '/profile')
}

/** Start Connect for a forge with no catalog card and leave for its authorize page; a non-http link is refused,
 *  not followed. It comes back to Profile (`routes/forge_connect.py`). */
async function beginConnect(forge: string, label: string): Promise<void> {
  const started = await startForgeConnect(forge)
  leaveForAuthorize(started.authorize_url, `The ${label} authorization link the server returned is not a usable URL.`)
}

/** The return leg of a Connect round trip, read off `?git_connect=` by the page (`hooks/useConnectReturn`). */
export type ConnectReturn = { connected: boolean; reason: string; forge: string }

/* The callbacks' closed slug vocabularies (`routes/git_connect.py`, `routes/forge_connect.py::_ERRORS`),
 * worded per forge. `Object.create(null)`: the key comes off the URL (`?reason=__proto__`). KEEP IT
 * COMPLETE — the fallback says "press Connect again", wrong for a slug only an operator or admin can clear,
 * and for `install_requested` (stored). */
const RETURN_ERRORS: Record<string, (label: string) => string> = Object.assign(Object.create(null), {
  expired: (l: string) => `The connect attempt timed out. Connect ${l} again to start a fresh one.`,
  state_mismatch: (l: string) => `That response did not match the attempt this browser started. Connect ${l} again.`,
  wrong_user: (l: string) => `That connect attempt belongs to a different signed-in user. Connect ${l} again.`,
  denied: (l: string) => `The authorization was declined at ${l}, so nothing was connected.`,
  no_code: (l: string) => `${l} did not return an authorization code. Connect ${l} again.`,
  exchange_failed: (l: string) => `${l} refused to complete the connection. Connect ${l} again.`,
  not_configured: (l: string) => `${l} Connect is not set up. Ask an operator.`,
  /* Nothing stored, and Connect again cannot help: the designated app is the other kind (GitHub App vs OAuth App). */
  wrong_app_kind: () => 'GitHub Connect is set up with the wrong kind of GitHub app. Ask an operator.',
  consent_blocked: () =>
    'Your Microsoft Entra tenant blocks this app until an administrator consents to it once. Ask a tenant '
    + 'administrator, or paste a token.',
  /* The one non-failure: the credential WAS stored; only an org owner can clear it. It claims nothing about
   * connection state (a crafted link could otherwise assert one) — the GitHub row below answers that. */
  install_requested: () =>
    'Waiting for an organization owner to approve the GitHub App. Until then its repositories are out of reach.',
})
const INSTALL_REQUESTED_REASON = 'install_requested'
const RETURN_FALLBACK = (l: string) => `The ${l} connection did not complete. Connect ${l} again.`

/** A failed return's sentence and severity: `install_requested` is a warning (the credential was stored). */
export function connectReturnError(reason: string, label: string): { severity: 'warning' | 'error'; text: string } {
  return {
    severity: reason === INSTALL_REQUESTED_REASON ? 'warning' : 'error',
    text: (RETURN_ERRORS[reason] ?? RETURN_FALLBACK)(label),
  }
}

export default function GitConnectionsCard({
  userId,
  ownGitCredentials,
  returned = null,
  addOnly = false,
  onCancel,
  onChanged,
}: {
  /** The signed-in person (the routes are self-service). */
  userId: string
  /** The page's own `?mine=true` git rows — "existing credential"'s fallback when the per-root roster is refused. */
  ownGitCredentials: readonly GitCredential[]
  /** The Connect round trip's outcome, if this page load is its return. */
  returned?: ConnectReturn | null
  /** The Git account New page (`pages/GitAccountNewPage`): only the Add form, its Cancel `onCancel` (none without
   *  one), and Connect in a popup (`components/projects/githubOAuthPopup.ts::connectForgeInPopup`) instead of leaving
   *  the page — a full-page authorize would reload it and lose the opener's draft (`hooks/useNestedCreate.ts`). */
  addOnly?: boolean
  onCancel?: () => void
  /** After any write lands (the page re-reads what depends on it: the account, its roster, GitHub reach); `next` is
   *  the account a save returned (none after a popup Connect or a Disconnect). */
  onChanged?: (next?: GitConnection) => void
}) {
  const queryClient = useQueryClient()
  const { queryKey } = gitConnectionsQuery(userId)
  const query = useQuery(gitConnectionsQuery(userId))
  const connections = query.data ?? null
  const loadError = query.isError ? apiDetail(query.error, 'Git accounts could not load. Reload the page.') : ''
  const setConnections = (update: (rows: GitConnection[] | undefined) => GitConnection[]) =>
    queryClient.setQueryData<GitConnection[]>(queryKey, update)
  const [adding, setAdding] = useState(false)
  /* The rows' own state, keyed by `rowKey`: which row's token form is open (one at a time), the row a write
   * refused (shown under it, in the server's words), and the rows with a write in flight. */
  const [tokenFor, setTokenFor] = useState<string | null>(null)
  const [rowError, setRowError] = useState<{ key: string; text: string } | null>(null)
  const [busyRows, withBusy] = useBusyIds<string>()
  const act = (key: string, fallback: string, write: () => Promise<void>) => withBusy(key, async () => {
    setRowError(null)
    try {
      await write()
    } catch (err) {
      setRowError({ key, text: apiDetail(err, fallback) })
    }
  })

  /** A write's answer replaces its own account's row, or joins the list after its root's others (a root's
   *  `not_connected` entry gives way to it); a new default takes the flag from its root's others. */
  const replaceRow = (next: GitConnection) => {
    setConnections(rows => {
      const list = (rows ?? [])
        .filter(row => !(row.root === next.root && !isListed(row)))
        .map(row => (row.root === next.root && next.is_default && rowKey(row) !== rowKey(next)
          ? { ...row, is_default: false } : row))
      const at = list.findIndex(row => rowKey(row) === rowKey(next))
      if (at >= 0) return list.map((row, i) => (i === at ? next : row))
      const last = list.map(row => row.root).lastIndexOf(next.root)
      return last >= 0 ? [...list.slice(0, last + 1), next, ...list.slice(last + 1)] : [...list, next]
    })
    onChanged?.(next)
  }

  /* Shown gone at once, then re-read: the next default, the pointer and the root's connect offer are the
   * server's to say. */
  const afterDisconnect = async (gone: GitConnection) => {
    setConnections(rows => {
      const rest = (rows ?? []).filter(row => rowKey(row) !== rowKey(gone))
      return gone.self_managed || rest.some(row => row.root === gone.root) ? rest
        : [...rest, { ...gone, state: 'not_connected' as const, detail: null, credential: null }]
    })
    onChanged?.()
    await query.refetch()
  }

  /* A popup Connect stored the account server-side: read the list again, and the form gives way to its row. */
  const afterPopupConnect = async () => {
    setAdding(false)
    await query.refetch()
    onChanged?.()
  }

  // `hasOwn`: the forge is a URL parameter, so `constructor` must not read a prototype member.
  const returnLabel = returned ? (Object.hasOwn(FORGE_LABELS, returned.forge) ? FORGE_LABELS[returned.forge as Forge] : 'GitHub') : ''
  /* Gated on the RECORD, not the URL (anyone can send `?git_connect=connected`): only a row the server
   * reports connected for that forge makes the claim. */
  const returnConfirmed = returned?.connected === true
    && (connections ?? []).some(row => row.forge === returned.forge && !row.self_managed && row.state === 'connected')

  const returnError = returned && !returned.connected ? connectReturnError(returned.reason, returnLabel) : null

  const listed = (connections ?? []).filter(isListed)
  /* The "+" names what it offers (the add form's types): "Connect GitHub, GitLab, Bitbucket or Azure DevOps". */
  const forgeLabels = [...new Set((connections ?? []).map(row => row.forge_label))]
  const addTitle = forgeLabels.length > 1
    ? `Connect ${forgeLabels.slice(0, -1).join(', ')} or ${forgeLabels[forgeLabels.length - 1]}`
    : forgeLabels.length === 1 ? `Connect ${forgeLabels[0]}` : 'Add Git account'

  /** One connected account's row: the forge, then who it is and its root in full under the name (the root is
   *  what tells two self-managed hosts apart, so it never truncates), Default where its root holds
   *  several (`shared`), and in its ⋯ Reconnect, Disconnect, and Make default on a shared root — the default
   *  serves every repository no other account's owners reach. Reconnect re-runs Connect for an account that
   *  came from Connect; a token row (`accountName` null), a self-managed GitLab, or a forge with no app
   *  configured opens a token form under the row instead — a tenant that blocks the app connected with a
   *  token for a reason, and sending it back to sign-in would fail. */
  const connectionRow = (connection: GitConnection): FeedRowSpec => {
    const key = rowKey(connection)
    const shared = listed.some(other => other.root === connection.root && rowKey(other) !== key)
    const { root, forge, forge_label: label, state } = connection
    const account = accountName(connection)
    /* A root's only account is addressed by the root; one of several by its credential too. */
    const testId = shared && connection.credential
      ? `git-connection-${root}-${connection.credential.id}` : `git-connection-${root}`
    /* Which account a verb acts on, said only where the root holds several. */
    const name = shared && account ? `${label} ${account}` : label
    const canConnect = !connection.self_managed && connectOffered(connection) && account !== null
    const connectPath = canConnect ? catalogConnectPath(connection) : null
    const tokenOpen = tokenFor === key
    const error = rowError?.key === key ? rowError.text : ''
    const needsReconnect = state === 'needs_reconnect'

    const reconnect = () => {
      if (canConnect) void act(key, `Could not start the ${label} connection`, () => beginConnect(forge, label))
      else { setRowError(null); setTokenFor(open => (open === key ? null : key)) }
    }
    const disconnect = () => act(key, `Could not disconnect ${name}`, async () => {
      await deleteGitConnection(userId, root, connection.credential?.id)
      await afterDisconnect(connection)
    })
    const makeDefault = () => act(key, `Could not make ${name} the default`, async () => {
      if (connection.credential) replaceRow(await putGitConnection(userId, root, connection.credential.id, true))
    })

    return {
      key,
      data: { connection: testId, state },
      indicator: needsReconnect
        // One line; the forge's own why (e.g. the token was revoked) is its hover.
        ? { tone: 'warning', glyph: '!', label: 'Needs reconnect', title: connection.detail ?? undefined, tapTitle: true }
        : { tone: 'success', glyph: '✓', label: 'Connected' },
      title: (
        <>
          <Box component="span" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5 }}>{label}{credentialMark(forge, label)}</Box>
          <Box component="span" sx={[WRAP_SX, { display: 'block', fontWeight: FONT_WEIGHT.regular }]} data-testid={`${testId}-state`}
            title={needsReconnect ? connection.detail ?? undefined : account ?? undefined}>
            {needsReconnect ? <StatusChip label="Needs reconnect" tone="warning" />
              : account === null ? 'Connected with a token'
                : <Box component="strong" sx={{ fontWeight: FONT_WEIGHT.semibold }}>{account}</Box>}
          </Box>
          <Box component="span" sx={[WRAP_SX, monoSx, { display: 'block', color: 'text.secondary', fontSize: FONT_SIZE.sm }]}>{root}</Box>
        </>
      ),
      titleText: label,
      href: null,
      titleLines: 'all',
      markers: shared && connection.is_default ? [{
        key: 'default', glyph: '★', label: 'Default', title: 'Used for repositories no other account reaches',
        testId: `${testId}-default`, hug: true, word: { text: 'Default' },
      }] : undefined,
      busy: busyRows.has(key),
      actions: [
        ...(shared && !connection.is_default && connection.credential
          ? [{ key: 'default', label: 'Make default', ariaLabel: `Make ${name} the default`, onClick: () => void makeDefault() } as OverflowItem] : []),
        connectPath
          ? { key: 'reconnect', label: 'Reconnect', ariaLabel: `Reconnect ${name}`, to: connectPath }
          : { key: 'reconnect', label: 'Reconnect', ariaLabel: `Reconnect ${name}`, onClick: reconnect },
        { key: 'disconnect', label: 'Disconnect', ariaLabel: `Disconnect ${name}`, danger: true, onClick: () => void disconnect() },
      ],
      detail: tokenOpen || error ? (
        <Box sx={[FORM_SX, { pl: 3, pb: 1 }]}>
          {error && <Banner severity="error" testId={`${testId}-error`}>{error}</Banner>}
          {tokenOpen && (
            <PatForm connection={connection} userId={userId} onError={text => setRowError({ key, text })}
              onSaved={next => { setTokenFor(null); setRowError(null); replaceRow(next) }} />
          )}
        </Box>
      ) : undefined,
    }
  }

  return (
    <Stack spacing={1} data-testid="git-connections">
      {returnConfirmed && (
        <Banner severity="success">{returnLabel} connected — your runners will push with this account.</Banner>
      )}
      {returnError && <Banner severity={returnError.severity}>{returnError.text}</Banner>}
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {connections === null && !loadError ? (
        <LoadingState rows={2} />
      ) : connections !== null && !addOnly && (
        <>
          <SectionHeader
            action={
              <Tip title={addTitle}><Button
                type="button"
                variant="outlined"
                color="inherit"
                aria-label="Add Git account"
                aria-expanded={adding}
                data-testid="git-account-add"
                onClick={() => setAdding(open => !open)}
                sx={[...ROW_ACTION_SX, { fontSize: FONT_SIZE.lg, fontWeight: FONT_WEIGHT.semibold, minWidth: SIZE.tapTarget }]}
              >
                +
              </Button></Tip>
            }
          />
          <FeedList
            ariaLabel="Git accounts"
            testId="git-accounts-table"
            emptyMessage="None"
            sections={listed.length === 0 ? [] : [{ key: 'accounts', rail: false, rows: listed.map(connectionRow) }]}
          />
        </>
      )}
      {connections !== null && (adding || addOnly) && (
        <AddForm
          userId={userId}
          connections={connections}
          ownGitCredentials={ownGitCredentials}
          inPopup={addOnly}
          onSaved={next => { setAdding(false); replaceRow(next) }}
          onConnected={afterPopupConnect}
          onCancel={addOnly ? onCancel : () => setAdding(false)}
        />
      )}
    </Stack>
  )
}

/** The add form's type select value for a self-managed GitLab (every other value is a fixed root). */
const SELF_MANAGED = 'self-managed'

type Method = 'connect' | 'pat' | 'pick'

/** The one add form: Type first, then how (Connect / token / existing credential), then that method's
 *  fields. Every forge is offered, one already holding an account too: what is added is another account
 *  beside it (a root holds any number; the server keeps the first the default). */
function AddForm({
  userId,
  connections,
  ownGitCredentials,
  inPopup,
  onSaved,
  onConnected,
  onCancel,
}: {
  userId: string
  connections: readonly GitConnection[]
  ownGitCredentials: readonly GitCredential[]
  /** Connect in a popup and stay (`addOnly`), not via the catalog screen or a full-page authorize. Also drops
   *  the form's own heading: `addOnly` is the New Git account page, whose title already names it. */
  inPopup: boolean
  /** After a popup Connect lands: the account is the server's to list. */
  onConnected: () => Promise<void>
  onSaved: (next: GitConnection) => void
  /** None: the form cannot be put away. */
  onCancel?: () => void
}) {
  /* One entry per fixed root (its first: the connect offer is the root's, whichever account carries it). */
  const fixed = connections.filter((row, i) => !row.self_managed
    && connections.findIndex(other => other.root === row.root) === i)
  /* GitHub first, connected or not (a second GitHub account is the common add); then the list's first root. */
  const [type, setType] = useState(() => (fixed.find(row => row.forge === 'github') ?? fixed[0])?.root ?? SELF_MANAGED)
  const target = fixed.find(row => row.root === type) ?? null
  const methodFor = (row: GitConnection | null): Method => (row && connectOffered(row) ? 'connect' : 'pat')
  const [method, setMethod] = useState<Method>(() => methodFor(target))
  const { busy, formError: error, setFormError: setError, run } = useSubmit()

  const pickType = (next: string) => {
    setType(next)
    setError('')
    setMethod(methodFor(fixed.find(row => row.root === next) ?? null))
  }

  const connectPath = target && !inPopup ? catalogConnectPath(target) : null

  const handleConnect = async () => {
    if (!target) return
    const { forge, forge_label: label } = target
    if (!inPopup) {
      await run(`Could not start the ${label} connection`, () => beginConnect(forge, label))
      return
    }
    // Opened in the click, before any await: a popup opened later is blocked.
    const popup = openConnectPopup()
    await run(`Could not connect ${label}`, async () => {
      await connectForgeInPopup(popup, forge, label, `Press Connect ${label} to try again.`)
      await onConnected()
    })
  }

  return (
    <Box role="group" aria-label="Add Git account" data-testid="git-account-add-form" sx={[FORM_SX, { pt: 1 }]}>
      {/* On its own page the title already says it. */}
      {!inPopup && <Typography variant="subtitle2" component="h3">Add Git account</Typography>}
      <FormTextField
        label="Type"
        help={{ tip: 'The Git host this account connects to.', docs: 'console-reference/help-and-profile/profile' }}
        id="git-add-type"
        value={type}
        onChange={pickType}
        disabled={busy}
        options={[
          ...fixed.map(row => ({ value: row.root, label: row.forge_label })),
          { value: SELF_MANAGED, label: 'Self-managed GitLab' },
        ]}
      />

      {target && (
        <FormTextField
          label="Method"
          help={{ tip: 'How Jaah gets access to this account.', docs: 'console-reference/help-and-profile/profile' }}
          hint={isEnabled('forge_connect') && !target.connect.available ? <ConnectUnavailableHint target={target} /> : undefined}
          id="git-add-method"
          value={method}
          onChange={v => { setError(''); setMethod(v as Method) }}
          disabled={busy}
          options={[
            ...(connectOffered(target)
              ? [{ value: 'connect', label: `Sign in with ${target.forge_label}`, help: 'Signs you in on the host; no token to paste.' }]
              : []),
            { value: 'pat', label: 'Paste a token', help: 'Connects with a token you create on the host.' },
            { value: 'pick', label: 'Use a credential I already added', help: 'Reuses a Git credential already in Secrets.' },
          ]}
        />
      )}

      {error && <Banner severity="error" testId="git-account-add-error">{error}</Banner>}

      {target === null ? (
        <SelfManagedForm userId={userId} onSaved={onSaved} />
      ) : method === 'connect' ? (
        <Box>
          {connectPath ? (
            <Button component={RouterLink} to={connectPath} variant="contained" sx={ROW_ACTION_SX} disabled={busy}>
              Connect {target.forge_label}
            </Button>
          ) : (
            <Button type="button" variant="contained" sx={ROW_ACTION_SX} disabled={busy}
              onClick={() => void handleConnect()}>
              Connect {target.forge_label}
            </Button>
          )}
        </Box>
      ) : method === 'pat' ? (
        <PatForm key={target.root} connection={target} userId={userId} onSaved={onSaved} onError={setError} />
      ) : (
        <PickForm key={target.root} connection={target} userId={userId} ownGitCredentials={ownGitCredentials}
          onSaved={onSaved} onError={setError} />
      )}

      {onCancel && (
        <Box>
          <Button type="button" variant="text" color="inherit" sx={ROW_ACTION_SX} disabled={busy} onClick={onCancel}>
            Cancel
          </Button>
        </Box>
      )}
    </Box>
  )
}

/** The token field's explanation per forge's `_pat_config`: its effect, then the one line a person needs to make it. */
function patHint(forge: string): Explain {
  const tip = 'Tasks clone and push with this token.'
  if (forge === 'bitbucket') return { tip, more: 'An Atlassian API token with Bitbucket scopes. App passwords are not accepted.' }
  if (forge === 'azure_devops') {
    return { tip, more: 'Create it for “All accessible organizations”: a PAT scoped to one organization reaches only that one.' }
  }
  if (forge === 'gitlab') return { tip, more: 'A personal access token with read_repository and write_repository.' }
  return { tip, more: 'A personal access token with repository read and write.' }
}

function PatForm({
  connection,
  userId,
  onSaved,
  onError,
}: {
  connection: GitConnection
  userId: string
  onSaved: (next: GitConnection) => void
  onError: (message: string) => void
}) {
  const { root, forge, forge_label: label } = connection
  const [token, setToken] = useState('')
  const [username, setUsername] = useState('')
  const [testOrg, setTestOrg] = useState('')
  const { busy: saving, formError, run } = useSubmit()
  // The tester's reason (422) is the row's banner, not this form's.
  useEffect(() => { if (formError) onError(formError) }, [formError, onError])
  const idBase = `git-pat-${root}`
  // Opened from the row's ⋯, which cannot announce it: focus lands on the form's first field instead.
  const formRef = useRef<HTMLDivElement>(null)
  useEffect(() => { formRef.current?.querySelector('input')?.focus() }, [])

  /* Every field the forge's `_pat_config` requires, so its "`<field>` is required" never reaches a person. */
  const complete = Boolean(token.trim())
    && (forge !== 'bitbucket' || Boolean(username.trim()))
    && (forge !== 'azure_devops' || Boolean(testOrg.trim()))

  function submit() {
    if (!complete) return
    const body: GitPatBody = { token }
    if (forge === 'bitbucket') body.username = username
    if (forge === 'azure_devops') body.test_org = testOrg
    onError('')
    return run(`${label} did not accept that token`, async () => onSaved(await postGitConnectionPat(userId, root, body)))
  }

  return (
    <Box ref={formRef} role="group" onKeyDown={enterRuns(() => void submit())} aria-label={`${label} token`} sx={FORM_SX}>
      {forge === 'bitbucket' && (
        <FormTextField label="Atlassian email" help="Bitbucket checks the API token against this account."
          id={`${idBase}-username`} type="email" autoComplete="off"
          value={username} onChange={setUsername} disabled={saving} />
      )}
      {forge === 'azure_devops' && (
        <FormTextField label="Organization" help="The token is tested against this Azure DevOps organization."
          id={`${idBase}-org`} autoComplete="off" placeholder="acme"
          value={testOrg} onChange={setTestOrg} disabled={saving} />
      )}
      <FormTextField label={forge === 'bitbucket' ? 'API token' : 'Token'} help={patHint(forge)}
        id={`${idBase}-token`} type="password" autoComplete="off"
        value={token} onChange={setToken} disabled={saving} />
      <Box>
        <Button type="button" variant="contained" sx={ROW_ACTION_SX} disabled={saving || !complete}
          onClick={() => void submit()}>
          {saving ? 'Testing…' : 'Test and connect'}
        </Button>
      </Box>
    </Box>
  )
}

function PickForm({
  connection,
  userId,
  ownGitCredentials,
  onSaved,
  onError,
}: {
  connection: GitConnection
  userId: string
  ownGitCredentials: readonly GitCredential[]
  onSaved: (next: GitConnection) => void
  onError: (message: string) => void
}) {
  const { root, forge, forge_label: label } = connection
  const [choice, setChoice] = useState('')
  const { busy: saving, formError, run } = useSubmit()
  useEffect(() => { if (formError) onError(formError) }, [formError, onError])
  /* The fetcher is held in a ref, so the page re-filtering its roster every render does not re-fetch. */
  const { data, error: failed } = useApiRead(['git-connection-credentials', userId, root, forge], async () => {
    try {
      return { options: (await listGitCredentialsForRoot(userId, root)).credentials, ownOnly: false }
    } catch (err) {
      /* Behind `users:view`: a 403 for most members, who get their own rows of this forge instead (the PUT
       * re-checks the root and names a mismatch). Any other failure is "could not tell", never "none".
       * `ownOnly`: the list is the page's own rows only, so it may miss a granted one. */
      if (err instanceof ApiError && err.status === 403) {
        return { options: ownGitCredentials.filter(row => row.type === forge), ownOnly: true }
      }
      throw err
    }
  }, 'The credential list could not load.')

  function submit() {
    if (!choice) return
    onError('')
    return run(`Could not connect ${label}`, async () => onSaved(await putGitConnection(userId, root, choice)))
  }

  if (failed) return <Banner severity="error">{failed}</Banner>
  if (data === null) return <LoadingState rows={1} />
  const { options, ownOnly } = data
  if (options.length === 0) {
    return (
      <Typography variant="body2" sx={[WRAP_SX, { color: 'text.secondary' }]}>
        {ownOnly ? `None of your credentials is for ${root}.` : `No stored credential for ${root}.`}
      </Typography>
    )
  }
  return (
    <Box role="group" aria-label={`${label} existing credential`} sx={FORM_SX}>
      <FormTextField
        label="Credential"
        help={{ tip: 'Connects this host with a credential already stored.', more: ownOnly ? 'Only credentials you added are listed.' : undefined }}
        id={`git-pick-${root}`}
        value={choice}
        onChange={setChoice}
        disabled={saving}
        options={options.map(row => ({ value: row.id, label: row.name }))}
      >
        {/* A native select with no matching value shows its first option as picked without a change event. */}
        {choice === '' && <option value="" disabled>Choose…</option>}
      </FormTextField>
      <Box>
        <Button type="button" variant="contained" sx={ROW_ACTION_SX} disabled={saving || !choice}
          onClick={() => void submit()}>
          Use
        </Button>
      </Box>
    </Box>
  )
}

/** A self-managed GitLab: base URL + token only (no Connect, D4). The row appears once it is tested. */
function SelfManagedForm({ userId, onSaved }: { userId: string; onSaved: (next: GitConnection) => void }) {
  const [baseUrl, setBaseUrl] = useState('')
  const [token, setToken] = useState('')
  const { busy: saving, formError: error, run } = useSubmit()

  async function submit() {
    if (!token.trim() || !selfManagedRoot(baseUrl)) return
    await run('That GitLab did not accept the token', async () => {
      onSaved(await postGitConnectionPat(userId, selfManagedRoot(baseUrl), { token }))
    })
  }

  return (
    <Box role="group" onKeyDown={enterRuns(() => void submit())} aria-label="Self-managed GitLab"
      data-testid="git-add-self-managed" sx={FORM_SX}>
      {error && <Banner severity="error">{error}</Banner>}
      <FormTextField label="Base URL" help="Repositories under this address use this account." id="git-self-managed-url" type="url" autoComplete="off"
        placeholder="https://git.example.org" value={baseUrl} onChange={setBaseUrl} disabled={saving} />
      <FormTextField label="Token" help={patHint('gitlab')} id="git-self-managed-token" type="password"
        autoComplete="off" value={token} onChange={setToken} disabled={saving} />
      <Box>
        <Button type="button" variant="contained" sx={ROW_ACTION_SX}
          disabled={saving || !token.trim() || !selfManagedRoot(baseUrl)} onClick={() => void submit()}>
          {saving ? 'Testing…' : 'Test and connect'}
        </Button>
      </Box>
    </Box>
  )
}
