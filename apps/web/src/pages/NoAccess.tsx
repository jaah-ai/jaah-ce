import { useEffect, useState, type ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Divider from '@mui/material/Divider'
import MenuItem from '@mui/material/MenuItem'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { useAuth, useCan } from '../auth'
import { useSubmit } from '../hooks/useSubmit'
import { listRoles, type AccessRole } from '../components/access/api'
import {
  NOTE_MAX,
  createRequest,
  listRequests,
  REQUESTS_STALE_MS,
  probeHint,
  resourceForPath,
  scopeTypeLabel,
  type AccessRequest,
  type RequestHint,
} from '../lib/accessRequests'
import { isEnabled } from '../lib/editionFeatures'
import { ADD_NAME, isNameless } from '../lib/memberLabel'
import { NAME_PROMPT_DISMISSED_STEP, readOnboardingStepsDone } from '../lib/onboarding'
import { PERMISSION_KEYS } from '../types'
import Banner from '../ui/Banner'
import InterstitialPanel from '../ui/InterstitialPanel'
import LoadingState from '../ui/LoadingState'

/* The capability layer's one terminal destination: where every route guard sends a user lacking the
 * route's feature — the whole console for a zero-grant user.
 * A destination, not a dead end: it says what happened, what to do, and which account (an admin needs the
 * address to grant against), with its own Sign out (at <=480px the rail's lives in the "More" sheet).
 * Two messages: of eleven guards only one means "holds nothing"; the others mean "not THIS page". The copy
 * branches on whether the account holds any grant, so each is true for its reader.
 * No guard wraps this route (`RequireFeature`'s redirect chain ends here); there it fetches nothing.
 *
 * Given a `request` hint (a resource page's refused load, via `ResourceRefusal` below) it also offers
 * "Request access" (access-model C10, AC-9.2): the resource and the least role pre-filled, an optional
 * note, then "pending". No hint — another org's id, a nonexistent one, a guard redirect — no offer.
 */
export default function NoAccess({ request }: { request?: RequestHint | null } = {}) {
  const { admin, logout } = useAuth()
  const can = useCan()
  // `can`, not `permissions.length`: it knows a superadmin holds everything. Arrow, not `some(can)`: a
  // second parameter on `can` would receive the array index (the `.map(parseInt)` footgun).
  const holdsSomething = PERMISSION_KEYS.some(key => can(key))
  // A nameless account is asked for a name here, once, in place of the shell's banner (`NamePromptBanner.tsx`
  // stands down on this route). A closed banner silences the line too; the button keeps naming the fix.
  // Not in the community edition: its one user is the only one who sees anything.
  const nameless = admin ? isNameless(admin) : false
  const askName = isEnabled('users_invites') && nameless && !readOnboardingStepsDone(admin?.preferences).has(NAME_PROMPT_DISMISSED_STEP)
  return (
    <InterstitialPanel
      testId="no-access"
      title={request
        ? `No access to this ${scopeTypeLabel(request.scope.type).toLowerCase()}`
        : holdsSomething ? 'No access to that page' : 'No access'}
      body={request
        ? `You cannot open this ${scopeTypeLabel(request.scope.type).toLowerCase()}. Request access and `
          + 'the people who manage it will be asked.'
        : holdsSomething
        ? 'Ask an administrator for access. They need the address below.'
        : 'Nothing is granted to this account yet. Ask an administrator for access. They need the '
          + 'address below.'}
    >
        {admin?.email && (
          // The address on its own line: an email has no break points and split mid-domain inline at 375px.
          // `anywhere` remains the floor for longer addresses.
          <Box sx={{ marginTop: 0.5 }}>
            <Typography variant="caption" component="div" sx={{ color: 'text.secondary' }}>
              Signed in as
            </Typography>
            <Typography
              variant="body2"
              component="div"
              sx={{ fontWeight: 500, overflowWrap: 'anywhere' }}
            >
              {admin.email}
            </Typography>
          </Box>
        )}
        {!request && askName && (
          <Typography variant="body2" data-testid="no-access-name-prompt" sx={{ marginTop: 0.5 }}>
            Others see your email, not a name.
          </Typography>
        )}
        {request && <RequestAccess hint={request} />}
        {/* Embedded in a resource page (`request` set) the rail carries Profile and Sign out; the terminal
            page is the one place a zero-grant account finds them. */}
        {!request && <Box sx={{ display: 'flex', justifyContent: 'flex-start', gap: 1, marginTop: 0.5 }}>
          {/* Bullet 1's carve-out must reach a zero-grant account too; its empty rail has no Profile entry
              (Sidebar.tsx's footer button is not enough). */}
          <Button
            variant="outlined"
            component={Link}
            to="/profile"
            data-testid="no-access-profile-btn"
            sx={{ minHeight: 44 }}
          >
            {nameless ? ADD_NAME : 'Edit your profile'}
          </Button>
          <Button
            variant="outlined"
            onClick={() => void logout()}
            data-testid="no-access-signout-btn"
            // The rail's sign-out is desktop-footer or More-sheet only; this one is always on screen.
            sx={{ minHeight: 44 }}
          >
            Sign out
          </Button>
        </Box>}
    </InterstitialPanel>
  )
}

/** The roles a request may ask for, least first: the project roles valid at `scope` (the API's
 *  `REQUESTABLE_ROLES`; org roles are handed out, never asked for). */
function requestableRoles(roles: AccessRole[], scope: RequestHint['scope']['type']): AccessRole[] {
  return roles
    .filter(role => role.level === 'project' && role.valid_scopes.includes(scope))
    .sort((a, b) => a.rank - b.rank)
}

/** The caller's own open request for this scope, if any: a reload shows "pending", not a second form. */
function openRequestFor(rows: AccessRequest[] | undefined, hint: RequestHint): AccessRequest | undefined {
  return rows?.find(r => r.state === 'pending' && r.scope.type === hint.scope.type && r.scope.id === hint.scope.id)
}

function RequestAccess({ hint }: { hint: RequestHint }) {
  const queryClient = useQueryClient()
  const roles = useQuery({ queryKey: ['access-roles'], queryFn: listRoles, staleTime: 5 * 60_000 })
  const mine = useQuery({ queryKey: ['access-requests', 'mine'], queryFn: () => listRequests('mine'), staleTime: REQUESTS_STALE_MS })
  const choices = requestableRoles(roles.data ?? [], hint.scope.type)
  const [role, setRole] = useState('')
  const [note, setNote] = useState('')
  const { busy: submitting, formError: error, run } = useSubmit()
  const [filed, setFiled] = useState<AccessRequest | null>(null)

  // Pre-fill once the catalog arrives: the hint's role, else the least (what the API defaults to).
  useEffect(() => {
    if (role || choices.length === 0) return
    setRole(choices.some(c => c.slug === hint.role) ? hint.role! : choices[0].slug)
  }, [choices, hint.role, role])

  const pending = filed ?? openRequestFor(mine.data, hint)
  const type = scopeTypeLabel(hint.scope.type).toLowerCase()

  async function submit() {
    await run('The request could not be sent.', async () => {
      const row = await createRequest({ scope: hint.scope, role: role || null, note: note.trim() || null })
      setFiled(row)
      void queryClient.invalidateQueries({ queryKey: ['access-requests'] })
    })
  }

  return (
    <Box data-testid="request-access" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, marginTop: 0.5 }}>
      <Divider />
      <Typography variant="subtitle2" component="h2">
        Request access
      </Typography>
      {pending ? (
        <Box data-testid="request-access-pending" sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
          <Banner severity="info">
            Pending: {pending.role.name} on this {type}. You will be notified of the decision.
          </Banner>
          <Box>
            <Button component={Link} to="/support?tab=requests&box=mine" size="small" sx={{ minHeight: 44 }}>
              View your requests
            </Button>
          </Box>
        </Box>
      ) : mine.isPending || roles.isPending ? (
        <LoadingState />
      ) : (
        <>
          <TextField
            select
            label="Role"
            value={role}
            onChange={e => setRole(e.target.value)}
            size="small"
            data-testid="request-access-role"
            helperText={choices.find(c => c.slug === role)?.description}
            disabled={choices.length === 0}
          >
            {choices.map(c => (
              <MenuItem key={c.slug} value={c.slug}>
                {c.name}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            label="Note (optional)"
            value={note}
            onChange={e => setNote(e.target.value.slice(0, NOTE_MAX))}
            size="small"
            multiline
            minRows={2}
            placeholder="Why you need it"
            data-testid="request-access-note"
            slotProps={{ htmlInput: { maxLength: NOTE_MAX } }}
          />
          {error && <Banner severity="error">{error}</Banner>}
          <Box>
            <Button
              variant="contained"
              onClick={() => void submit()}
              disabled={submitting}
              data-testid="request-access-submit"
              sx={{ minHeight: 44 }}
            >
              {submitting ? 'Sending…' : 'Request access'}
            </Button>
          </Box>
        </>
      )}
    </Box>
  )
}

/** A resource page's "not found" state, made honest: when the address names a resource of the caller's
 *  own org they cannot open, this is NoAccess with the request offer; otherwise `fallback`, as before.
 *  The resource comes from the address (`lib/accessRequests.ts::resourceForPath`), the verdict from the
 *  API (`probeHint`). */
export function ResourceRefusal({ fallback }: { fallback: ReactNode }) {
  const { pathname } = useLocation()
  const can = useCan()
  const resource = resourceForPath(pathname)
  const holdsMembersFloor = can('projects:view')
  const hint = useQuery({
    queryKey: ['access-hint', resource?.type, resource?.id, holdsMembersFloor],
    queryFn: () => probeHint(resource!, holdsMembersFloor),
    enabled: resource !== null,
    // A refusal is re-asked on every visit: an approval elsewhere must open the page on the next one.
    staleTime: 0,
    gcTime: 0,
  })
  if (resource === null) return <>{fallback}</>
  if (hint.isPending) return <LoadingState />
  return hint.data ? <NoAccess request={hint.data} /> : <>{fallback}</>
}
