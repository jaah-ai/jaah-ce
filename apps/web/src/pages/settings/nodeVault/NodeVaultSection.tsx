import { useCallback, useEffect, useRef, useState, type JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import ExternalLink from '../../../ui/ExternalLink'
import Typography from '@mui/material/Typography'
import { ApiError } from '../../../api'
import { useAuth } from '../../../auth'
import { danger, useConfirm } from '../../../components/ConfirmDialog'
import { useApiRead } from '../../../hooks/useApiRead'
import { useRowActions } from '../../../hooks/useBusyIds'
import { useUrlFilters } from '../../../hooks/useUrlFilters'
import { FONT_WEIGHT } from '../../../theme'
import Banner from '../../../ui/Banner'
import LoadingState from '../../../ui/LoadingState'
import { KeyValue, KeyValueList } from '../../../ui/KeyValueList'
import { countOf } from '../../../lib/humanize'
import { shortHash } from '../../../lib/nodeVaultKeys'
import { planSwitchOn } from '../../../lib/planSwitches'
import { shortDate } from '../../../lib/relativeTime'
import { usePublicSettings } from '../../authEntry'
import { getNodeVault, NODEVAULT_AUDIT_URL, retireKey, startOver, type NodeVaultStatus } from './api'
import KeyStepper from './KeyStepper'
import NodeGrantList from './NodeGrantList'
import Tip from '../../../ui/Tip'

/* Settings → NodeVault. Org Owner/Admin only — `Settings.tsx`
 * mounts it for an org-admin seat and every route is `@org_admin`; the API is the boundary. One read,
 * `GET /api/nodevault`, drives the whole panel: the plan flags, the status card, the node list, and the
 * running operation whose `step` places the Setup stepper (server-owned, so a reload resumes it).
 *   • not in the plan and never set up → the API refuses (403) and the panel says so (AC-1.2);
 *   • `frozen` (plan off after setup) → existing credentials keep working, new work is refused (AC-1.3);
 *   • a closed setup's Result stays on screen through `?nvop=<operation id>` until Done, a rotation's through
 *     `?nvrot=<operation id>`;
 *   • Rotate opens the Rotation stepper below the keys, which stay in view so each old key's credential count
 *     — and so whether its Retire is enabled (AC-6.7) — follows the rekey live. */

export const FROZEN = 'NodeVault isn’t included in your plan'

const POLL_BUSY_MS = 4000
const POLL_IDLE_MS = 30000
const RESULT_PARAM = 'nvop'
const ROTATION_PARAM = 'nvrot'
/** The read's answer for an org the API refuses (403): an answer, not a failure — nothing is drawn (AC-1.2). */
const NOT_IN_PLAN = 'not-in-plan'

const KIND_WORDS: Record<string, string> = {
  rotation: 'A key rotation is running.',
  migration: 'Credentials are moving to NodeVault.',
  retire: 'An old key is being retired.',
}

/** AC-6.7's reason a key can't be retired yet — the server's own wording (`routes/nodevault.py::keys_in_use`). */
export function keysInUse(count: number): string {
  return count === 1 ? '1 credential still uses this key' : `${count} credentials still use this key`
}

const nodesWord = (n: number) => countOf(n, 'node')

/** What Start over loses, named: the credentials sealed under the key no node holds. */
export function startOverMessage(count: number, frozen: boolean): string {
  const lost = count === 0 ? ''
    : count === 1 ? ' 1 credential sealed under it can’t be opened again: re-enter it.'
      : ` ${count} credentials sealed under it can’t be opened again: re-enter them.`
  return `No node holds the current key, so it is retired.${lost}${frozen ? '' : ' Then set up NodeVault with a new key.'}`
}

/** The reader-docs page the not-set-up state links: the explanation, not the how-to (Set up starts that). */
const HOW_IT_WORKS_PATH = '/using-jaah/credentials/how-nodevault-works'

/* What NodeVault is, before the lone Set up button. The docs base is the operator's `signup_docs_url` (the
 * pre-auth settings read the sign-up pages share), so a console pointed at another docs site links there;
 * no base, no link. Its own component so the read happens only in this state. */
function NodeVaultIntro(): JSX.Element {
  const { docsUrl } = usePublicSettings().signupLinks
  return (
    <Typography variant="body2" sx={{ color: 'text.secondary', maxWidth: '72ch' }} data-testid="nodevault-intro">
      Seals credentials with a key only your nodes hold, so Jaah can’t open them.
      {docsUrl && (
        <>
          {' '}
          <ExternalLink href={`${docsUrl.replace(/\/+$/, '')}${HOW_IT_WORKS_PATH}`} sx={{ whiteSpace: 'nowrap' }}>
            How NodeVault works
          </ExternalLink>
        </>
      )}
    </Typography>
  )
}

export default function NodeVaultSection(): JSX.Element {
  const { admin } = useAuth()
  const orgId = admin?.org?.id ?? ''
  // An org the API would refuse (`nodevault_enabled` off) draws nothing, without a read that can only 403.
  const readable = planSwitchOn(admin, 'nodevault_enabled')
  const [setupOpen, setSetupOpen] = useState(false)
  const [rotateOpen, setRotateOpen] = useState(false)
  const { busy: retiring, error: retireError, run } = useRowActions()
  const [confirm, confirmDialog] = useConfirm()
  // The operation in progress lives in the URL (`hooks/useUrlFilters`): a reload resumes the stepper. `''` ends it.
  const [searchParams, setParam] = useUrlFilters()
  const resultOp = searchParams.get(RESULT_PARAM)
  const rotationOp = searchParams.get(ROTATION_PARAM)
  const setResultOp = useCallback((id: string) => setParam({ [RESULT_PARAM]: id }), [setParam])

  /* One react-query read, polled faster while an operation runs or a stepper is open. The 403 is caught INTO
     the answer rather than left to the error banner: the panel's own says-nothing branch below, not
     `useApiRead`'s banner, is what a refused org gets. */
  const stepping = setupOpen || rotateOpen || resultOp !== null || rotationOp !== null
  const { data, error: loadError, refresh: load } = useApiRead<NodeVaultStatus | typeof NOT_IN_PLAN>(
    ['nodevault'],
    () => getNodeVault().catch((err: unknown) => {
      if (err instanceof ApiError && err.status === 403) return NOT_IN_PLAN
      throw err
    }),
    'Couldn’t load NodeVault. Reload the page.',
    {
      enabled: readable,
      refetchInterval: s => stepping || (s !== null && s !== NOT_IN_PLAN && s.operation != null) ? POLL_BUSY_MS : POLL_IDLE_MS,
    },
  )
  const notInPlan = data === NOT_IN_PLAN
  const status = data === NOT_IN_PLAN ? null : data

  /* Remember a running setup or rotation, so its Result can be shown after it closes (and after a reload).
     Written only when an ANSWER names an operation this mount has not seen (`seenOp`), never because the param
     changed: Abandon clears the param while the cached answer still names the operation, and that answer must
     not write it back — the one re-read `exit` asks for then lands with no operation. A poll that left before
     the Abandon is dropped by that re-read (react-query cancels the fetch a `refetch` overtakes), and even one
     that landed would name the id already seen. */
  const seenOp = useRef<string | null>(null)
  useEffect(() => {
    const op = status?.operation ?? null
    const id = op?.id ?? null
    if (id === seenOp.current) return
    seenOp.current = id
    if (op?.kind === 'setup') setResultOp(op.id)
    if (op?.kind === 'rotation') setParam({ [ROTATION_PARAM]: op.id })
  }, [status, setResultOp, setParam])

  // Opening a stepper re-reads at once: the first fresh status must not wait a poll interval.
  function openSetup() {
    setSetupOpen(true)
    void load()
  }

  function openRotation() {
    setRotateOpen(true)
    void load()
  }

  function exit() {
    setSetupOpen(false)
    setResultOp('')
    void load()
  }

  function exitRotation() {
    setRotateOpen(false)
    setParam({ [ROTATION_PARAM]: '' })
    void load()
  }

  async function retire(hash: string) {
    if (!await confirm(danger('Retire', {
      title: 'Retire key?',
      identity: <Box component="span" sx={{ fontFamily: 'monospace' }}>{shortHash(hash)}</Box>,
      message: 'It is deleted from every node. This can’t be undone. No credential uses it.',
    }))) return
    await run(hash, 'Couldn’t retire the key. Try again.', () => retireKey(hash))
    void load()
  }

  async function startAfresh(count: number) {
    if (!await confirm(danger('Start over', { title: 'Start NodeVault over?', message: startOverMessage(count, status?.frozen ?? false) }))) return
    await run('start-over', 'Couldn’t start over. Try again.', () => startOver())
    void load()
  }

  if (notInPlan || !readable) {
    return <></>
  }
  if (!status) return loadError ? <Banner severity="error">{loadError}</Banner> : <LoadingState />

  const op = status.operation
  const inSetup = op?.kind === 'setup' || setupOpen || (resultOp !== null && op === null)
  const inRotation = op?.kind === 'rotation' || rotateOpen || (rotationOp !== null && op === null)

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }} data-testid="nodevault-section">
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {status.frozen && <Banner severity="warning" testId="nodevault-frozen">{FROZEN}. Existing NodeVault credentials keep working.</Banner>}

      {inSetup && orgId ? (
        <KeyStepper kind="setup" status={status} orgId={orgId} resultOperationId={resultOp} onRefresh={load} onExit={exit} />
      ) : !status.setup ? (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, alignItems: 'flex-start' }}>
          <NodeVaultIntro />
          <Button variant="contained" disabled={!status.allowed || !orgId} onClick={openSetup}>Set up</Button>
          {!status.allowed && <Typography variant="caption" sx={{ color: 'text.secondary' }}>{FROZEN}</Typography>}
        </Box>
      ) : (
        <>
          {op && op.kind !== 'rotation' && (
            <Banner severity="info" testId="nodevault-operation">
              {KIND_WORDS[op.kind] ?? 'A NodeVault operation is running.'}
              {op.kind === 'retire' && ' Offline nodes confirm when they reconnect.'}
            </Banner>
          )}
          <KeyValueList testId="nodevault-status">
            <KeyValue label="Current key">
              {status.current_key ? (
                <>
                  <Tip title={status.current_key.hash}><Box component="span" sx={{ fontFamily: 'monospace', fontWeight: FONT_WEIGHT.semibold }} data-testid="nodevault-current-key">
                    {shortHash(status.current_key.hash)}
                  </Box></Tip>
                  <Typography component="span" variant="body2" sx={{ color: 'text.secondary' }}> · since {shortDate(status.current_key.created_at)}</Typography>
                </>
              ) : '—'}
            </KeyValue>
            {status.previous_keys.length > 0 && (
              <KeyValue label="Previous keys">
                <Box component="ul" sx={{ m: 0, p: 0, listStyle: 'none' }} data-testid="nodevault-previous-keys">
                  {status.previous_keys.map(k => {
                    const retireOp = op?.kind === 'retire' && op.key_hash === k.hash ? op : null
                    const blocked = k.credential_count > 0
                      ? keysInUse(k.credential_count)
                      : !status.allowed ? FROZEN
                        : op ? 'Available when the running operation finishes' : ''
                    return (
                      <Box
                        component="li"
                        key={k.hash}
                        data-testid="nodevault-previous-key"
                        data-key={k.hash}
                        sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1.5, rowGap: 0, py: 0 }}
                      >
                        <Typography variant="body2" component="span">
                          <Tip title={k.hash}><Box component="span" sx={{ fontFamily: 'monospace' }}>{shortHash(k.hash)}</Box></Tip>
                          <Typography component="span" variant="body2" sx={{ color: 'text.secondary' }}>
                            {' · '}{countOf(k.credential_count, 'credential')}
                          </Typography>
                        </Typography>
                        {k.state === 'retiring' ? (
                          <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="nodevault-retiring">
                            Retiring{retireOp
                              ? retireOp.counts.nodes_pending > 0 ? ` · ${nodesWord(retireOp.counts.nodes_pending)} still to confirm` : ' · finishing'
                              : ''}
                          </Typography>
                        ) : (
                          <>
                            <Button
                              size="small"
                              variant="outlined"
                              disabled={blocked !== '' || retiring.size > 0}
                              onClick={() => void retire(k.hash)}
                            >
                              {retiring.has(k.hash) ? 'Retiring…' : 'Retire'}
                            </Button>
                            {blocked && (
                              <Typography variant="caption" sx={{ color: 'text.secondary' }} data-testid="nodevault-retire-blocked">{blocked}</Typography>
                            )}
                          </>
                        )}
                      </Box>
                    )
                  })}
                </Box>
              </KeyValue>
            )}
          </KeyValueList>
          {retireError && <Banner severity="error" testId="nodevault-retire-error">{retireError}</Banner>}
          {inRotation && orgId ? (
            <KeyStepper kind="rotation" status={status} orgId={orgId} resultOperationId={rotationOp} onRefresh={load}
              onStarted={id => setParam({ [ROTATION_PARAM]: id })} onExit={exitRotation} />
          ) : (
            <>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap' }}>
                <Button variant="outlined" disabled={!status.allowed || !orgId || op !== null} onClick={openRotation}>
                  Rotate key
                </Button>
                {status.can_start_over && status.current_key && (
                  <Button variant="outlined" color="error" disabled={retiring.size > 0}
                    onClick={() => void startAfresh(status.current_key?.credential_count ?? 0)} data-testid="nodevault-start-over">
                    {retiring.has('start-over') ? 'Starting over…' : 'Start over'}
                  </Button>
                )}
                <Link component={RouterLink} to={NODEVAULT_AUDIT_URL} variant="body2">Audit</Link>
              </Box>
              <Box>
                <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.semibold, mb: 1 }}>Nodes</Typography>
                <NodeGrantList nodes={status.nodes} keyHash={status.current_key?.hash ?? null} onChanged={() => void load()} />
              </Box>
            </>
          )}
        </>
      )}
      {(inSetup || inRotation) && !orgId && <Banner severity="error">Your session has no organization. Reload the page.</Banner>}
      {confirmDialog}
    </Box>
  )
}
