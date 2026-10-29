import { useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { get, post } from '../api'
import { FACTOR_ERRORS, enterOrg, idpSentence, switchOrg as postSwitch, switchRefusal } from '../lib/orgSwitch'
import { mfaPath, useAuth } from '../auth'
import { googleMount, useGoogleButton, usePublicSettings } from '../pages/authEntry'
import Banner from '../ui/Banner'
import InterstitialPanel from '../ui/InterstitialPanel'
import LoadingState from '../ui/LoadingState'
import SwitchCodeForm from './account/SwitchCodeForm'

/* The deep-link interstitial (multi-org R9/AC13). A detail page whose object 404s under the ACTIVE org renders
 * this in place of its not-found: it asks `GET /api/auth/locate/<type>/<id>` which of the caller's OTHER orgs
 * holds the object. 200 → "This belongs to B — Switch to B", carrying B's NAME ONLY (the API returns nothing
 * of the object, so nothing of it can be shown). Anything else → the page's own `fallback`, unchanged — a
 * non-member gets exactly today's not-found.
 *
 * Never a silent switch: only the button switches (`POST /api/auth/switch`). `session` → reload the same URL,
 * now acting as B. `reauth_required` → B's sign-in policy: `otp` asks for a code here; GitHub/Microsoft follow
 * `start_url`; Google mounts its own button, whose credential carries `switch_to`. */

export type LocatableType = 'project' | 'task' | 'node' | 'account' | 'credential' | 'feature'

type Located = { org_id: string; org_name: string }

/* Each type's id domain (`authz.ObjectSpec.id_type`): a malformed id is a not-found with nothing to ask. */
const INT_ID = /^\d+$/
const UUID_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const ID_SHAPE: Record<LocatableType, RegExp> = {
  project: INT_ID,
  task: INT_ID,
  node: UUID_ID,
  account: UUID_ID,
  credential: UUID_ID,
  feature: UUID_ID,
}

async function locate(objectType: LocatableType, objectId: string): Promise<Located | null> {
  try {
    return await get<Located>(`/api/auth/locate/${objectType}/${encodeURIComponent(objectId)}`)
  } catch {
    // 404 (the standard answer), and any other failure, leaves the page's own not-found in place.
    return null
  }
}

export default function OtherOrgGate({
  objectType,
  objectId,
  fallback,
}: {
  objectType: LocatableType
  objectId: string | undefined
  fallback: ReactNode
}) {
  const askable = !!objectId && ID_SHAPE[objectType].test(objectId)
  const located = useQuery({
    queryKey: ['locate', objectType, objectId],
    queryFn: () => locate(objectType, objectId!),
    enabled: askable,
    // Asked afresh on every visit: a membership gained or lost elsewhere must change the answer.
    staleTime: 0,
    gcTime: 0,
  })
  if (!askable) return <>{fallback}</>
  if (located.isPending) return <LoadingState />
  return located.data ? <SwitchOrgPrompt org={located.data} /> : <>{fallback}</>
}

/** The "switch to open it" card, also shown by a page that already knows which org holds what was asked for
 *  (Audit's Runs leaf for an operator acting in a tenant org). `title` defaults to "This belongs to <org>". */
export function SwitchOrgPrompt({ org, title }: { org: Located; title?: string }) {
  const { admin } = useAuth()
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [door, setDoor] = useState<'otp' | 'google' | null>(null)
  const [codeError, setCodeError] = useState<string | undefined>(undefined)
  const { googleClientId } = usePublicSettings()
  const googleRef = useGoogleButton(
    googleClientId,
    door === 'google',
    credential => void switchWithGoogle(credential),
  )

  async function switchOrg(withCode?: string) {
    setBusy(true)
    setError(null)
    setCodeError(undefined)
    try {
      const step = await postSwitch(org.org_id, withCode)
      // Leaving the page: the button stays disabled until it unloads.
      // Re-pinned and the old org's cache dropped (`enterOrg`), then this same URL reloads in the new org.
      if (step.kind === 'session') return enterOrg(step.org ?? { id: org.org_id, slug: '', name: org.org_name }, { stay: true })
      if (step.kind === 'leaving') return
      if (step.kind === 'otp') {
        setDoor('otp')
        if (step.error) setCodeError(FACTOR_ERRORS[step.error])
      } else if (step.door === 'google') {
        setDoor('google')
      } else {
        setError(idpSentence(org.org_name, step.door))
      }
    } catch (err) {
      setError(switchRefusal(err, org.org_name))
    }
    setBusy(false)
  }

  async function switchWithGoogle(credential: string) {
    setError(null)
    try {
      const answer = await post<{ mfa?: 'verify' | 'enrol' }>('/api/auth/google', {
        credential,
        switch_to: org.org_id,
      })
      if (answer?.mfa) navigate(mfaPath(answer.mfa), { replace: true })
      else enterOrg({ id: org.org_id, slug: '', name: org.org_name }, { stay: true })
    } catch {
      setError('Google sign-in failed. Try again.')
    }
  }

  return (
    <InterstitialPanel
      testId="other-org-interstitial"
      titleTestId="other-org-title"
      title={title ?? `This belongs to ${org.org_name}`}
      body={`${admin?.org?.name ? `You're acting as ${admin.org.name}. ` : ''}Switch to open it.`}
    >
        {error && <Banner severity="error">{error}</Banner>}
        {door === 'otp' ? (
          <SwitchCodeForm busy={busy} error={codeError} onCode={withCode => void switchOrg(withCode)} testIdPrefix="other-org" />
        ) : door === 'google' ? (
          <Box>
            <Typography variant="body2" sx={{ mb: 1, overflowWrap: 'anywhere' }}>
              {`${org.org_name} requires Google sign-in.`}
            </Typography>
            <Box ref={googleRef} sx={googleMount} data-testid="other-org-google" />
          </Box>
        ) : (
          <Button
            variant="contained"
            onClick={() => void switchOrg()}
            disabled={busy}
            data-testid="other-org-switch"
            sx={{ minHeight: 44, alignSelf: 'flex-start', maxWidth: '100%', textTransform: 'none' }}
          >
            <Box component="span" sx={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              Switch to {org.org_name}
            </Box>
          </Button>
        )}
    </InterstitialPanel>
  )
}
