import { type KeyboardEvent, useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MenuItem from '@mui/material/MenuItem'
import Stack from '@mui/material/Stack'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { apiDetail, ApiError } from '../api'
import { useApiRead } from '../hooks/useApiRead'
import { useSubmit } from '../hooks/useSubmit'
import { navigateExternal } from '../lib/externalNavigate'
import { safeHref } from '../lib/safeHref'
import {
  deleteGitConnection,
  listGitConnections,
  listGitCredentialsForRoot,
  postGitConnectionPat,
  putGitConnection,
  startForgeConnect,
  type GitConnection,
  type GitCredential,
  type GitPatBody,
} from '../pages/credentials/api'
import Banner from '../ui/Banner'
import { credentialMark } from '../ui/credentialMarks'
import DataTable from '../ui/DataTable'
import FormField from '../ui/FormField'
import FormTextField from '../ui/FormTextField'
import { fieldTapTargetSx, monoSx, ROW_ACTION_SX } from '../ui/listStyles'
import LoadingState from '../ui/LoadingState'
import { FORGE_LABELS, type Forge } from '../types'
import { isEnabled } from '../lib/editionFeatures'

/* Whether a forge's sign-in (its OAuth app) is offered. Never without forge Connect (D22): no Jaah OAuth app to
   sign in through, so every forge takes a pasted token. */
const connectOffered = (row: GitConnection): boolean => isEnabled('forge_connect') && row.connect.available

/* ── PROFILE > GIT ACCOUNTS: A TABLE OF WHAT IS CONNECTED, PLUS ONE ADD FORM ──────────────
 *
 * The server's `GET /api/users/{id}/git-connections` is the whole model: the four fixed roots plus each
 * self-managed GitLab the person connected, each with its state and whether Connect works. The TABLE lists
 * only the roots that hold an account (connected or needing a reconnect) — a forge nobody connected is not
 * a row (operator decision: no itemised "Not connected" list). The `+` in the table head opens ONE form:
 * pick the type, then how (Connect, paste a token, an existing credential). Rows offer Reconnect and
 * Disconnect only.
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

/** The mark column plus its gap: the cell's text starts under the forge name, not under the mark. */
const MARK_GAP_PX = 8

/** Wrap anywhere rather than overflow: a 40-character account or a deep sub-path host has no spaces. */
const WRAP_SX = { overflowWrap: 'anywhere', minWidth: 0 } as const

/** The add form's and the Reconnect panel's frame: a flat column under the table. */
const FORM_SX = { display: 'flex', flexDirection: 'column', gap: 1.5, pt: 0.5 } as const

/** `host[/sub-path]` from what a person types (`https://git.example.org/gitlab/` → `git.example.org/gitlab`),
 *  the root shape the server keys on; the server validates it. */
export function selfManagedRoot(baseUrl: string): string {
  return baseUrl.trim().replace(/^https?:\/\//i, '').replace(/\/+$/, '').toLowerCase()
}

/** A root holding an account — the table's rows. `not_connected` is the add form's, never a row. */
const isListed = (row: GitConnection) => row.state !== 'not_connected'

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

/** Start a forge's OAuth Connect and leave for its authorize page; a non-http link is refused, not followed.
 *  Every forge comes back to Profile (`routes/forge_connect.py`); the create wizard's popup names its own
 *  return (`components/projects/githubOAuthPopup.ts`). */
async function beginConnect(forge: string, label: string): Promise<void> {
  const started = await startForgeConnect(forge)
  const href = safeHref(started.authorize_url)
  if (!href) throw new Error(`The ${label} authorization link the server returned is not a usable URL.`)
  navigateExternal(href)
}

/** The return leg of a Connect round trip, read off `?git_connect=` by the page. */
export type ConnectReturn = { result: 'connected' | 'error'; reason: string; forge: string }

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
  addRequest = 0,
  onChanged,
}: {
  /** The signed-in person (the routes are self-service). */
  userId: string
  /** The page's own `?mine=true` git rows — "existing credential"'s fallback when the per-root roster is refused. */
  ownGitCredentials: readonly GitCredential[]
  /** The Connect round trip's outcome, if this page load is its return. */
  returned?: ConnectReturn | null
  /** Bumped to open the Add Git account form (`lib/gitAddLink.ts`'s deep link); 0 asks nothing. */
  addRequest?: number
  /** After any write lands (the page re-reads what depends on it: the account, its roster, GitHub reach). */
  onChanged?: () => void
}) {
  const queryClient = useQueryClient()
  const queryKey = ['git-connections', userId]
  const query = useQuery({ queryKey, queryFn: async () => (await listGitConnections(userId)).connections })
  const connections = query.data ?? null
  const loadError = query.isError ? apiDetail(query.error, 'Git accounts could not load. Reload the page.') : ''
  const setConnections = (update: (rows: GitConnection[] | undefined) => GitConnection[]) =>
    queryClient.setQueryData<GitConnection[]>(queryKey, update)
  const [adding, setAdding] = useState(addRequest > 0)
  useEffect(() => {
    if (addRequest > 0) setAdding(true)
  }, [addRequest])

  /** A write's answer replaces its own row (or joins the list: a new self-managed root). */
  const replaceRow = (next: GitConnection) => {
    setConnections(rows => {
      const list = rows ?? []
      return list.some(row => row.root === next.root)
        ? list.map(row => (row.root === next.root ? next : row))
        : [...list, next]
    })
    onChanged?.()
  }

  /* Shown gone at once, then re-read: the pointer and the root's connect offer are the server's to say. */
  const afterDisconnect = async (root: string) => {
    setConnections(rows => (rows ?? []).flatMap(row => (
      row.root !== root ? [row]
        : row.self_managed ? []
          : [{ ...row, state: 'not_connected' as const, detail: null, credential: null }]
    )))
    onChanged?.()
    await query.refetch()
  }

  // `hasOwn`: the forge is a URL parameter, so `constructor` must not read a prototype member.
  const returnLabel = returned ? (Object.hasOwn(FORGE_LABELS, returned.forge) ? FORGE_LABELS[returned.forge as Forge] : 'GitHub') : ''
  /* Gated on the RECORD, not the URL (anyone can send `?git_connect=connected`): only a row the server
   * reports connected for that forge makes the claim. */
  const returnConfirmed = returned?.result === 'connected'
    && (connections ?? []).some(row => row.forge === returned.forge && !row.self_managed && row.state === 'connected')

  const returnError = returned?.result === 'error' ? connectReturnError(returned.reason, returnLabel) : null

  const listed = (connections ?? []).filter(isListed)
  /* The "+" names what it offers (the add form's types): "Connect GitHub, GitLab, Bitbucket or Azure DevOps". */
  const forgeLabels = [...new Set((connections ?? []).map(row => row.forge_label))]
  const addTitle = forgeLabels.length > 1
    ? `Connect ${forgeLabels.slice(0, -1).join(', ')} or ${forgeLabels[forgeLabels.length - 1]}`
    : forgeLabels.length === 1 ? `Connect ${forgeLabels[0]}` : 'Add Git account'

  return (
    <Stack spacing={1} data-testid="git-connections">
      {returnConfirmed && (
        <Banner severity="success">{returnLabel} connected — your runners will push with this account.</Banner>
      )}
      {returnError && <Banner severity={returnError.severity}>{returnError.text}</Banner>}
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {connections === null && !loadError ? (
        <LoadingState rows={2} />
      ) : connections !== null && (
        <DataTable ariaLabel="Git accounts" testId="git-accounts-table">
          <TableHead>
            <TableRow>
              <TableCell>Account</TableCell>
              <TableCell align="right" sx={{ width: '1%' }}>
                <Button
                  type="button"
                  variant="outlined"
                  color="inherit"
                  aria-label="Add Git account"
                  title={addTitle}
                  aria-expanded={adding}
                  data-testid="git-account-add"
                  onClick={() => setAdding(open => !open)}
                  sx={[...ROW_ACTION_SX, { fontSize: '1rem', fontWeight: 600, minWidth: 44 }]}
                >
                  +
                </Button>
              </TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {listed.length === 0 ? (
              <TableRow>
                <TableCell colSpan={2} sx={{ color: 'text.secondary' }}>
                  None
                </TableCell>
              </TableRow>
            ) : listed.map(row => (
              <ConnectionRow
                key={row.root}
                userId={userId}
                connection={row}
                onSaved={replaceRow}
                onDisconnected={afterDisconnect}
              />
            ))}
          </TableBody>
        </DataTable>
      )}
      {connections !== null && adding && (
        <AddForm
          userId={userId}
          connections={connections}
          ownGitCredentials={ownGitCredentials}
          onSaved={next => { setAdding(false); replaceRow(next) }}
          onCancel={() => setAdding(false)}
        />
      )}
    </Stack>
  )
}

/** One connected root: who it is connected as, and Reconnect / Disconnect. Reconnect re-runs Connect for
 *  an account that came from Connect; a token row (`accountName` null), a self-managed GitLab, or a forge
 *  with no app configured gets a token field instead — a tenant that blocks the app connected with a token
 *  for a reason, and sending it back to sign-in would fail. */
function ConnectionRow({
  userId,
  connection,
  onSaved,
  onDisconnected,
}: {
  userId: string
  connection: GitConnection
  onSaved: (next: GitConnection) => void
  onDisconnected: (root: string) => Promise<void>
}) {
  const [tokenOpen, setTokenOpen] = useState(false)
  const { busy, formError: error, setFormError: setError, run } = useSubmit()
  const { root, forge, forge_label: label, state } = connection
  const account = accountName(connection)
  const testId = `git-connection-${root}`
  const canConnect = !connection.self_managed && connectOffered(connection) && account !== null

  const handleReconnect = () => {
    if (canConnect) void run(`Could not start the ${label} connection`, () => beginConnect(forge, label))
    else { setError(''); setTokenOpen(open => !open) }
  }

  const handleDisconnect = () => run(`Could not disconnect ${label}`, async () => {
    await deleteGitConnection(userId, root)
    await onDisconnected(root)
  })

  const expanded = tokenOpen || Boolean(error)

  return (
    <>
      <TableRow data-testid={testId} data-state={state}
        sx={expanded ? { '& > td': { borderBottom: 'none' } } : undefined}>
        <TableCell sx={WRAP_SX}>
          <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: `${MARK_GAP_PX}px` }}>
            <Box sx={{ pt: '2px', flexShrink: 0, display: 'flex' }}>{credentialMark(forge, label)}</Box>
            <Box sx={WRAP_SX}>
              <Typography variant="subtitle2" component="h3" sx={WRAP_SX}>{label}</Typography>
              <Box data-testid={`${testId}-state`} sx={WRAP_SX}>
                {/* One line; the forge's own why (e.g. the token was revoked) is its tooltip. */}
                {state === 'needs_reconnect' ? (
                  <Typography variant="body2" title={connection.detail ?? undefined}
                    sx={[WRAP_SX, { color: 'warning.main', fontWeight: 600 }]}>
                    Needs reconnect
                  </Typography>
                ) : (
                  <Typography variant="body2" sx={WRAP_SX}>
                    {account === null ? 'Connected with a token' : (
                      <Box component="strong" sx={{ fontWeight: 600 }}>{account}</Box>
                    )}
                  </Typography>
                )}
              </Box>
              <Typography variant="caption" sx={[WRAP_SX, { color: 'text.secondary', display: 'block' }, monoSx]}>
                {root}
              </Typography>
            </Box>
          </Box>
        </TableCell>
        <TableCell align="right" sx={{ verticalAlign: 'top' }}>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'flex-end', gap: 1 }}>
            <Button type="button" variant="outlined" color="inherit" sx={ROW_ACTION_SX} disabled={busy}
              aria-expanded={canConnect ? undefined : tokenOpen} aria-label={`Reconnect ${label}`}
              onClick={handleReconnect}>
              Reconnect
            </Button>
            <Button type="button" variant="outlined" color="error" sx={ROW_ACTION_SX} disabled={busy}
              aria-label={`Disconnect ${label}`} onClick={() => void handleDisconnect()}>
              Disconnect
            </Button>
          </Box>
        </TableCell>
      </TableRow>
      {expanded && (
        <TableRow>
          <TableCell colSpan={2} sx={{ pt: 0 }}>
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
              {error && <Banner severity="error" testId={`${testId}-error`}>{error}</Banner>}
              {tokenOpen && (
                <PatForm connection={connection} userId={userId} onError={setError}
                  onSaved={next => { setTokenOpen(false); setError(''); onSaved(next) }} />
              )}
            </Box>
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

/** The add form's type select value for a self-managed GitLab (every other value is a fixed root). */
const SELF_MANAGED = 'self-managed'

type Method = 'connect' | 'pat' | 'pick'

/** The one add form: Type first, then how (Connect / token / existing credential), then that method's
 *  fields. A fixed forge that already holds an account is offered disabled — its row's Reconnect is the
 *  way to change it (one account per root, the server's model). */
function AddForm({
  userId,
  connections,
  ownGitCredentials,
  onSaved,
  onCancel,
}: {
  userId: string
  connections: readonly GitConnection[]
  ownGitCredentials: readonly GitCredential[]
  onSaved: (next: GitConnection) => void
  onCancel: () => void
}) {
  const fixed = connections.filter(row => !row.self_managed)
  const [type, setType] = useState(() => fixed.find(row => !isListed(row))?.root ?? SELF_MANAGED)
  /* A type that got an account while the form was open (a row's Reconnect saved) falls back to self-managed. */
  const target = fixed.find(row => row.root === type && !isListed(row)) ?? null
  const methodFor = (row: GitConnection | null): Method => (row && connectOffered(row) ? 'connect' : 'pat')
  const [method, setMethod] = useState<Method>(() => methodFor(target))
  const { busy, formError: error, setFormError: setError, run } = useSubmit()

  const pickType = (next: string) => {
    setType(next)
    setError('')
    setMethod(methodFor(fixed.find(row => row.root === next) ?? null))
  }

  const handleConnect = async () => {
    if (!target) return
    await run(`Could not start the ${target.forge_label} connection`, () => beginConnect(target.forge, target.forge_label))
  }

  return (
    <Box role="group" aria-label="Add Git account" data-testid="git-account-add-form" sx={[FORM_SX, { pt: 1 }]}>
      <Typography variant="subtitle2" component="h3">Add Git account</Typography>
      <FormField label="Type">
        <TextField id="git-add-type" select sx={fieldTapTargetSx} value={type}
          onChange={e => pickType(e.target.value)} disabled={busy}>
          {fixed.map(row => (
            <MenuItem key={row.root} value={row.root} disabled={isListed(row)}>
              {isListed(row) ? `${row.forge_label} (already added)` : row.forge_label}
            </MenuItem>
          ))}
          <MenuItem value={SELF_MANAGED}>Self-managed GitLab</MenuItem>
        </TextField>
      </FormField>

      {target && (
        <FormField
          label="Method"
          hint={isEnabled('forge_connect') && !target.connect.available && target.connect.reason ? target.connect.reason : undefined}
        >
          <TextField id="git-add-method" select sx={fieldTapTargetSx} value={method}
            onChange={e => { setError(''); setMethod(e.target.value as Method) }} disabled={busy}>
            {connectOffered(target) && (
              <MenuItem value="connect">Sign in with {target.forge_label}</MenuItem>
            )}
            <MenuItem value="pat">Paste a token</MenuItem>
            <MenuItem value="pick">Use a credential I already added</MenuItem>
          </TextField>
        </FormField>
      )}

      {error && <Banner severity="error" testId="git-account-add-error">{error}</Banner>}

      {target === null ? (
        <SelfManagedForm userId={userId} onSaved={onSaved} />
      ) : method === 'connect' ? (
        <Box>
          <Button type="button" variant="contained" sx={ROW_ACTION_SX} disabled={busy}
            onClick={() => void handleConnect()}>
            Connect {target.forge_label}
          </Button>
        </Box>
      ) : method === 'pat' ? (
        <PatForm key={target.root} connection={target} userId={userId} onSaved={onSaved} onError={setError} />
      ) : (
        <PickForm key={target.root} connection={target} userId={userId} ownGitCredentials={ownGitCredentials}
          onSaved={onSaved} onError={setError} />
      )}

      <Box>
        <Button type="button" variant="text" color="inherit" sx={ROW_ACTION_SX} disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </Box>
    </Box>
  )
}

/** The token fields each forge's `_pat_config` takes, with the one line a person needs to make it. */
function patHint(forge: string): string {
  if (forge === 'bitbucket') return 'An Atlassian API token with Bitbucket scopes. App passwords are not accepted.'
  if (forge === 'azure_devops') {
    return 'Create it for “All accessible organizations”: a PAT scoped to one organization reaches only that one.'
  }
  if (forge === 'gitlab') return 'A personal access token with read_repository and write_repository.'
  return 'A personal access token with repository read and write.'
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
    <Box role="group" onKeyDown={enterRuns(() => void submit())} aria-label={`${label} token`} sx={FORM_SX}>
      {forge === 'bitbucket' && (
        <FormTextField label="Atlassian email" id={`${idBase}-username`} type="email" autoComplete="off"
          value={username} onChange={setUsername} disabled={saving} />
      )}
      {forge === 'azure_devops' && (
        <FormTextField label="Organization"
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
      <FormField label="Credential" help={ownOnly ? 'Only credentials you added are listed.' : undefined}>
        <TextField id={`git-pick-${root}`} select sx={fieldTapTargetSx} value={choice}
          onChange={e => setChoice(e.target.value)} disabled={saving}>
          {options.map(row => (
            <MenuItem key={row.id} value={row.id} sx={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
              {row.name}
            </MenuItem>
          ))}
        </TextField>
      </FormField>
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
      <FormTextField label="Base URL" id="git-self-managed-url" type="url" autoComplete="off"
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
