import { type CSSProperties, type ReactNode, useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link as RouterLink, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { SxProps, Theme } from '@mui/material/styles'
import { ApiError, apiDetail, del, post, put } from '../../api'
import { useAuth } from '../../auth'
import { actAsOnce, enterActAs } from '../../lib/actAs'
import { isEnabled } from '../../lib/editionFeatures'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { toggled } from '../../lib/sets'
import { useChangeOrgAction } from '../../components/ChangeOrgDialog'
import { useOpenDetailPath } from '../../components/ListDetail'
import { useConfirm } from '../../components/ConfirmDialog'
import HoverCard from '../../components/HoverCard'
import { useLiveRefetch } from '../../hooks/useLiveRefetch'
import { useEventStream } from '../../hooks/useEventStream'
import {
  isAuthRejected, isRefreshBlocked, quotaAgeMs, quotaClass, QUOTA_STALE_MS,
} from '../../lib/quota'
import { shortNode } from '../../lib/nodeName'
import { absoluteTime, compactSpan, humanDuration } from '../../lib/relativeTime'
import type {
  Account, AccountNode, AccountWorktree, ClaudeAccount, ClaudeResources, OrgRef, ProviderChoice, QuotaData,
} from '../../types'
import { CompactVerbLabel, compactVerbProps } from '../../ui/CompactVerb'
import { PRESS_SWAP_SX, PressSwap } from '../../ui/pressSwap'
import Banner from '../../ui/Banner'
import { Icon } from '../../ui/icons'
import LoadFailedState from '../../ui/LoadFailedState'
import ListStates from '../../ui/feed/ListStates'
import { rowPadLeft } from '../../ui/feed/geometry'
import type { FeedColumn, FeedRowSpec, OverflowItem } from '../../ui/feed/types'
import { editItem } from '../../components/tasks/rowActions'
import { ACCOUNTS_LIST_CONTAINER_SX, ACCOUNTS_NO_ENABLED, ACCOUNTS_PROVIDER_UNDER, ELLIPSIS_SX, FEED_NO_ID, PHONE, tapTargetSx } from '../../ui/listStyles'
import { orgSlots, useOrgVisible, useOtherOrgOf } from '../../ui/OrgIndicator'
import StatusChip from '../../ui/StatusChip'
import { toneFor } from '../../ui/statusTone'
import type { Tone } from '../../ui/StatusChip'
import { useRowActions } from '../../hooks/useBusyIds'
import { DisabledReason } from '../../ui/DisabledReason'
import PageHeader from '../../ui/PageHeader'
import { useAwaitingApprovalReason } from '../../lib/orgLifecycle'
import { useAgentProviders } from '../../hooks/useAgentProviders'
import { isDefaultProvider } from '../../lib/providerCatalogue'
import {
  accountEditPath, accountLoginPath, dispatchBody, EXPLICIT_ONLY_TITLE,
  NEW_ACCOUNT_PATH, NO_LOGIN_PERMISSION_REASON, othersAccountCell, pendingDispatchFailure, providerCell, slugOf, takePendingDispatch,
  useCanLogIn,
} from './accountModel'
import { ACCOUNTS_KEY, getResources, listAccounts, RESOURCES_KEY, setAccountEnabled } from './api'
import { nodeLinkUrl } from '../nodes/nodeLinkStore/api'
import { nodesAddPath } from '../../components/addNode/deepLink'
import ShareSubscriptionsAction, { useShareSubscriptionsShown } from '../nodes/nodeLinkStore/ShareSubscriptionsAction'
import { ImportAccountAction } from './AccountTransfer'

/* Agents > Agents (the runner accounts): the credential accounts the fleet's runners log in as, their per-node OAuth
 * sessions and remaining quota. A tab panel: `pages/nodes/BrainsPage.tsx` owns the strip; Add account rides
 * it (see its call site).
 * Served at `/accounts` (`routeGroups.ts`'s `MACHINES_TAB_PATHS`). Gated `accounts:view` at the route
 * (App.tsx); DB writes stay org-admin-only.
 */

// ── Page-scoped Material spellings ──
// `quotaClass()` (lib/quota.ts) owns the thresholds; this is the one place its verdict becomes a palette
// colour. `''` (no reading) has no entry: an unmeasured number is not a tier.
const QUOTA_TONE_COLOR: Record<string, string> = {
  'quota-low': 'success.main',
  'quota-mid': 'warning.main',
  'quota-high': 'error.main',
}

/* `ui/feed/FeedList` owns the row's pixels and `ui/feed/geometry.ts` its bands. Widths: QUOTA_SLOT — a
 * 32px reserved percentage measure plus the `3h 20m` countdown; SESSIONS_SLOT — a count and an `N busy`
 * pill; TOGGLE_SLOT — the state word ("Disabled", ~47px at the 11px slot face) with its 4px padding.
 * QUOTA_SLOT is priced against what it renders: 32 + 4 gap + 8 chars of 11px mono (~6.6px) = 89, so 92.
 * Longer notes (`⚠ 4h old`, `⚠ blocked · 4h old`) ellipsize with their text on `title`. */
const QUOTA_SLOT = 92
const SESSIONS_SLOT = 62
const TOGGLE_SLOT = 56
/* The per-node child row's state; its quota is the account row's. */
const NODE_STATE_SLOT = 78

/* The provider column (account row and per-node children), priced against the widest label the catalogue
 * serves: PRD R1.2 requires the provider at 390px with no truncation.
 * Measured with `canvas.measureText` at `FeedList`'s 11px `system-ui` slot face: 66, 75, 77 and 96px; 99 is
 * the widest plus the rows' usual 3px margin. Labels are not named here: this file may not name a vendor
 * (a repo test enforces it).
 * Adding a provider re-prices this constant, enforced by a repo test that reads
 * this line and fails both ways (label too long, or this narrowed below the measured width).
 * Measured in a browser at the supported widths with the 96px label on a row: the cell is
 * 99px, never clipped (`scrollWidth == clientWidth`), and the row stays one line at 448, 1024 and 1440px;
 * the extra 19px came out of the identity, which ellipsizes on the phone as before.
 *
 * The identity pays (ellipsized by `EMAIL_SX`): a shortened address still identifies, a shortened vendor
 * does not; `5h` is PRD R5.3's to price.
 */
const PROVIDER_SLOT = 99


// Every DB write addresses the `accounts` row by id, so a line with `account_id: null` has nothing to
// write to. Fail closed: disabled, with this title.
const NO_ROW_TITLE =
  'A node reports this account, but it was never added here, so it cannot be enabled, disabled or '
  + 'removed yet. Add it with Add account under the same name.'

// The row exists but its editable fields are not on this payload: `name`/`description` come only from
// `GET /api/accounts` (`accounts:view`; note the no-credentials pass serves `name`
// in the blob's `email` slot, so it is not confidential), joined on `account_id` (`fetchAccountRecords`).
// If that request failed, fail closed with this title.
const NO_RECORD_TITLE = 'Name and description did not load. Reload to edit.'

/* `GET /api/resources` refused this seat (403): nothing on the roster is theirs to see. */
const REFUSED_TITLE = 'Your role can’t view account status. Ask an admin for access.'

/* Why an operator is offered no login on another org's account: Re-login, Sign in, Add to node and a
 * node's Login open the login page, which reads and dispatches (`POST /api/commands`) as the viewer's own org. */
const actAsToLogIn = (org: OrgRef) => `Act as ${org.name} to log it in`

/** Why a row offers no login: the permission, or another org's account (`actAsToLogIn`). */
function loginRefusal(canLogIn: boolean, otherOrg: OrgRef | null): string {
  return canLogIn && otherOrg ? actAsToLogIn(otherOrg) : NO_LOGIN_PERMISSION_REASON
}
/* A refused row's disc: grey `?`, as the per-node `no_reading` — the state is not ours to see, not a fault. */
const REFUSED_MARK = { tone: toneFor(null), glyph: '?', label: 'status hidden', title: REFUSED_TITLE } as const



/** What the kind chip means. The vendor is the account's own: an Anthropic sentence on another vendor's
 *  key would be false. `null` label (Claude, or no catalogue row) keeps the baseline's wording. */
function apiKeyAccountTitle(vendorLabel: string | null): string {
  return `Stored ${vendorLabel ?? 'Anthropic'} API key: no login, no plan quota, billed per token.`
}

/* An API-key account whose key is gone (`api_key_set` false: the broker row was deleted). It cannot run
anything until a key is added from Edit — said on the row, never left looking healthy. */
const NO_KEY_TITLE =
  'No key is stored for this account any more, so no runner can use it. Add a key from Edit.'


/* `n/a`, not `—`: the dash means "no reading yet" (a temporary, actionable state); an API-key account can
never have one. */
const NO_QUOTA_TITLE = 'No quota: an API key is billed per token.'

/* The second way to have no quota: the provider publishes no reading of its plan
(`agent_providers.has_quota_model`, asked of the control plane). Both titles avoid the dash's "not
measured yet" and `0%`'s "exhausted" (PRD R5.3). */
const PROVIDER_NO_QUOTA_TITLE = 'Not available for this provider: it reports no quota.'

/* The baseline's two windows, used where the catalogue did not load: the treatment every row had before
providers declared their own (`agent_providers.quota_windows`). */
const DEFAULT_QUOTA_WINDOWS = ['5h', '7d']

/* The header row over the account line's slots (`FeedList`'s `columns`): each label borrows its slot's width and
 * gate, so it drops where the values do. The quota pair is named by the windows of the providers on screen when
 * they all agree (`quotaColumnWindows`), else by the default ones; each cell's hover names its own. */
function accountColumns(windows: string[]): { title: FeedColumn; slots: FeedColumn[] } {
  const [short, long] = [windows[0], windows[1] ?? DEFAULT_QUOTA_WINDOWS[1]]
  return {
    title: { key: 'name', label: 'Account' },
    slots: [
      { key: 'org', label: 'Org' },
      { key: 'sessions', label: 'Sessions' },
      { key: 'enabled', label: 'Enabled' },
      { key: 'provider', label: 'Provider' },
      { key: 'five', label: short, ariaLabel: `${short} quota used` },
      { key: 'week', label: long, ariaLabel: `${long} quota used` },
    ],
  }
}

/** The quota columns' windows: the one set every row with a reading to show declares, else the defaults (rows
 *  disagree, or none has a quota). */
function quotaColumnWindows(perRow: (string[] | null)[]): string[] {
  const shown = [...new Set(perRow.filter((w): w is string[] => w !== null).map(w => w.join('\u0000')))]
  return shown.length === 1 ? shown[0].split('\u0000') : DEFAULT_QUOTA_WINDOWS
}

/* A provider with one quota window (a daily one, say) has no weekly reading to show — "n/a", not the dash's
"not read yet". The sentence names the window that does exist. */
function noWeeklyWindowTitle(shortWindow: string): string {
  return `No weekly window: a single ${shortWindow} window.`
}

/* The third way to have no quota: this ONE account's node says there is none to read (`quota_model:
'none'`), though its provider reports quota for other accounts. */
const ACCOUNT_NO_QUOTA_TITLE = 'Not available for this account: its login has no readable quota. It stays in rotation.'

/* A reading that carries the long window but not the short one: this account's plan has no such window
(some subscriptions publish only a weekly limit), not a reading still to come. */
function noShortWindowTitle(shortWindow: string): string {
  return `No ${shortWindow} window on this account's plan: weekly limit only.`
}


/** Is this line an API-key account? Only the joined `/api/accounts` record carries the kind (the roster
 *  projection omits it). An `undefined` record answers false: the subscription treatment. */
function isApiKeyAccount(record: Account | undefined): boolean {
  return record?.auth_kind === 'api_key'
}

/** Does this line's provider publish no quota? From the control plane's catalogue
 *  (`GET /api/accounts/providers` via `useAgentProviders`), never a vendor name here
 *  (`app/agent_providers.py`). Fails to `false` for a missing
 *  record, an unread catalogue (an empty list) or an unknown provider: the existing treatment. */
function providerHasNoQuota(record: Account | undefined, providers: ProviderChoice[]): boolean {
  if (!record) return false
  return providers.some(p => p.value === record.provider && p.has_quota_model === false)
}

/** This line's quota window labels, short first, from the catalogue (`ProviderChoice.quota_windows`);
 *  `DEFAULT_QUOTA_WINDOWS` for a missing record, an unread catalogue or an unknown provider. */
function providerQuotaWindows(record: Account | undefined, providers: ProviderChoice[]): string[] {
  const windows = record ? providers.find(p => p.value === record.provider)?.quota_windows : undefined
  return windows && windows.length > 0 ? windows : DEFAULT_QUOTA_WINDOWS
}

// Two units, not `compactSpan`'s one: a 5h window's reset is read to the minute.
function fmtResetsIn(iso: string): string {
  const ms = Date.parse(iso) - Date.now()
  return ms <= 0 ? 'now' : humanDuration(ms / 1000, true)
}

/* How far ahead of a Claude login's hard expiry the row warns — the refresh token's fixed lifetime from login,
which no refresh extends. Same three days as the operator alert (`fleet_observe.LOGIN_EXPIRY_WARN_SECONDS`). */
const LOGIN_EXPIRY_WARN_MS = 3 * 24 * 3600 * 1000

/* The soonest node login expiring inside `LOGIN_EXPIRY_WARN_MS` and the node holding it (where an early re-login
goes), or null. A rejected, credential-less or absent node is skipped: it already needs its login, which the Nodes
column says (and its stored date may be stale). */
function soonestLoginExpiry(nodes: AccountNode[]): { iso: string; nodeId: string } | null {
  const now = Date.now()
  let soonest: { iso: string; nodeId: string; at: number } | null = null
  for (const n of nodes) {
    if (!n.refresh_expires_at || n.present === false || n.status === 'no_credentials' || isAuthRejected(n.quota)) continue
    const at = new Date(n.refresh_expires_at).getTime()
    if (Number.isNaN(at) || at <= now || at - now > LOGIN_EXPIRY_WARN_MS) continue
    if (!soonest || at < soonest.at) soonest = { iso: n.refresh_expires_at, nodeId: n.node_id, at }
  }
  return soonest && { iso: soonest.iso, nodeId: soonest.nodeId }
}

function QuotaCell({
  pct, resetsAt, authRejected = false, refreshBlocked = false, ageMs = null,
}: {
  pct: number | null | undefined
  resetsAt: string | null | undefined
  authRejected?: boolean
  // The reader is blocked in a way retrying won't fix; any percents shown are last-known.
  refreshBlocked?: boolean
  // Age of the reading, or null without a timestamp. Drives the stale marker.
  ageMs?: number | null
}) {
  const stale = ageMs !== null && ageMs >= QUOTA_STALE_MS
  // No number: say which kind of nothing — a blocked reader (our bug) or not read yet ('—').
  // Absence, not rejection, blanks this cell: rejection is fleet-wide (`bool_or`) while a login is per
  // (account, node), and the fold hands us another node's figure (`account_fold.effective_quota`) when one
  // reads. Testing `authRejected` first threw those figures away.
  if (pct == null) {
    if (authRejected) {
      // Not "re-login": the Nodes column says it per node, which is what tells you which node to fix.
      return (
        <Typography
          variant="caption"
          sx={{ color: 'text.disabled' }}
          title="No quota reading — a node cannot use this account (the Nodes column says which, and why)"
        >
          —
        </Typography>
      )
    }
    if (refreshBlocked) {
      return (
        <StatusChip
          label="reader blocked"
          tone={toneFor('badge-failed')}
          title={"The collector cannot refresh this account's token (refused before reaching the API). The\n"
            + "account itself is probably fine — check the jaahd log for 'oauth refresh BLOCKED'."}
        />
      )
    }
    return <Typography variant="caption" sx={{ color: 'text.disabled' }}>—</Typography>
  }
  const tier = quotaClass(pct)
  const ageLabel = ageMs === null ? 'undated' : `${compactSpan(ageMs)} old`
  // A stale or unreadable figure is muted and its note is warning-coloured; the note also spells "⚠" and
  // the age, so it survives greyscale and screen readers.
  const dulled = stale || refreshBlocked
  return (
    <Box
      /* A test/state handle on exactly the retired `.quota-stale` condition: the suite asserts a fresh reading
      carries no stale marker, which needs a selector that can still match. */
      data-testid={dulled ? 'quota-stale' : undefined}
      sx={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 0.5, whiteSpace: 'nowrap' }}
    >
      <Typography
        variant="mono"
        sx={{
          fontSize: '0.8125rem',
          fontWeight: 600,
          // A reserved measure so a column of readings stays a column of magnitudes.
          minWidth: 32,
          textAlign: 'right',
          color: dulled ? 'text.secondary' : (QUOTA_TONE_COLOR[tier] ?? 'text.primary'),
        }}
      >
        {pct}%
      </Typography>
      {/* The number and countdown share one slot whose `meta` gate drops them below 900px. */}
      {refreshBlocked ? (
        <Typography
          variant="mono"
          sx={{ fontSize: '0.6875rem', color: 'warning.main', display: 'inline' }}
          title={`Last successful reading: ${ageLabel}. The token refresh is being refused — see the\n`
            + "jaahd log for 'oauth refresh BLOCKED'."}
        >
          ⚠ blocked · {ageLabel}
        </Typography>
      ) : stale ? (
        <Typography
          variant="mono"
          sx={{ fontSize: '0.6875rem', color: 'warning.main', display: 'inline' }}
          title={`Last successful reading: ${ageLabel}`}
        >
          ⚠ {ageLabel}
        </Typography>
      ) : resetsAt ? (
        <Typography
          variant="mono"
          sx={{ fontSize: '0.6875rem', color: 'text.secondary', display: 'inline' }}
          title={`Resets in ${fmtResetsIn(resetsAt)}`}
        >
          {fmtResetsIn(resetsAt)}
        </Typography>
      ) : null}
    </Box>
  )
}

/* The quota slot for an account that has no quota — separate from `QuotaCell`, which reasons about a
 * reading that could exist. It occupies the number's position exactly (mono, 0.8125rem/600, 32px, right
 * aligned), so `n/a` sits in the same run as `71%`; no countdown. `text.disabled` like the dash, since
 * nothing is wrong; the word and the `title` carry the distinction. The title is a prop: two reasons,
 * `NO_QUOTA_TITLE` (API-key) and `PROVIDER_NO_QUOTA_TITLE` (vendor publishes none).
 */
function NoQuotaCell({ title }: { title: string }) {
  return (
    <Typography
      variant="mono"
      data-testid="acct-no-quota"
      title={title}
      sx={{
        fontSize: '0.8125rem',
        fontWeight: 600,
        minWidth: 32,
        textAlign: 'right',
        color: 'text.disabled',
      }}
    >
      n/a
    </Typography>
  )
}

// The project-worktrees this account is active on, on hovering the Sessions count.
function SessionsTip({ worktrees }: { worktrees: AccountWorktree[] }) {
  if (worktrees.length === 0) {
    return <Typography variant="caption" sx={{ color: 'text.secondary' }}>No live slots</Typography>
  }
  return (
    <Box sx={{ minWidth: 0 }}>
      <Typography variant="caption" sx={{ fontWeight: 600, display: 'block', mb: 0.25 }}>
        Active slots
      </Typography>
      {worktrees.map((w, i) => (
        <Box
          key={`${w.project}-${w.number}-${i}`}
          sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1 }}
        >
          <Typography variant="mono" sx={{ fontSize: '0.75rem' }}>{w.project} #{w.number ?? '—'}</Typography>
          <StatusChip
            label={w.session_status ?? 'idle'}
            tone={toneFor(w.session_status === 'busy' ? 'badge-running' : 'badge-idle')}
          />
        </Box>
      ))}
    </Box>
  )
}

/* The toggle pill sized to TOGGLE_SLOT: "Disabled" at the 11px slot face, 3px padding and its border fit 56px. */
// The swapping label lays its two faces out in a row (`ui/pressSwap.tsx`); a plain label keeps its ellipsis.
const SWAP_LABEL_SX = { '& .MuiChip-label': { display: 'inline-flex' } } as const

const STATE_CHIP_SX = {
  height: 20,
  maxWidth: '100%',
  '& .MuiChip-label': { px: '3px', fontSize: '0.6875rem' },
  '&.Mui-disabled': { pointerEvents: 'auto', cursor: 'not-allowed' },
} as const

/* The state's narrow home, wherever the toggle's slot is gated off (`acctEnabled`: a phone, a narrow roster): the
mark in the identity, read-only — the ⋯ item toggles it there. A wider gap after it keeps it apart from the email. */
const STATE_UNDER_SHOWN = { display: 'inline', mr: 0.75 } as const
const STATE_UNDER_SX = {
  display: 'none',
  fontSize: '0.6875rem',
  [FEED_NO_ID]: STATE_UNDER_SHOWN,
  [ACCOUNTS_NO_ENABLED]: STATE_UNDER_SHOWN,
  /* A phone carries it on the provider line instead (`STATE_ON_PROVIDER_LINE_SX`): here it would stand alone on a third line.
  Doubled, since the phone's roster also matches `ACCOUNTS_NO_ENABLED`, whose rule lands later. */
  [PHONE]: { '&&': { display: 'none' } },
} as const
const STATE_ON_PROVIDER_LINE_SX = { display: 'none', [PHONE]: STATE_UNDER_SHOWN } as const

/** The state's narrow mark: the toggle's own (`StateToggle`), so the state reads the same at every width. */
function StateUnderMark({ enabled, testId, sx }: { enabled: boolean; testId: string; sx: object }) {
  return (
    <Box
      component="span"
      data-testid={testId}
      aria-label={enabled ? 'enabled' : 'disabled'}
      title={enabled ? 'Enabled' : 'Disabled'}
      sx={[sx, { color: enabled ? 'success.main' : 'text.secondary' }]}
    >
      {enabled ? '✓' : 'Disabled'}
    </Box>
  )
}

// Per-row enable toggle → `PUT /api/accounts/{account_id}` `{ enabled }`: `Account.enabled` is what
// dispatch gates on and what GET /api/resources re-reads, so the write sticks. Flips optimistically
// (`onOptimistic`) until the refetch lands. Disabling the last enabled account is allowed; the parent's
// `warning` rides the Disable title, so it is read at the moment it matters, not as a standing row mark.
// Keyed on `account_id`, not `config_dir`: `GET /api/resources` blanks `config_dir` for tenants
// (`routes/portfolio.py`'s tenant pass), which disabled the optimistic path for them, and two rows can
// share a dir. `account_id` is what the write addresses and what `noRow` requires.
function StateToggle({
  account, warning, otherOrg, onOptimistic, onCompleted, onError,
}: {
  account: ClaudeAccount
  warning?: string
  otherOrg?: OrgRef | null
  onOptimistic: (accountId: string, enabled: boolean) => void
  onCompleted: () => void
  onError: (msg: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const accountId = account.account_id
  const next = !account.enabled
  const noRow = accountId == null

  async function submit() {
    if (busy || noRow) return
    setBusy(true)
    onOptimistic(accountId, next)
    try {
      // Another org's row goes as that org for this one request.
      await setAccountEnabled(accountId, next, actAsOnce(otherOrg ?? null))
      // The row is the authority the projection re-reads, so refetch straight away.
      onCompleted()
    } catch (err) {
      // Revert the optimistic flip and say why.
      onOptimistic(accountId, account.enabled)
      onError(apiDetail(err, 'Failed to update the account'))
    } finally {
      setBusy(false)
    }
  }

  return (
    /* The state in one word, pressable: a toggle button (`aria-pressed`) whose title names the change. A clickable
    pill, as the row's other pressable marks (`ui/StatusChip`'s `onClick`): the chip's own hover and focus fill. A real
    `<button>`, so `disabled` holds. */
    <Chip
      component="button"
      size="small"
      variant="outlined"
      color={account.enabled ? 'success' : 'default'}
      clickable
      /* Enabled is the normal state, so a ✓ (`ui/EnabledChip`'s mark); the exception keeps its word. */
      /* Pointed at, the pill shows the flip it makes (`ui/pressSwap.tsx`); a disabled pill keeps its state. */
      label={busy || noRow
        ? (account.enabled ? '✓' : 'Disabled')
        : <PressSwap rest={account.enabled ? '✓' : 'Disabled'} press={account.enabled ? { glyph: '⊘', label: 'Disable' } : { glyph: '✓', label: 'Enable' }} />}
      disabled={busy || noRow}
      aria-pressed={account.enabled}
      aria-label={account.enabled ? 'enabled' : 'disabled'}
      title={
        noRow ? NO_ROW_TITLE
        : account.enabled ? `Disable${warning ? `. ${warning}` : ''}`
        : 'Enable'
      }
      onClick={() => void submit()}
      sx={[STATE_CHIP_SX, busy || noRow ? null : PRESS_SWAP_SX, busy || noRow ? null : SWAP_LABEL_SX]}
    />
  )
}

/* The page's confirm is `ui/ConfirmDialog`'s modal (a feed row is one line; as `pages/Nodes.tsx`). It
 * names what is removed and what survives. */

// Per-node deprovision is its own route, not a field: apps/api and apps/jaahd deploy independently,
// and a dropped optional `node_id` would clear the pinned node instead (an older API ignores a field it does
// not know). `accountId` is a uuid string. The node segment is refused unless it is a safe segment:
// `encodeURIComponent` passes `.`/`..`, and `fetch()` resolves `/nodes/../deprovision` to the
// account-level deprovision, which wipes the pinned node's credentials and deletes the row. `POST
// /api/nodes` allows `..` as a name, so throwing is the right failure.
const SAFE_PATH_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._-]*$/

function deprovisionPath(accountId: string, nodeId: string): string {
  if (nodeId === '.' || nodeId === '..' || !SAFE_PATH_SEGMENT.test(nodeId)) {
    throw new Error(`refusing to deprovision: ${JSON.stringify(nodeId)} cannot be a node path segment`)
  }
  return `/api/accounts/${encodeURIComponent(accountId)}/nodes/${encodeURIComponent(nodeId)}/deprovision`
}

/* The account Delete's question. A login account goes through `POST /api/accounts/{id}/remove`: its sessions
 * stop and its login leaves every node and the shared folder; a node that cannot do it now does it when it next
 * connects, so nothing is reported back. An API-key account has nothing on a node: `DELETE /api/accounts/{id}`. */
function deleteQuestion(account: ClaudeAccount, name: string, isApiKey: boolean): string {
  if (isApiKey) return `Delete ${name}? Its stored API key is deleted with it.`
  const sessions = account.sessions?.total ?? 0
  return `Delete ${name}? Its login is removed from every node and the shared folder.`
    + (sessions > 0 ? ` Its ${sessions} running session(s) are stopped.` : '')
}

/** The per-node removal's question; it says what survives, since with several nodes this is not the whole
 *  account. */
function removeNodeQuestion(nodeLabel: string, nodeCount: number): string {
  return `Remove this account's credentials from ${nodeLabel}?`
    + (nodeCount > 1
      ? ` The other ${nodeCount - 1} node(s) keep theirs, and the account stays.`
      : ' It is the only node holding this account, so the roster row goes too.')
}



/* Why Edit cannot be pressed (`null` = live): no roster row (`NO_ROW_TITLE`) or unreadable fields
 * (`NO_RECORD_TITLE`). The sheet item carries it via `OverflowItem`'s `disabled` + `disabledReason`, and the
 * handler refuses too. */
function editBlockedReason(accountId: string | null | undefined, record: Account | undefined): string | null {
  if (accountId == null) return NO_ROW_TITLE
  if (!record) return NO_RECORD_TITLE
  return null
}

/** `authRejectedReason` for a credential the node could not read at all — a login, not a rejection. */
const UNREADABLE_REASON = 'unreadable'

// What a rejected node offers. `auth_rejected` has three causes needing three fixes; only a re-login is a
// button (a login cannot fix an unpaid bill or suspended seat). An unknown/absent reason (older nodes) keeps
// the generic wording and stays clickable.
const REJECTION: Record<string, { label: string; hint: string }> = {
  billing: { label: 'Billing', hint: 'Payment required (402) — a re-login will not fix this' },
  seat: { label: 'Seat', hint: 'Seat suspended or forbidden (403) — needs an admin, not a re-login' },
}

/* The state slot for a viewer who may not log in, shared by all three login-offering arms. The verb is
removed (a permission), but the slot is not left empty: the node's state is also drawn by the disc
(`NODE_STATE_MARK`, `ui/feed/FeedList`) and `data-node-state`, and an empty slot would assert an absence.
So it reverts to the word, as `no_reading`, `ok` and `RejectedNode`'s billing/seat arms do. Not a
disabled `Link`: a disabled verb promises a change, and a missing key is not a state. */
function noLoginPermissionNode(word: string, fact: string, reason: string) {
  return (
    <Typography
      variant="caption"
      sx={{ color: 'text.secondary' }}
      title={`${fact} — ${reason}`}
    >
      {word}
    </Typography>
  )
}

function RejectedNode({ nodeId, nodeLabel, quota, canLogIn, noLoginReason, onRelogin }: {
  /** The uuid the re-login addresses; `nodeLabel` is what the words show. */
  nodeId: string
  nodeLabel: string
  quota: QuotaData | null
  /** Whether this viewer may dispatch `account_reauth` (`LOGIN_PERMISSION`) for this account. */
  canLogIn: boolean
  /** Why not, on the word's hover (`loginRefusal`). */
  noLoginReason: string
  onRelogin: (nodeId: string) => void
}) {
  const stated = REJECTION[quota?.authRejectedReason ?? '']
  if (stated) {
    // Not a button: there is no action we can take for them.
    return (
      <Typography variant="caption" sx={{ color: 'warning.main' }} title={`${stated.hint} (${nodeLabel})`}>
        {stated.label}
      </Typography>
    )
  }
  /* No key, no button: `needs login` is the state (see `noLoginPermissionNode`), in `warning.main`. */
  if (!canLogIn) {
    return (
      <Typography
        variant="caption"
        sx={{ color: 'warning.main' }}
        title={`OAuth token rejected on ${nodeLabel} — ${noLoginReason}`}
      >
        needs login
      </Typography>
    )
  }
  return (
    <Link
      component="button"
      type="button"
      variant="caption"
      underline="hover"
      onClick={() => onRelogin(nodeId)}
      title={`OAuth token rejected on ${nodeLabel} — log this node in again`}
    >
      Login
    </Link>
  )
}

// Does this account need me, in one word, or null. Healthy accounts are fully described by their 5h/7d/
// session columns; only what those cannot express (a node needing a login) survives. Derived from the same
// per-node records as the expansion.
/** Does any node hold a session for this account? `acct.status` cannot say: the fold keeps one node's `status`,
 *  and drops a null in favour of a sibling's `no_credentials` (`account_fold.py`), so a single new node's
 *  `no_credentials` would mark an account that is `ok` on every other node. */
export function credentialsNowhere(acct: ClaudeAccount, nodes: AccountNode[]): boolean {
  if (acct.status !== 'no_credentials') return false
  return !nodes.some(n => n.present !== false && n.status !== 'no_credentials')
}

/* A `node_pinned` provider's sign-in stays on the node that ran it: every other node reads `no_credentials`
by design, and while any node still holds the credential the account can run. True in that case — the one
place `summarizeNodes` reads it. */
export function pinnedCredentialHeld(nodes: AccountNode[], nodePinned: boolean): boolean {
  return nodePinned
    && nodes.some(n => n.present !== false && n.status !== 'no_credentials' && !isAuthRejected(n.quota))
}

/** The nodes the ⚠ rollup counts as blocked. A `no_credentials` node needs a login just like a rejection; silence
 *  would read as healthy. Except the other nodes of a pinned account signed in on its own (`pinnedCredentialHeld`):
 *  they keep their own child rows, with their Login, out of this rollup. */
export function blockedNodes(nodes: AccountNode[], nodePinned = false): AccountNode[] {
  const noCredsBlocks = !pinnedCredentialHeld(nodes, nodePinned)
  return nodes.filter(n => isAuthRejected(n.quota) || (noCredsBlocks && n.status === 'no_credentials'))
}

export function summarizeNodes(nodes: AccountNode[], nodePinned = false): string | null {
  const blocked = blockedNodes(nodes, nodePinned)
  if (!blocked.length) return null
  // Name the cause when every blocked node agrees; otherwise the count. Wording from REJECTION.
  const causes = new Set(blocked.map(n => REJECTION[n.quota?.authRejectedReason ?? '']?.label ?? 'Login'))
  return causes.size === 1 ? [...causes][0] : `${blocked.length} blocked`
}

/* The account's health as the row's one mark — the state a runner-launch decision turns on:
 *   ⊘ disabled  — excluded from selection; a choice, not a fault, so grey not red.
 *   ✕ erroring  — needs a login: `auth_rejected`, no credential dir, or a blocked node
 *                 (`summarizeNodes`, the derivation the row's ⚠ marker uses, so they cannot disagree).
 *   ▲ exhausted — authenticated but in `quotaClass`'s top risk tier: it just has to reset.
 *   ● available — enabled, authenticated, with room.
 * Shape first, colour second: the glyphs separate in greyscale, the tones do not. `quotaClass` stays
 * the single home of the THRESHOLDS. */
type AccountHealth = 'disabled' | 'erroring' | 'exhausted' | 'available'

const ACCOUNT_HEALTH_MARK: Record<AccountHealth, { tone: Tone; glyph: string }> = {
  disabled: { tone: 'neutral', glyph: '⊘' },
  erroring: { tone: 'error', glyph: '✕' },
  exhausted: { tone: 'warning', glyph: '▲' },
  available: { tone: 'success', glyph: '●' },
}

function accountHealth(
  acct: ClaudeAccount, enabled: boolean, blocked: string | null, isApiKey = false, noQuota = false,
  noCredentials = acct.status === 'no_credentials',
): AccountHealth {
  if (!enabled) return 'disabled'
  /* An API-key account drops two `erroring` causes and keeps the third:
  • `blocked` — dropped: a rollup of per-node logins, which this kind has none of.
  • `acct.status === 'no_credentials'` — dropped: `routes/portfolio.py` reports that for a broker-held key,
  a red ✕ on a healthy account.
  • `isAuthRejected(acct.quota)` — kept: a revoked key earns `auth_rejected` and the scheduler honours it.
  The fix is a rotate from Edit, not a re-login.
  `exhausted` is unreachable for it (null percents).
  A provider row keeps `blocked`/`no_credentials` (it logs in per node) but must skip the `exhausted`
  test: its vendor publishes no percentage, and ▲ beside "not available for this provider" would contradict
  itself (R5.3). So `noQuota` gates that test only. */
  if (isApiKey) return isAuthRejected(acct.quota) ? 'erroring' : 'available'
  /* `noCredentials` is the page's `credentialsNowhere`, never `acct.status` alone: that is one node's, picked by
  the fold's node order, so one new node's `no_credentials` would red an account serving on four others. */
  if (blocked || isAuthRejected(acct.quota) || noCredentials) return 'erroring'
  if (noQuota) return 'available'
  const high = quotaClass(acct.quota?.fiveHourPercent) === 'quota-high'
    || quotaClass(acct.quota?.weeklyPercent) === 'quota-high'
  return high ? 'exhausted' : 'available'
}

/** The identity region — address (or its no-address chip), greyed when disabled
 *  (R1's "visibly greyed but still VISIBLE"). */
/* `maxWidth: '100%'` makes the ellipsis work: `FeedList`'s `TITLE_SX` ellipsizes only its own text, and an
`inline-flex` child is simply cut mid-character. Exposed once `PROVIDER_SLOT` narrowed the cell; `EMAIL_SX`
puts the truncation on the text. */
/* The identity's parts give way in order when the row is narrow: the email first, then the name down to a
 * floor that still reads; chips (`API key`, `no credentials`) mean nothing clipped, so they keep
 * their width. */
/* The account's no-credentials chip, gone wherever the provider line sits under the name and says it instead. */
const NO_CREDENTIALS_CHIP_CLASS = 'acct-no-credentials-chip'

/* The identity's wrapped shape: the name on its own line, the provider under it, then the chips. */
const IDENTITY_WRAPPED = { flexWrap: 'wrap', rowGap: 0.25, '& > *': { maxWidth: '100%' } } as const
const IDENTITY_SX = {
  display: 'inline-flex', alignItems: 'center', gap: 0.5, minWidth: 0, maxWidth: '100%',
  '& > *': { flexShrink: 0 },
  // Wrapped on a narrow roster (beside an open account) and on a phone.
  /* The no-credentials chip folds into the provider line on a narrow roster and on a phone
  (`NO_CREDENTIALS_UNDER_SX`): wrapped beside the provider, it made a third line. */
  [ACCOUNTS_PROVIDER_UNDER]: { ...IDENTITY_WRAPPED, [`& .${NO_CREDENTIALS_CHIP_CLASS}`]: { display: 'none' } },
  [PHONE]: { ...IDENTITY_WRAPPED, [`& .${NO_CREDENTIALS_CHIP_CLASS}`]: { display: 'none' } },
} as const

/** The provider slot's narrow home (that slot is gated `acctProvider`): under the name, whole. On a narrow roster
 *  the chips share its line (two-line rows, as Secrets' type line); a phone gives it a line of its own. */
const PROVIDER_UNDER = { display: 'block', fontSize: '0.6875rem', color: 'text.secondary' } as const
const PROVIDER_UNDER_SX = {
  display: 'none',
  // Ellipsized, not wrapped, once it carries the folded "no credentials".
  [ACCOUNTS_PROVIDER_UNDER]: { ...PROVIDER_UNDER, ...ELLIPSIS_SX, '&&': { flexShrink: 1, minWidth: 0 } },
  // Whole on a phone, which also matches the narrow band: its ellipsis is undone.
  [PHONE]: { ...PROVIDER_UNDER, flexBasis: '100%', whiteSpace: 'normal', overflow: 'visible', textOverflow: 'clip' },
} as const
/** The no-credentials chip's words, folded onto the provider line wherever that line shows (`PROVIDER_UNDER_SX`):
 *  the row's one "no credentials", a phone's too. */
const NO_CREDENTIALS_UNDER_SX = { display: 'none', [ACCOUNTS_PROVIDER_UNDER]: { display: 'inline' }, [PHONE]: { display: 'inline' } } as const

/** A closed 5h slot's bare dash (`phoneDashGone`): hidden wherever the slot closes. */
const DASH_GONE_SX = { [ACCOUNTS_PROVIDER_UNDER]: { display: 'none' }, [PHONE]: { display: 'none' } } as const

/** An identity carrying chips that must stay whole: they wrap to the next line instead of clipping. */
const IDENTITY_WRAP_SX = { display: 'flex', flexWrap: 'wrap', rowGap: 0.25 } as const

/** The identity's text, the part that shrinks first. Wrapped, a zero basis keeps the chips after it (the plan)
 *  on its line rather than a line of their own; it still never shrinks below a readable floor. */
const EMAIL_WRAPPED = { '&&': { flex: '1 1 0', minWidth: '8em' } } as const
const EMAIL_SX = {
  minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
  /* Flex shrink is weighted by size, so a merely larger factor still clipped a long name beside a long email: this
  one leaves the name whole until the email is down to its floor. */
  '&&': { flexShrink: 10000, minWidth: '3em' },
  [ACCOUNTS_PROVIDER_UNDER]: EMAIL_WRAPPED,
  [PHONE]: EMAIL_WRAPPED,
} as const
const IDENTITY_DISABLED_SX = { opacity: 0.5 } as const
/** The plan tag beside the email: a quiet outlined word, never shrunk (a clipped plan names nothing). */
const PLAN_SX = {
  fontSize: '0.6875rem', color: 'text.secondary', border: 1, borderColor: 'divider', borderRadius: 0.5, px: 0.5,
  lineHeight: 1.5, whiteSpace: 'nowrap',
} as const
/** The account's name, the row's label: first and weighted, it takes its natural width and shrinks only
 *  after the email has, never below a floor that still names the account; the full name is on its `title`. */
const NAME_WRAPPED = {
  flexBasis: '100%', minWidth: 0, maxWidth: '100%',
  display: '-webkit-box', WebkitBoxOrient: 'vertical', WebkitLineClamp: 2,
  whiteSpace: 'normal', overflowWrap: 'anywhere',
} as const
const NAME_SX = {
  minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 600,
  /* The floor is `min()`ed with the name's own width (`--name-ch`, set per row): a min-width wider than a short
  name stretched its box and left a gap before the email. */
  '&&': { flexShrink: 1, minWidth: 'min(6.5em, var(--name-ch, 6.5em))' },
  /* On a phone or a narrow roster it takes the identity's whole first line and wraps to a second, clamped
  there (unclamped, a long name ran to three lines and pushed the row's height around). */
  [ACCOUNTS_PROVIDER_UNDER]: { '&&': NAME_WRAPPED },
  [PHONE]: { '&&': NAME_WRAPPED },
} as const

/** `NAME_SX`'s `--name-ch`: the name's length in `ch`, near its own width, so a short name's floor is the name. */
const nameWidthStyle = (name: string) => ({ '--name-ch': `${name.length}ch` }) as CSSProperties

/** A per-node row's identity: the server icon, then its label (name, chip, host), which alone wraps. */
const NODE_IDENTITY_SX = { display: 'inline-flex', alignItems: 'flex-start', gap: 0.5, minWidth: 0, maxWidth: '100%' } as const
/* Centred on the label's first line (the 20px chip's), wherever the label wraps to. */
const NODE_ICON_SX = { color: 'text.secondary', mt: '2px' } as const
const NODE_LABEL_SX = {
  display: 'inline-flex', alignItems: 'center', gap: 0.5, minWidth: 0, maxWidth: '100%',
  '& > *': { flexShrink: 0, maxWidth: '100%' },
} as const

/** A per-node row's name beside its chips: it shrinks before they do, never below a few characters. */
const NODE_NAME_SX = {
  minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
  '&&': { flexShrink: 1, minWidth: '4em' },
} as const

/* The em-dash for a value not read, matching `MetaRow`/`MetaActor` (which render nodes, not text). */
const NO_ACTOR = '—'

// ── Per-node account state, as the account row's children ──
// A login is per (account, node), so N rows at depth 1. The folded `node_id` (`[1]` of an array_agg
// beside a `bool_or`) cannot express mixed health. These are per-node measurements. No session here, no
// row.

/** Which of eight per-node states. `ok` must be earned (a real quota), never the fall-through: five
 *  accounts with no `.credentials.json` once read "ok". Every other arm names which unknown it is. */
type NodeState =
  'provisioning' | 'syncing' | 'not_synced' | 'missing' | 'no_credentials' | 'unreadable' | 'rejected' | 'no_reading' | 'ok'

/** The node rows worth showing: the nodes that hold the account, and the ones its shared token is on
 *  its way to (`sync`). Any other node it is not on stays out — every node would otherwise list a Login,
 *  which one "Add to node" says once. A node that holds the account always stays: it has a session to read and to
 *  deprovision. */
export function servingNodes(nodes: AccountNode[]): AccountNode[] {
  return nodes.filter(n => n.present !== false || n.sync != null)
}

/** `noQuota` — the parent's verdict — changes one rung: a healthy provider child
 *  (`provider_health.observe` publishes a status and no quota) would otherwise always read `? no reading`
 *  beside cells saying nothing is wrong. The other four rungs are real facts for a provider account.
 *  It skips that rung only on a positive observation, `status: "connected"` (projected by `account_fold`):
 *  a null status "is NOT the same as healthy" (`types.ts`; `account_fold.py`'s note), so a provider
 *  child with no status still reads `? no reading`. */
const NODE_STATUS_CONNECTED = 'connected'

function nodeState(n: AccountNode, noQuota: boolean): NodeState {
  const state = probedNodeState(n, noQuota)
  /* A node still coming up (`provisioning`: new, no successful probe of any account yet) says only that where its
  cell would only be WAITING on a first reading. A cell that needs a login (`no_credentials`, `missing`) keeps
  its Login: a probe can only follow it. */
  return n.provisioning === true && state === 'no_reading' ? 'provisioning' : state
}

function probedNodeState(n: AccountNode, noQuota: boolean): NodeState {
  // Not on this node yet, but shared to it through a token-folder NodeLink: on its way, or overdue.
  if (n.present === false && n.sync) return n.sync
  // Not on this node at all: nothing to read or remove.
  if (n.present === false) return 'missing'
  if (n.status === 'no_credentials') return 'no_credentials'
  // The node read no credential at all (jaahd `collector._REJECTED_UNREADABLE`): nothing was rejected.
  if (isAuthRejected(n.quota) && n.quota?.authRejectedReason === UNREADABLE_REASON) return 'unreadable'
  if (isAuthRejected(n.quota)) return 'rejected'
  if (n.quota == null) return noQuota && n.status === NODE_STATUS_CONNECTED ? 'ok' : 'no_reading'
  return 'ok'
}

/* The disc for each; shape first, colour second.
 *   ◌ syncing — a shared login on its way here · ! not_synced — it did not arrive (the reason hangs below) ·
 *   ○ missing — never added here · ! no_credentials — no dir on this node, a first login is the fix ·
 *   ! unreadable — the node cannot read the credential (where it looked hangs on the hover) ·
 *   ✕ rejected — the token was refused · ? no_reading — reported but unmeasured · ● ok — a real quota. */
const NODE_STATE_MARK: Record<NodeState, { tone: Tone; glyph: string; label: string }> = {
  provisioning: { tone: 'info', glyph: '◐', label: 'provisioning' },
  syncing: { tone: 'info', glyph: '◌', label: 'syncing' },
  not_synced: { tone: 'warning', glyph: '!', label: 'not synced' },
  missing: { tone: 'neutral', glyph: '○', label: 'missing' },
  no_credentials: { tone: 'warning', glyph: '!', label: 'no credentials' },
  unreadable: { tone: 'warning', glyph: '!', label: 'can’t read credential' },
  rejected: { tone: 'error', glyph: '✕', label: 'rejected' },
  no_reading: { tone: 'neutral', glyph: '?', label: 'no reading' },
  ok: { tone: 'success', glyph: '●', label: 'ok' },
}

/** Why a node has `no reading`, keyed by its `no_reading_reason` (jaahd `collector.NO_READING_*`). Each
 *  is retried every probe cycle; the label is what the slot shows, the title what it means. */
const NO_READING_UNKNOWN = { label: 'no reading', title: 'has this account but no quota reading yet' }
const NO_READING_REASON: Record<string, { label: string; title: string }> = {
  refresh_locked: {
    label: 'refresh locked',
    title: 'the sign-in is being refreshed elsewhere (token-refresh lock held); retried each cycle',
  },
  refresh_failed: { label: 'refresh failed', title: 'token refresh failed; retried each cycle' },
  usage_failed: { label: 'usage check failed', title: 'the provider did not return the quota; retried each cycle' },
}

/** The state slot. `missing`, `rejected` and `no_credentials` carry the login that fixes them, in the slot
 *  as well as the ⋯. */
/** A node row's display name: its `node_name`, or the uuid only for a node that no longer exists. */
function nodeName(n: AccountNode): string {
  return n.node_name ?? n.node_id
}

function nodeStateNode(
  n: AccountNode,
  state: NodeState,
  /** `LOGIN_PERMISSION`, threaded from the page like `noQuota`/`providerLabel`, so a child cannot disagree
   *  with its row. */
  canLogIn: boolean,
  onRelogin: (nodeId: string) => void,
  onEnrol: (nodeId: string) => void,
  /** Why `canLogIn` is false, on the word's hover (`loginRefusal`). */
  noLoginReason: string,
) {
  if (state === 'missing') {
    /* The action, not the word: the same login `no_credentials` offers (`onEnrol`, mode="add" —
    `_account_reauth` refuses a missing dir), spelled identically; the ⋯ item matches. The state still shows
    in the disc's `○`/`missing` and `data-node-state="missing"`; the hover separates it from
    `no_credentials`. */
    /* …and the word is what a viewer without the key gets back. */
    if (!canLogIn) {
      return noLoginPermissionNode('missing', `This account is not logged in on ${nodeName(n)}`, noLoginReason)
    }
    return (
      <Link
        component="button"
        type="button"
        variant="caption"
        underline="hover"
        onClick={() => onEnrol(n.node_id)}
        title={`This account is not logged in on ${nodeName(n)} — log it in there`}
      >
        Login
      </Link>
    )
  }
  if (state === 'syncing') {
    /* A chip, no spinner: it ends on its own after two probe intervals (`app/account_sharing.py`). */
    return (
      <StatusChip
        label="Syncing…"
        tone="info"
        data-status="syncing"
        title={`Arrives on ${nodeName(n)} at its next check`}
      />
    )
  }
  if (state === 'not_synced') {
    return (
      <StatusChip
        label="Not synced"
        tone="warning"
        data-status="not_synced"
        title={`${notSyncedText(n)} (${nodeName(n)})`}
      />
    )
  }
  if (state === 'provisioning') {
    return (
      <Typography
        variant="caption"
        data-testid="acct-node-provisioning"
        sx={{ color: 'text.secondary' }}
        title={`${nodeName(n)} is still being set up: it has not read any account yet. This shows its real state `
          + 'after its first successful check. Nothing to do here.'}
      >
        provisioning
      </Typography>
    )
  }
  if (state === 'no_credentials') {
    /* Not `auth_rejected`: no session on this node. A first login must go through `onEnrol` (mode="add"):
    `_account_reauth` refuses to create a dir (`account_not_found`). Labelled 'Login', not 'Add'. */
    /* `needs login` without the key, as `rejected` gets: `summarizeNodes` collapses the two. The disc keeps
    `no credentials`. */
    if (!canLogIn) {
      return noLoginPermissionNode('needs login', `No OAuth session in this account's dir on ${nodeName(n)}`, noLoginReason)
    }
    return (
      <Link
        component="button"
        type="button"
        variant="caption"
        underline="hover"
        onClick={() => onEnrol(n.node_id)}
        title={`No OAuth session in this account's dir on ${nodeName(n)} — log this node in`}
      >
        Login
      </Link>
    )
  }
  if (state === 'unreadable') {
    const fact = `${nodeName(n)} found no credential it can read for this account`
      + (n.quota?.credentialPath ? ` (looked in ${n.quota.credentialPath})` : '')
    if (!canLogIn) return noLoginPermissionNode('can’t read', fact, noLoginReason)
    return (
      <Link
        component="button"
        type="button"
        variant="caption"
        underline="hover"
        onClick={() => onRelogin(n.node_id)}
        title={`${fact} — log this node in again`}
      >
        Login
      </Link>
    )
  }
  if (state === 'rejected') {
    return (
      <RejectedNode
        nodeId={n.node_id} nodeLabel={nodeName(n)} quota={n.quota} canLogIn={canLogIn} noLoginReason={noLoginReason}
        onRelogin={onRelogin}
      />
    )
  }
  if (state === 'no_reading') {
    // Reported with no reading; distinct from missing and ok. The node's reason, when it gave one, names
    // which — an unknown reason falls back to the bare words rather than guessing.
    const why = NO_READING_REASON[n.no_reading_reason ?? ''] ?? NO_READING_UNKNOWN
    return (
      <Typography
        variant="caption"
        data-no-reading-reason={n.no_reading_reason ?? undefined}
        sx={{ color: 'text.secondary' }}
        title={`${nodeName(n)}: ${why.title} — unverified, not known-good`}
      >
        {why.label}
      </Typography>
    )
  }
  return (
    <Typography variant="caption" sx={{ color: 'text.secondary' }} title={`Usable on ${nodeName(n)}`}>
      ok
    </Typography>
  )
}

/* An open account's lines under its row, at its children's indent: the feed's `detail` region. */
const DETAIL_LINE_SX = [rowPadLeft(1), { pb: 0.5, fontSize: '0.75rem', color: 'text.secondary' }] as const

function AccountDetailLines({ lines }: { lines: { key: string; testId: string; text: ReactNode }[] }) {
  if (lines.length === 0) return null
  return (
    <>
      {lines.map(line => <Box key={line.key} data-testid={line.testId} sx={DETAIL_LINE_SX}>{line.text}</Box>)}
    </>
  )
}

/** A `not_synced` node's words, with the reason the node gave (`sync_reason`) when there is one. */
function notSyncedText(n: AccountNode): string {
  return n.sync_reason ? `Not synced — ${n.sync_reason}` : 'Not synced'
}

/* Where a node reads the account's token (`token_source`): a fact beside its name, not a state. Only the exception
is said: `shared` is what the account row's NodeLink mark already tells, so only `local` gets a chip. An
older node that does not say gets none — never a guess. */
const LOCAL_SOURCE_CHIP: { label: string; tone: Tone } = { label: 'Local', tone: 'neutral' }

/* Material's `Dns` (a server) and `FolderShared` paths, inlined: `@mui/icons-material` is not a dependency
(`ui/InfoTip`). The server marks a node row as a node; the folder is a NodeLink. */
const SERVER_PATH =
  'M20 13H4c-.55 0-1 .45-1 1v6c0 .55.45 1 1 1h16c.55 0 1-.45 1-1v-6c0-.55-.45-1-1-1M7 19c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2M20 3H4c-.55 0-1 .45-1 1v6c0 .55.45 1 1 1h16c.55 0 1-.45 1-1V4c0-.55-.45-1-1-1M7 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2'
const NODELINK_PATH =
  'M20 6h-8l-2-2H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2m-5 3c1.1 0 2 .9 2 2s-.9 2-2 2-2-.9-2-2 .9-2 2-2m4 8h-8v-1c0-1.33 2.67-2 4-2s4 .67 4 2v1z'

function GlyphIcon({ path, sx }: { path: string; sx?: SxProps<Theme> }) {
  return (
    <Icon viewBox="0 0 24 24" sx={[{ fill: 'currentColor' }, ...(Array.isArray(sx) ? sx : [sx])]}>
      <path d={path} />
    </Icon>
  )
}

/* The token-folder NodeLinks a node reads this account from, one folder mark each, to that NodeLink.
`titleAside`, outside the row's link, so the press is the mark's own. */
function SharedViaLinks({ links, onOpen }: { links: { id: string; name: string }[]; onOpen: (id: string) => void }) {
  return (
    <Box component="span" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.25, ml: 0.5, [PHONE]: { display: 'none' } }}>
      {links.map(link => (
        <Box
          key={link.id}
          component="button"
          type="button"
          data-testid="acct-shared-via"
          aria-label={`Open NodeLink ${link.name}`}
          title={`Shared via NodeLink ${link.name}`}
          onClick={() => onOpen(link.id)}
          sx={{
            display: 'inline-flex', alignItems: 'center', p: '2px', border: 0, borderRadius: 1, cursor: 'pointer',
            background: 'none', color: 'info.main', fontSize: '1rem',
            '&:hover': { bgcolor: 'action.hover' },
            '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main' },
          }}
        >
          <GlyphIcon path={NODELINK_PATH} />
        </Box>
      ))}
    </Box>
  )
}

/* `SharedViaLinks`' phone home: there the wrapped identity pushed the aside to the row's far edge, so the mark
follows the name instead. Inside the row's link, so a mark, not a button; each NodeLink is on its own page. */
function SharedViaMark({ links }: { links: { id: string; name: string }[] }) {
  return (
    <Box
      component="span"
      data-testid="acct-shared-via-phone"
      role="img"
      aria-label={`Shared via NodeLink ${links.map(link => link.name).join(', ')}`}
      sx={{ display: 'none', [PHONE]: { display: 'inline-flex', verticalAlign: '-0.125em', ml: 0.5, color: 'info.main' } }}
    >
      <GlyphIcon path={NODELINK_PATH} />
    </Box>
  )
}

/** The account's per-node rows, at depth 1. */
function accountNodeRows({
  nodes, accountId, busy, noQuota, canLogIn, noLoginReason, canDeprovision, onRelogin, onEnrol, onRemove,
}: {
  nodes: AccountNode[]
  /** Null when the read-time join found no roster row: the per-node remove says `NO_ROW_TITLE`. A uuid
   *  string, carried, never parsed (see `deprovisionPath`). */
  accountId: string | null | undefined
  busy: boolean
  /** The parent's no-quota verdict (see `nodeState`). */
  noQuota: boolean
  /* The two write gates, resolved by the page (two routes, two answers — see `LOGIN_PERMISSION`). */
  /** May this viewer dispatch a login (`account_add` on a `missing` node, `account_reauth` on a dead
   *  session)? `LOGIN_PERMISSION`, and the account the viewer's own org's (`loginRefusal`). */
  canLogIn: boolean
  /** Why not, on a node state's hover. */
  noLoginReason: string
  /** May this viewer deprovision one node? `POST /api/accounts/{id}/nodes/{node_id}/deprovision` is
   *  `require_account_writer`, so the page passes its org-admin verdict. */
  canDeprovision: boolean
  /** Re-login pre-targeted at this row's node. */
  onRelogin: (nodeId: string) => void
  /** Log the account IN on a node it is missing from. */
  onEnrol: (nodeId: string) => void
  /** `nodeId` is the uuid the route takes; `nodeLabel` words the question. */
  onRemove: (nodeId: string, nodeLabel: string) => void
}): FeedRowSpec[] {
  // Short name only when unambiguous (`box-1.tailnet-a.network` vs `box-1.tail0000.example`).
  const ambiguous = (full: string) => nodes.filter(n => shortNode(nodeName(n)) === shortNode(full)).length > 1
  return nodes.map((n): FeedRowSpec => {
    const state = nodeState(n, noQuota)
    const mark = NODE_STATE_MARK[state]
    const name = ambiguous(nodeName(n)) ? nodeName(n) : shortNode(nodeName(n))
    /* Which host a co-located node runs on; a host itself never reaches here (`workRows`). */
    const where = n.hosting === 'co_located' && n.hosted_on ? `on ${shortNode(n.hosted_on)}` : null
    // Only a node that holds the account reads it from somewhere; a syncing one says that instead.
    const source = n.present !== false && n.token_source === 'local' ? LOCAL_SOURCE_CHIP : null
    return {
      key: n.node_id,
      depth: 1,
      indicator: { tone: mark.tone, glyph: mark.glyph, label: mark.label },
      title: (
        <Box component="span" sx={NODE_IDENTITY_SX}>
          <GlyphIcon path={SERVER_PATH} sx={NODE_ICON_SX} />
          {/* The icon stays outside the wrap, so a wrapped `on <host>` starts under the name, not left of the icon. */}
          <Box component="span" data-testid="acct-node-label" sx={[NODE_LABEL_SX, source || where ? IDENTITY_WRAP_SX : null]}>
            <Box component="span" sx={NODE_NAME_SX}>{name}</Box>
            {source && (
              <StatusChip label={source.label} tone={source.tone} data-status={n.token_source ?? undefined} />
            )}
            {where && <Box component="span" data-testid="acct-node-where" sx={EMAIL_SX}>{where}</Box>}
          </Box>
        </Box>
      ),
      titleText: [nodeName(n), where && `(${where})`, source?.label].filter(Boolean).join(' '),
      // The chip and the host wrap under the name rather than being clipped on a phone.
      titleLines: source || where ? 'all' : undefined,
      busy,
      data: { 'node-state': state, hosting: n.hosting ?? undefined, 'token-source': n.token_source ?? undefined },
      /* The reason on the row's hover: a phone's state slot holds the chip, never the sentence. */
      notes: state === 'not_synced' ? [{ key: 'sync', tone: 'muted', text: notSyncedText(n) }] : undefined,
      /* The state only: the quota and the vendor are the account row's, one reading for all its nodes. */
      /* The state stands in the Provider column, its own: two blanks hold the 5h and 7d columns beside it, so a word
      never sits under a quota header. On a phone (5h the only column) the blanks close and the state keeps its width. */
      slots: [
        {
          key: 'state', width: PROVIDER_SLOT, phoneWidth: NODE_STATE_SLOT, align: 'left',
          narrowWidth: { query: ACCOUNTS_PROVIDER_UNDER, width: NODE_STATE_SLOT },
          node: nodeStateNode(n, state, canLogIn, onRelogin, onEnrol, noLoginReason),
        },
        /* Closed wherever the account row's Provider column is (the phone, a narrow roster): the state then stands
        at the row's end, as before. */
        { key: 'five', width: QUOTA_SLOT, phoneWidth: 0, narrowWidth: { query: ACCOUNTS_PROVIDER_UNDER, width: 0 }, node: null },
        { key: 'week', width: QUOTA_SLOT, gate: 'acctWeek', node: null },
      ],
      /* The verb in the ⋯, not an icon button: `theme.ts` raises `MuiIconButton` to 44x44 below `sm`. A
      `missing` node offers the way in, spelled as its state slot's link. */
      /* ── Withheld outright from a viewer the route would refuse ──
      Absent, not disabled: each item is only a verb (contrast `components/NodeEnableToggle.tsx`; see
      `noLoginPermissionNode`). The list goes empty and `ui/FeedList` drops the ⋯ (as `Accounts.test.tsx`'s
      non-superadmin case asserts for the account line). `disabled: accountId == null` stays a state. */
      actions: state === 'syncing' ? []
        : state === 'missing' || state === 'not_synced'
        ? (canLogIn ? [{
            key: 'add',
            icon: '+',
            label: 'Login',
            ariaLabel: `Login on ${nodeName(n)}`,
            onClick: () => onEnrol(n.node_id),
          }] : [])
        : (canDeprovision ? [{
            key: 'deprovision',
            icon: '−',
            label: 'Deprovision',
            // Names which node; `RowActionsOverflow` appends the disabled reason.
            ariaLabel: `Deprovision on ${nodeName(n)}`,
            danger: true,
            disabled: accountId == null,
            disabledReason: NO_ROW_TITLE,
            onClick: () => { if (accountId != null) onRemove(n.node_id, nodeName(n)) },
          }] : []),
    }
  })
}

// The account auth wizard, shared by two flows (`mode`):
//   • 'add'    — slug input → account_add → poll → awaiting_code → account_add_submit_code (finalize).
//   • 'reauth' — fixed slug, a start step (node) → account_reauth → poll → awaiting_code → finish; no
//                create/enable side effects.
// Both poll /api/accounts/provisioning. How it finishes is `loginNeedsCode`'s: a paste-code provider
// (Claude included) gets a code field; an await-credentials provider (device auth) finishes on the node,
// so the wizard waits and drives `*_await_login`.
// The handoff is sealed (`lib/loginSeal.ts`): phase 1 sends this page's `peer_key`, the node
// answers a `sealed_url` only this page can open plus its own `seal_key`, and the pasted code goes back as
// `sealed_code`. The control plane relays ciphertext and never holds the URL or the code. It never
// renders a token or credential material, only the authorization URL it unsealed.






export default function Accounts() {
  const { admin } = useAuth()
  /* ── The two write gates this page reads; neither is the route's boundary ──
   * Presentation only (`components/RequireFeature.tsx`); every refusal is also enforced server-side.
   * `isOrgAdmin` — the DB writes: `POST /api/accounts` (API-key create), `PUT`/`DELETE /api/accounts/{id}`
   * and both deprovision routes pair a key with `require_account_writer` (`org_admin_writer`,
   * `app/routes/accounts.py`); `useCan` is allow-all for an org admin, so the verdict is the whole answer.
   * `canLogIn` — every OAuth login verb, a different audience (see `LOGIN_PERMISSION`): a non-admin
   * with `system:create` can log an account in but cannot create an API-key account. */
  const isOrgAdmin = isOrgAdminSeat(admin)
  const canLogIn = useCanLogIn()
  // The community edition has no NodeLink to share subscriptions over.
  const shareShown = useShareSubscriptionsShown() && isEnabled('nodelink')
  /* An open account beside the roster narrows it (`components/ListDetail.tsx`): the header goes compact. */
  const besideDetail = useOpenDetailPath() !== null
  /* A pending org may not add an AI account (the API 403s it): disabled with the reason, never hidden. */
  const addBlocked = useAwaitingApprovalReason()
  // The page's one confirm, the app's shared modal.
  const [confirm, confirmDialog] = useConfirm()
  // A move invalidates every query, the two rosters included.
  const changeOrg = useChangeOrgAction('account')
  const orgVisible = useOrgVisible()
  /* An operator sees every org's accounts and edits another org's on its behalf: the account page's writes, the
  toggle and Delete go as that org (`actAsOnce`; the page's `lib/actAs.ts::useActAsRows`). */
  const otherOrgOf = useOtherOrgOf()
  const queryClient = useQueryClient()
  /* Every mount reads through REST (spec R3; `refetchOnMount: 'always'`, since the edit page writes before it
  navigates back here): cached rows paint meanwhile. */
  const resQuery = useQuery<ClaudeResources>({ queryKey: RESOURCES_KEY, queryFn: getResources, refetchOnMount: 'always' })
  const res = resQuery.data ?? null
  const loading = resQuery.isPending
  /* A seat the route admits (`accounts:view`) may still lack the roster's own key (`system:view`): a 403 is a
  refusal, said in words as the page's empty state, never a load failure. */
  const refused = resQuery.error instanceof ApiError && resQuery.error.status === 403
  const error = resQuery.isError && !refused ? 'Failed to load accounts' : ''
  // Kept apart from `error`: the SSE-driven refetch clears `error` on success, which would wipe a write
  // failure within a tick. `busyRows`: rows with a write in flight; the feed's `busy` stops a second press.
  const { busy: busyRows, withBusy, run, error: writeError, setError: setWriteError } = useRowActions<string>()
  // The form page (New / Edit / Login) is its own route (`AccountEditPage.tsx`); the roster only links there.
  const navigate = useNavigate()
  /* Expanded rows, at every width: the per-node child rows are the only view of where an account works. */
  const [expandedSlugs, setExpandedSlugs] = useState<Set<string>>(new Set())
  function toggleExpanded(slug: string) {
    setExpandedSlugs(prev => toggled(prev, slug))
  }

  // account_ids just toggled, overlaid until /api/resources catches up. Keyed on `account_id`, not
  // `config_dir` (see `StateToggle`).
  const [optimistic, setOptimistic] = useState<Record<string, boolean>>({})

  /* The second roster: the table renders `GET /api/resources`; `name`, `description` and provenance live
   * only on `GET /api/accounts`. The first is `system:view`, the second `accounts:view`, each over a
   * grant-scoped select (`authz.readable_account_select`), so they cover the same accounts. Held by row id, joined by `account_id`.
   * The list, fetched with the roster, rather than a per-row GET on open: it settles before render, so a
   * session without records gets a disabled control with `NO_RECORD_TITLE`, and the expanded detail rows
   * need no N requests.
   * `null` = unavailable (failed or not yet fetched): both answer "is there a record to edit" the same way.
   */
  // Attempted for every session: `GET /api/accounts` is `accounts:view`, the key `App.tsx`
  // already gates this route on. The name's hover and the expansion need it for every role; the Edit verb
  // stays org-admin-only.
  const recordsQuery = useQuery<Account[]>({ queryKey: ACCOUNTS_KEY, queryFn: listAccounts, refetchOnMount: 'always' })
  // Best-effort like `fetchNodes`: it only adds the edit control and detail labels, so a failure never touches
  // `error`, and fails closed (`null`) even over rows an earlier read returned.
  const records = useMemo<Record<string, Account> | null>(
    () => (recordsQuery.isError || !recordsQuery.data
      ? null
      : Object.fromEntries(recordsQuery.data.map(row => [row.id, row]))),
    [recordsQuery.data, recordsQuery.isError],
  )
  const fetchResources = () => queryClient.invalidateQueries({ queryKey: RESOURCES_KEY })
  const fetchAccountRecords = () => queryClient.invalidateQueries({ queryKey: ACCOUNTS_KEY })

  /* Write each pending login's Auto-assign/cap/description once its row is on `/api/accounts`, then re-read so the
  roster shows it ("explicit only"). Taken off the map before the write, so a refetch cannot send it twice;
  a failure goes to the page banner, since the account would otherwise run work the box said it would not. */
  useEffect(() => {
    if (!records) return
    for (const { slug, row, wanted } of takePendingDispatch(records)) {
      const body = dispatchBody(row, wanted)
      if (!body) continue
      put(`/api/accounts/${row.id}`, body)
        .then(() => fetchAccountRecords())
        .catch(err => setWriteError(pendingDispatchFailure(row.name || slug, apiDetail(err, 'the write failed'))))
    }
  }, [records])

  /* The provider catalogue at page level via the wizard's hook (a separate GET; affordable: the response is
  static per `app/agent_providers.py`, best-effort, unread = empty list). The roster reads only
  `has_quota_model`. Not on the live-refetch path: an `account` event cannot change a vendor. */
  const providerCatalogue = useAgentProviders()

  /* Drop overrides the server now agrees with, on every fresh read, keyed on `account_id` like the overlay
  (this pass rebuilds the map, so a different key would drop every entry the toggle wrote). */
  useEffect(() => {
    if (!resQuery.data) return
    const accounts = resQuery.data.accounts
    setOptimistic(prev => {
      const next: Record<string, boolean> = {}
      for (const a of accounts) {
        const key = a.account_id
        if (key && prev[key] !== undefined && prev[key] !== a.enabled) {
          next[key] = prev[key]
        }
      }
      return next
    })
  }, [resQuery.dataUpdatedAt])

  // Scoped to `account`: this page's rows are the only thing such an event can change.
  // Both rosters: `emit_console_event(entity="account")` fires on writes that change either, including
  // another operator's relabel. Stream down -> the old fixed-interval poll of the resources alone.
  useLiveRefetch(
    useEventStream('/api/stream', ['account']),
    () => { void fetchResources(); void fetchAccountRecords() },
    30000,
    { poll: () => void fetchResources() },
  )

  // Keyed on `account_id` (see `StateToggle`).
  function applyOptimistic(accountId: string, enabled: boolean) {
    setOptimistic(prev => ({ ...prev, [accountId]: enabled }))
  }

  // Effective (optimistic-overlaid) enabled state for an account.
  function effEnabled(acct: ClaudeAccount): boolean {
    const key = acct.account_id
    if (key && optimistic[key] !== undefined) return optimistic[key]
    return acct.enabled
  }

  const accounts = res?.accounts ?? []

  const enabledCount = accounts.filter(effEnabled).length

  /* The write runs inside the confirm: a refusal stays in the dialog with its reason. Another org's row
  goes as that org for this one request (`actAsOnce`), as a task's Delete does. */
  async function confirmAndDelete(
    rowKey: string, account: ClaudeAccount, name: string, isApiKey: boolean, otherOrg: OrgRef | null,
  ) {
    const accountId = account.account_id
    if (accountId == null) return
    const asOrg = actAsOnce(otherOrg)
    const id = encodeURIComponent(accountId)
    const ok = await confirm({
      title: 'Delete account',
      message: deleteQuestion(account, name, isApiKey),
      confirmLabel: 'Delete',
      destructive: true,
      run: {
        action: () => withBusy(rowKey, async () => {
          if (isApiKey) {
            await (asOrg ? del(`/api/accounts/${id}`, asOrg) : del(`/api/accounts/${id}`))
            return
          }
          await (asOrg ? post(`/api/accounts/${id}/remove`, undefined, asOrg) : post(`/api/accounts/${id}/remove`))
        }),
        failed: 'Failed to delete the account',
        busyLabel: 'Deleting…',
      },
    })
    if (!ok) return
    await fetchResources()
  }

  async function confirmAndRemoveNode(
    rowKey: string, accountId: string | null | undefined, nodeId: string, nodeLabel: string, nodeCount: number,
  ) {
    if (accountId == null) return
    const ok = await confirm({
      title: 'Remove credentials from node',
      message: removeNodeQuestion(nodeLabel, nodeCount),
      confirmLabel: 'Remove',
      destructive: true,
    })
    if (!ok) return
    await withBusy(rowKey, async () => {
      setWriteError('')
      try {
        await post(deprovisionPath(accountId, nodeId))
        await fetchResources()
      } catch (err) {
        /* The route deletes nothing on a host failure (502); surface it. `deprovisionPath`'s refusal is not an
        `ApiError`, so `apiDetail` would misreport it as a host failure. */
        setWriteError(
          err instanceof ApiError || !(err instanceof Error)
            ? apiDetail(err, `Failed to deprovision on ${nodeLabel}`)
            : err.message,
        )
      }
    })
  }

  /* The roster as feed rows: the mark is the state (`ACCOUNT_HEALTH_MARK`); the account's name leads the
  title (the slug is the edit page's, never a row label); the nodes table is the children; an open row's absences
  are its detail; every verb survives (the toggle inline and in the ⋯, sharing one write; the rest in the ⋯). */
  /* Per row, which slots show only the em-dash (no reading, no provider, no sessions). Beside an open record the
  roster narrows, and a column that is a dash on EVERY row gives up its width before the name truncates. */
  const dashSlots: Set<string>[] = []
  /* Per row, the quota windows its columns read (null: no quota to show), for the header (`quotaColumnWindows`). */
  const rowWindows: (string[] | null)[] = []
  const builtRows: FeedRowSpec[] = accounts.map((acct, i): FeedRowSpec => {
    const enabled = effEnabled(acct)
    /* Said on the Disable control, not as a row marker: `enabledCount` counts the org-narrowed roster, so
    `=== 1` is the steady state of an org with one account, and a mark would sit on it forever. */
    const lastEnabledWarning = enabled && enabledCount === 1
      ? 'Disabling this leaves no account eligible for new runner launches' : undefined
    const displayAcct = { ...acct, enabled }
    const slug = slugOf(acct)
    // `config_dir` is not unique (only `slug` is), so key on account_id first.
    const rowKey = String(acct.account_id ?? acct.config_dir ?? acct.email ?? i)
    const isOpen = expandedSlugs.has(rowKey)
    // The joined `/api/accounts` row: the only source of `name`, `description`, provenance and auth kind.
    // `undefined` fails closed.
    const record = acct.account_id != null ? records?.[acct.account_id] : undefined
    /* Read once: many decisions turn on it. Named `isApiKey`, never `apiKey`, which would be the secret. */
    const isApiKey = isApiKeyAccount(record)
    /* Whether the quota slots show a reading — separate from `isApiKey`, which also empties node children,
    the ⚠ rollup and Deprovision. A provider account has all of those and lacks only a quota; widening
    `isApiKey` would blank its node rows (PRD R5.3). This file names no vendor. */
    const accountHasNoQuota = acct.quota_model === 'none'
    const noQuota = isApiKey || providerHasNoQuota(record, providerCatalogue.rows) || accountHasNoQuota
    const noQuotaTitle = isApiKey ? NO_QUOTA_TITLE
      : providerHasNoQuota(record, providerCatalogue.rows) ? PROVIDER_NO_QUOTA_TITLE : ACCOUNT_NO_QUOTA_TITLE
    /* The windows this vendor's reading covers; with one, the weekly slot is "no such window". */
    const quotaWindows = providerQuotaWindows(record, providerCatalogue.rows)
    const noWeekly = !noQuota && quotaWindows.length < 2
    const noShort = !noQuota && acct.quota?.fiveHourPercent == null && acct.quota?.weeklyPercent != null
    rowWindows.push(noQuota ? null : quotaWindows)
    /* Resolved once for the row and its children, so they cannot disagree. */
    /* A row the records answer came back without is someone else's (`othersAccountCell`), not an unread one — unless
    it is another org's account shared with the operator's filtered orgs, which names its own org and provider. */
    const sharedFromOrg = record === undefined && acct.owner == null && acct.tenant_org != null
    const provider = records !== null && record === undefined && !sharedFromOrg && (acct.account_id != null || acct.owner != null)
      ? othersAccountCell(acct)
      : providerCell(record ?? (acct.provider ? { provider: acct.provider } : undefined), providerCatalogue)
    /* No node rows for an API-key account, whatever the projection sends: every node verb would be a request
    with nothing to run. Emptied here, the one point children, detail and Deprovision read. */
    const nodeRows = isApiKey ? [] : (acct.nodes ?? [])
    /* The ⚠ rollup reads `nodeRows`, never `acct.nodes`: `account_fold.account_node_matrix` emits
    `{present: false}` entries for an API-key slug, which would mark a healthy account "Needs attention:
    Login". Declared after the kind is known. */
    const nodePinned = providerCatalogue.rows.find(p => p.value === record?.provider)?.node_pinned === true
    /* The nodes that run work: a `host` node runs none, so its missing login blocks nothing and it is no child row.
    The health rollup reads these too. */
    const workRows = nodeRows.filter(n => n.hosting !== 'host')
    /* The row's org: the joined record's, else the roster row's own (a shared account has no record). `undefined`
    when neither says, so no slot is drawn rather than a false "No org". */
    const rowOrg = record !== undefined ? record.org : (acct.tenant_org ?? undefined)
    /* Another org's row (operator only): the toggle and Delete go as that org (`actAsOnce`), as the account page's
    writes do. */
    const otherOrg = otherOrgOf(rowOrg)
    /* Its logins are not offered: the login page reads and dispatches as the viewer's org, so a login
    there is the whole tab's act-as (the ⋯'s Act as). `loginRefusal` words why, on each state the verbs leave. */
    const rowCanLogIn = canLogIn && otherOrg === null
    const noLoginReason = loginRefusal(canLogIn, otherOrg)
    /* The children: only the work nodes that hold it (`servingNodes`). */
    const servingRows = servingNodes(workRows)
    /* A node still provisioning asks a login of no one. */
    const askingRows = workRows.filter(n => nodeState(n, noQuota) !== 'provisioning')
    const summary = summarizeNodes(askingRows, nodePinned)
    /* The ⚠ names the nodes, in its tooltip and accessible name, so a row signed in on two of three says which one without
    being expanded. */
    const blockedOn = blockedNodes(askingRows, nodePinned).map(nodeName)
    /* The account-level "no credentials": only when no node holds a session (`credentialsNowhere`). */
    const noCredentials = !isApiKey && credentialsNowhere(acct, workRows)
    /* Nodes it could still be logged in on that no row offers. Never a host: it runs no work. */
    const addable = rowCanLogIn && !!slug
      && workRows.some(n => n.present === false && !servingRows.includes(n))
    /* An account signed in nowhere: the one next step is a first login, on a node whose dir holds no session (as its
    node row's Login), else on one it is missing from (`addable`'s picker, when it stands in for Add to node). */
    const signInNode = noCredentials
      ? workRows.find(n => n.present !== false && nodeState(n, noQuota) === 'no_credentials')?.node_id ?? null
      : null
    const offersSignIn = rowCanLogIn && !!slug && noCredentials && (signInNode !== null || addable)
    /* Signed in nowhere and no node has reported: there is nothing to sign it in on, so the next step is a node;
    a host runs no work, so hosts alone count as none. Add node is the org admin's (`Nodes.tsx`'s
    `showAddNode`). */
    const needsNode = noCredentials && workRows.length === 0
    /* No node holds a session: one that did was deleted, taking it along, or none ever did. With the
    Sign in link beside it, the link is the "sign in again". */
    const nodeRemoved = !!record?.node_removed_at
    const notServingText = nodeRemoved
      ? rowCanLogIn ? 'Its node was removed — sign in again.' : 'Its node was removed.'
      : 'Not signed in on any node yet.'
    /* Worded for the reader's reach: the fact is the same for everyone; the remedy only for whoever holds its
    verb (`canLogIn` for Add account, `isOrgAdmin` for the record-only delete). Three arms (an org admin
    always has `canLogIn`). The `!canLogIn` arm names the actor: login is `system:create`
    (`NO_LOGIN_PERMISSION_REASON`), so no `accounts:*` grant helps; "Operator" is the one actor a tenant is
    told about. */
    const noCredentialsTitle = 'No login has completed for this account, so it cannot authenticate.'
      + (needsNode
        ? ` No node to sign it in on yet.${isOrgAdmin ? ' Add one from ⋯, or delete it.' : ' Ask an operator to add one.'}`
        : rowCanLogIn
        ? offersSignIn
          ? isOrgAdmin ? ' Sign it in from ⋯, or delete it.' : ' Sign it in from ⋯.'
          : isOrgAdmin ? ' Finish Add account for it, or delete it.' : ' Finish Add account for it.'
        : otherOrg && canLogIn ? ` ${actAsToLogIn(otherOrg)}.` : ' Ask an operator to log it in.')
    const loginExpiry = isAuthRejected(acct.quota) ? null : soonestLoginExpiry(nodeRows)
    // Does any node row offer a Login? Only a 'token' rejection does (billing/seat are text).
    const offersNodeLogin = nodeRows.some(
      n => isAuthRejected(n.quota) && !REJECTION[n.quota?.authRejectedReason ?? ''],
    )
    /* A login about to expire is renewed early, on the node holding it; null when this viewer may not log in. */
    const renewLogin = loginExpiry && rowCanLogIn && !isApiKey && slug
      ? () => navigate(accountLoginPath(slug, loginExpiry.nodeId))
      : null
    /* An API-key account whose stored key is gone (its credential row was deleted): nothing can run on it. */
    const keyMissing = isApiKey && record?.api_key_set === false
    const healthState = keyMissing && enabled ? 'erroring'
      : accountHealth(acct, enabled, summary, isApiKey, noQuota, noCredentials)
    const health = ACCOUNT_HEALTH_MARK[healthState]
    const editBlocked = editBlockedReason(acct.account_id, record)
    /* The name opens the account's page, as People, Projects and Secrets rows do; Edit stays in ⋯.
    No link while Edit is blocked: no row, or fields that did not load. */
    const linked = editBlocked === null
    // The label every surface shows (`accounts.name`); the slug only for a row with no readable record.
    const name = record?.name || acct.name || slug || acct.email || 'account'
    const sharedVia = isApiKey ? [] : (acct.shared_via ?? [])
    /* The name and the email hover with the account's description; the name alone when it has none. */
    const identityTitle = record?.description ? `${name} — ${record.description}` : name
    /* Why it is not signed in, then the verb that fixes it as a link, so a phone reader need not find it in ⋯. */
    const loginSentence = (verb: string, to: string) => (
      <>
        {nodeRemoved ? 'Its node was removed.' : notServingText}{' '}
        <Link
          component="button"
          type="button"
          variant="inherit"
          underline="hover"
          onClick={() => navigate(to)}
          aria-label={`${verb}: ${name}`}
          sx={{ verticalAlign: 'baseline' }}
        >
          {verb}
        </Link>
      </>
    )

    const actions: OverflowItem[] = []
    // The account-level re-login covers only an account with no node row to click; it stands down once a node
    // row offers a re-login (`offersNodeLogin`, token rejections only). A `missing` row's first login does not.
    /* No re-login for an API-key account (no OAuth session). Reachable: `accountHealth` keeps `isAuthRejected`
    for this kind, and `Accounts.test.tsx` proves this guard removes the verb. */
    /* `canLogIn` first: this is the one account-line verb that is not org-admin-only; it dispatches
    `account_reauth` on the command bus (`LOGIN_PERMISSION`). */
    if (rowCanLogIn && !isApiKey && isAuthRejected(acct.quota) && slug && !offersNodeLogin) {
      actions.push({
        key: 'relogin',
        icon: '↻',
        label: 'Re-login',
        ariaLabel: `Re-login ${name} — refresh this account's expired OAuth credentials`,
        onClick: () => navigate(accountLoginPath(slug)),
      })
    } else if (renewLogin) {
      actions.push({
        key: 'relogin',
        icon: '↻',
        label: 'Re-login',
        ariaLabel: `Re-login ${name} — renew its login before it expires`,
        onClick: renewLogin,
      })
    }
    if (offersSignIn && slug) {
      actions.push({
        key: 'sign-in',
        icon: '→',
        label: 'Sign in',
        ariaLabel: `Sign in ${name} — log this account in on a node`,
        onClick: () => navigate(accountLoginPath(slug, signInNode, true)),
      })
    }
    if (needsNode && isOrgAdmin) {
      actions.push({
        key: 'new-node',
        icon: '+',
        label: 'Add node',
        ariaLabel: `Add node — no node to sign ${name} in on yet`,
        onClick: () => navigate(nodesAddPath(rowOrg?.id)),
      })
    }
    /* The one way onto another node, in place of a Login per node it is not on (`addable`). */
    // Sign in already IS this picker when no node holds an empty dir; otherwise both: they reach different nodes.
    if (addable && slug && !(offersSignIn && signInNode === null)) {
      actions.push({
        key: 'add-node',
        icon: '+',
        label: 'Add to node',
        ariaLabel: `Add ${name} to another node — log it in there`,
        onClick: () => navigate(accountLoginPath(slug, null, true)),
      })
    }
    if (isOrgAdmin) {
      /* Edit, the toggle, then the destructive pair: least to most consequential. */
      // Names which account; `RowActionsOverflow` appends `editBlockedReason` when disabled.
      actions.push(editItem(name, () => { if (!editBlocked && acct.account_id != null) navigate(accountEditPath(acct.account_id)) }, {
        disabled: editBlocked !== null,
        disabledReason: editBlocked ?? undefined,
      }))
      actions.push({
        /* The toggle's second home at every width, sharing `setAccountEnabled`, without the optimistic overlay
        (the menu closes on press). */
        key: 'enabled',
        icon: enabled ? '⏸' : '▶',
        label: enabled ? 'Disable' : 'Enable',
        ariaLabel: `${enabled ? 'Disable' : 'Enable'} ${name}${lastEnabledWarning ? ` — ${lastEnabledWarning}` : ''}`,
        // The inline toggle's fail-closed gate (`NO_ROW_TITLE`), on the sheet item too; another org's goes as that org.
        disabled: acct.account_id == null,
        disabledReason: NO_ROW_TITLE,
        onClick: () => { void toggleEnabled(rowKey, acct, enabled, otherOrg) },
      })
      /* The way into that org for this whole tab (`lib/actAs.ts`), as the task board's row offers. */
      if (otherOrg) {
        actions.push({
          key: 'act-as',
          icon: '⇄',
          label: `Act as ${otherOrg.name}`,
          ariaLabel: `Act as ${otherOrg.name}`,
          onClick: () => enterActAs(otherOrg),
        })
      }
      /* Delete, on every account whatever its nodes say (`deleteQuestion`); per-node Deprovision stays on the
      node rows. */
      actions.push({
        key: 'delete',
        icon: '✕',
        label: 'Delete',
        ariaLabel: `Delete ${name}`,
        danger: true,
        disabled: acct.account_id == null,
        disabledReason: NO_ROW_TITLE,
        onClick: () => { void confirmAndDelete(rowKey, acct, name, isApiKey, otherOrg) },
      })
    }
    /* The operator's "Change org…", gated inside the hook. It addresses the `accounts` row, so a
    line with no row offers none; the name is `accounts.name`, the server's `name_of`, never the slug fallback. */
    const rowName = record?.name || acct.name
    const changeOrgItem = acct.account_id != null && rowName
      ? changeOrg.menuItem({ id: acct.account_id, name: rowName, org: rowOrg, href: accountEditPath(acct.account_id) })
      : null
    if (changeOrgItem) {
      // Before the destructive pair, as on every other surface.
      const at = actions.findIndex(a => a.danger)
      actions.splice(at < 0 ? actions.length : at, 0, changeOrgItem)
    }

    const slots: FeedRowSpec['slots'] = [
      // The tenant comes from the joined `/api/accounts` record, else the roster row's own `tenant_org`.
      ...orgSlots(orgVisible && rowOrg !== undefined, rowOrg, { gate: 'acctOrg' }),
      {
        key: 'sessions',
        width: SESSIONS_SLOT,
        gate: 'acctSessions',
        align: 'right',
        mono: true,
        dim: true,
        testId: 'acct-sessions',
        node: acct.sessions && acct.sessions.total > 0 ? (
          <HoverCard content={<SessionsTip worktrees={acct.worktrees ?? []} />}>
            <Box component="span" sx={{ display: 'inline-flex', alignItems: 'center' }}>
              <Box component="span">{acct.sessions.total}</Box>
              {acct.sessions.busy > 0 && (
                <Box component="span" sx={{ ml: 0.5, display: 'inline-flex' }}>
                  <StatusChip label={`${acct.sessions.busy} busy`} tone={toneFor('badge-running')} />
                </Box>
              )}
            </Box>
          </HoverCard>
        ) : '—',
      },
    ]
    if (isOrgAdmin) {
      slots.push({
        key: 'enabled',
        width: TOGGLE_SLOT,
        align: 'center',
        testId: 'acct-enabled',
        /* Gated off the phone row (the ⋯ item covers it): the band's 24px row has no room for a 44px tap target
        below `sm`. Off a narrow roster too (`ACCOUNTS_NO_ENABLED`). */
        gate: 'acctEnabled',
        node: (
          <StateToggle
            account={displayAcct}
            warning={lastEnabledWarning}
            otherOrg={otherOrg}
            onOptimistic={applyOptimistic}
            onCompleted={() => void fetchResources()}
            onError={setWriteError}
          />
        ),
      })
    }
    /* The provider (PRD R1.2's first surface). A slot, not a chip: every row has one provider, so the reservation
    is never wasted and vendors form runs down a column. Left-aligned, not mono (vendor words). Left of the 5h
    reading, which stays right-most. Off the phone band: its 99px left the identity ~58px at 360px, too narrow to
    name the account, so there it reads under the name instead (`PROVIDER_UNDER_SX`), still whole; likewise on a
    narrow roster (`ACCOUNTS_PROVIDER_UNDER`, beside an open account). */
    slots.push({
      key: 'provider',
      width: PROVIDER_SLOT,
      gate: 'acctProvider',
      align: 'left',
      testId: 'acct-provider',
      // The state an oracle reads; `providerCell` has three kinds of answer.
      data: { provider: record?.provider ?? undefined, 'provider-kind': provider.kind },
      title: provider.title,
      // The page's dash for "not read", never a defaulted vendor (see `providerCell`).
      node: provider.label ?? NO_ACTOR,
    })
    /* The 5h reading is ungated and last: `ui/feed/geometry.ts` orders slots meta → id → ungated, so the
    right-most survives narrowing. 5h at every width (it gates a launch in the next minutes), 7d to 768px,
    sessions above 900px; with all on `meta` no quota showed below 900px. */
    const fivePct = noShort ? acct.quota?.weeklyPercent : acct.quota?.fiveHourPercent
    const fiveIsDash = !noQuota && fivePct == null && (isAuthRejected(acct.quota) || !isRefreshBlocked(acct.quota))
    const quotaDash = isAuthRejected(acct.quota) || !isRefreshBlocked(acct.quota)
    dashSlots.push(new Set([
      ...(acct.sessions && acct.sessions.total > 0 ? [] : ['sessions']),
      ...(!noQuota && !noWeekly && !noShort && acct.quota?.weeklyPercent == null && quotaDash ? ['week'] : []),
      ...(provider.label == null ? ['provider'] : []),
      ...(fiveIsDash ? ['five'] : []),
    ]))
    const phoneDashGone = noCredentials && fiveIsDash
    slots.push({
      key: 'five', width: QUOTA_SLOT, align: 'right', mono: true, testId: 'acct-5h',
      /* A no-credentials row whose cell is a bare dash (`QuotaCell` with no figure and no blocked chip): on a phone
      the slot closes and the dash goes, since the provider line's "no credentials" already says why there is no
      reading; the identity takes the room. Likewise on a narrow roster, where the row's
      `no credentials` chip says it and the 92px left the name ~80px short. A kept, stale figure keeps 92px, so
      its `⚠ old` note is never cut. */
      phoneWidth: phoneDashGone ? 0 : undefined,
      narrowWidth: phoneDashGone ? { query: ACCOUNTS_PROVIDER_UNDER, width: 0 } : undefined,
      // On a phone this is the only quota column, so it must be right about which nothing it shows.
      /* A plan with only a weekly window shows THAT reading here: on a phone this is the only quota column,
      and an `n/a` in it would hide the account's one real figure. The other slot then says so. */
      data: { quota: noQuota ? 'not-applicable' : 'reading' },
      title: noQuota ? undefined
        : noShort ? `${quotaWindows[1] ?? 'weekly'} quota used — this plan has no ${quotaWindows[0]} window`
        : `${quotaWindows[0]} quota used`,
      node: noQuota ? <NoQuotaCell title={noQuotaTitle} /> : (
        <Box component="span" sx={phoneDashGone ? DASH_GONE_SX : undefined}>
          <QuotaCell
            pct={fivePct}
            resetsAt={noShort ? acct.quota?.weeklyResetsAt : acct.quota?.fiveHourResetsAt}
            authRejected={isAuthRejected(acct.quota)}
            refreshBlocked={isRefreshBlocked(acct.quota)}
            ageMs={quotaAgeMs(acct.quota)}
          />
        </Box>
      ),
    })

    /* The weekly reading, beside 5h under one pair of headers: it drops first (`acctWeek`), 5h never.
    The quota slots answer for the kind, not the reading: an API-key account has no plan windows, and
    `QuotaCell` with null percents says "not read yet". `NoQuotaCell` states the fact; `data.quota` exposes
    it as a state (`FeedSlot.data`). */
    slots.push({
      key: 'week', width: QUOTA_SLOT, gate: 'acctWeek', align: 'right', mono: true, testId: 'acct-7d',
      data: { quota: noQuota || noWeekly || noShort ? 'not-applicable' : 'reading' },
      title: noQuota || noWeekly || noShort ? undefined : `${quotaWindows[1]} quota used`,
      node: noQuota ? <NoQuotaCell title={noQuotaTitle} />
        : noWeekly ? <NoQuotaCell title={noWeeklyWindowTitle(quotaWindows[0])} />
        : noShort ? <NoQuotaCell title={noShortWindowTitle(quotaWindows[0])} /> : (
        <QuotaCell
          pct={acct.quota?.weeklyPercent}
          resetsAt={acct.quota?.weeklyResetsAt}
          authRejected={isAuthRejected(acct.quota)}
          refreshBlocked={isRefreshBlocked(acct.quota)}
          ageMs={quotaAgeMs(acct.quota)}
        />
      ),
    })

    const markers = []
    /* A mark, not the word: a marker reserves no width on rows without it. Warning-toned ⚠ so it never reads as a
    stray grey dot; not on a no-credentials row, whose chip and ✕ already say it. */
    if (summary && !noCredentials) {
      markers.push({ key: 'attention', glyph: '⚠', tone: 'warning' as const, label: `Needs attention: ${summary} on ${blockedOn.join(', ')}` })
    }
    return {
      key: rowKey,
      indicator: {
        tone: health.tone,
        glyph: health.glyph,
        label: healthState,
      },
      title: (
        <Box
          component="span"
          data-testid="acct-identity"
          sx={[IDENTITY_SX, enabled ? null : IDENTITY_DISABLED_SX]}
        >
          <Box component="span" data-testid="acct-name" sx={NAME_SX} style={nameWidthStyle(name)} title={identityTitle}>
            {name}
            {sharedVia.length > 0 && <SharedViaMark links={sharedVia} />}
          </Box>
          <Box
            component="span"
            data-testid="acct-provider-phone"
            sx={PROVIDER_UNDER_SX}
            title={noCredentials ? [provider.title, noCredentialsTitle].filter(Boolean).join(' ') : provider.title}
          >
            {/* A phone's state leads the provider line rather than hanging on a line of its own (`STATE_UNDER_SX`). */}
            {isOrgAdmin && <StateUnderMark enabled={enabled} testId="acct-state-provider-line" sx={STATE_ON_PROVIDER_LINE_SX} />}
            {/* First, so an ellipsized line still says why the account cannot run. */}
            {noCredentials && (
              <Box component="span" data-testid="acct-no-credentials-under" sx={NO_CREDENTIALS_UNDER_SX}>no credentials · </Box>
            )}
            {provider.label ?? NO_ACTOR}
          </Box>
          {isOrgAdmin && <StateUnderMark enabled={enabled} testId="acct-state-phone" sx={STATE_UNDER_SX} />}
          {/* The kind chip takes the identity region, where a subscription shows its email; an API-key account has
          none, so the kinds separate on sight. Only on this kind. No glyph: "API key" is already the whole signal,
          and a key character renders as a box on some fonts. */}
          {isApiKey ? (
            <>
              <StatusChip
                label="API key"
                tone="info"
                title={apiKeyAccountTitle(
                  record && !isDefaultProvider(record.provider) ? provider.label ?? record.provider : null,
                )}
              />
              {keyMissing && <StatusChip label="no key" tone="error" title={NO_KEY_TITLE} />}
              {/* The roster-row pass sends `name` in the `email` slot (`routes/portfolio.py`); shown only when it
              says something the name above does not. */}
              {acct.email && acct.email !== name && <Box component="span" sx={EMAIL_SX}>{acct.email}</Box>}
            </>
          ) : noCredentials ? (
            <>
              <StatusChip
                className={NO_CREDENTIALS_CHIP_CLASS}
                label="no credentials"
                tone={toneFor('badge-idle')}
                title={noCredentialsTitle}
              />
              {/* A probed identity the name above does not already say. */}
              {acct.email && acct.email !== name && <Box component="span" sx={EMAIL_SX} title={identityTitle}>{acct.email}</Box>}
            </>
          ) : (
            /* Wrapped, not a bare text node, so `EMAIL_SX` can ellipsize it. Not repeated when it is the name. */
            acct.email !== name && <Box component="span" sx={EMAIL_SX} title={identityTitle}>{acct.email ?? NO_ACTOR}</Box>
          )}
          {/* The plan the node's probe read (`tier`), for every provider alike: identity, beside the login it names. */}
          {acct.tier && !isApiKey && (
            <Box component="span" data-testid="acct-plan" sx={PLAN_SX} title={`Plan: ${acct.tier}`}>{acct.tier}</Box>
          )}
          {/* Out of rotation on a runner's break: the verdict, its sentence (e.g. the vendor refusing the account). */}
          {record?.quota_status_detail && (
            <StatusChip
              label={record.quota_status === 'auth_rejected' ? 'rejected' : 'limit hit'}
              tone={record.quota_status === 'auth_rejected' ? 'error' : 'warning'}
              title={record.quota_status_detail}
            />
          )}
          {/* Auto-assign off: the scheduler never picks it for "any provider" work — a fact about who it runs for,
          so a neutral chip beside the others, never a warning. */}
          {record?.auto_assign === false && (
            <StatusChip label="explicit only" tone="neutral" title={EXPLICIT_ONLY_TITLE} />
          )}
        </Box>
      ),
      titleText: name,
      /* Beside the link, not inside it: the expiry chip is a button for a viewer who may log in. */
      titleAside: loginExpiry || sharedVia.length > 0 ? (
        <>
          {/* Before the login dies, not after: an already-rejected account says so through the ✕ and the Nodes
          column instead. Pressing it renews now (the ⋯ Re-login's twin) for a viewer who may log in. */}
          {loginExpiry && (
            <StatusChip
              label={`re-login in ${compactSpan(Date.parse(loginExpiry.iso) - Date.now())}`}
              tone="warning"
              title={`Claude login expires ${absoluteTime(loginExpiry.iso)}. Only a re-login renews it.`}
              ariaLabel={renewLogin ? `Re-login ${name} now — expires ${absoluteTime(loginExpiry.iso)}` : undefined}
              onClick={renewLogin ?? undefined}
              press={{ glyph: '↻', label: 'Re-login' }}
            />
          )}
          {sharedVia.length > 0 && <SharedViaLinks links={sharedVia} onOpen={id => navigate(nodeLinkUrl(id))} />}
        </>
      ) : undefined,
      href: linked && acct.account_id != null ? accountEditPath(acct.account_id) : undefined,
      // Wraps only where the identity does (the phone band and a narrow roster, `IDENTITY_SX`/`NAME_SX`); one line elsewhere.
      titleLines: 'all',
      busy: busyRows.has(rowKey),
      data: { disabled: enabled ? undefined : 'true' },
      /* The expansion is the per-node table for a subscription and the record detail for an API-key account, so
      the control's word changes with it. */
      disclosure: {
        expanded: isOpen,
        onToggle: () => toggleExpanded(rowKey),
        label: isApiKey ? 'details' : 'nodes',
      },
      markers,
      slots,
      actions,
      /* Open, what the expansion has no table for, in words (`AccountDetailLines`). */
      detail: isOpen
        ? (
          <AccountDetailLines
            lines={[
              /* No roster row: say which absence, and what to do about it. The description is the name's hover and the
              provenance is the account page's. */
              ...(record ? [] : [{
                key: 'record',
                testId: 'acct-record-line',
                /* Plain words, never the table behind it: what is missing, and the one thing that fixes it. */
                text: acct.account_id == null
                  ? `A node reports this account, but it was never added here. Add it with Add account, `
                    + `name “${slug ?? name}”, to manage it.`
                  // `NO_RECORD_TITLE`'s fact; the endpoint is not superadmin-only.
                  : 'Description and provenance did not load. Reload the page to see them.',
              }]),
              /* Why no node table: for a subscription, no node visible to this seat has reported
              (`account_fold.account_node_matrix` keys on `last_seen`; a never-logged-in account shows `missing` rows);
              for an API-key account, nothing to log in anywhere, ever.
              The roster sentence names the fact, not a place ("environment" was false for a tenant's never-seen node,
              and not tenant vocabulary: `apps/api/app/tenancy/copy.py`). */
              ...(isApiKey
                ? [{ key: 'api-key-kind', testId: 'acct-api-key-note', text: 'The key is write-only: rotate it from Edit.' }]
                : nodeRows.length === 0 || needsNode
                ? [{
                    key: 'no-nodes',
                    testId: 'acct-no-nodes',
                    /* Signed in nowhere: say why there is no Sign in, and link the step that gives it one. */
                    text: needsNode
                      ? (
                        <>
                          No node to sign it in on yet.
                          {isOrgAdmin && (
                            <>
                              {' '}
                              <Link component={RouterLink} to={nodesAddPath(rowOrg?.id)} underline="hover" aria-label={`Add node: ${name}`}>
                                Add node
                              </Link>
                            </>
                          )}
                        </>
                      )
                      : 'No node has reported yet',
                  }]
                : servingRows.length === 0
                ? [{
                    key: 'no-nodes',
                    testId: 'acct-no-nodes',
                    /* The verb is the action itself, so a phone reader need not find it in the ⋯ menu. */
                    text: addable && slug
                      ? loginSentence(offersSignIn ? 'Sign in' : 'Add to node', accountLoginPath(slug, null, true))
                      : notServingText,
                  }]
                : []),
            ]}
          />
        )
        : undefined,
      /* Why it is signed in nowhere, on hover; the ⋯ menu's Sign in is the next step. */
      // Open, the detail's own sign-in sentence says it.
      notes: !isOpen && offersSignIn && slug
        ? [{ key: 'sign-in', tone: 'muted', text: nodeRemoved ? 'Its node was removed.' : notServingText }]
        : undefined,
      children: isOpen
        ? accountNodeRows({
            nodes: servingRows,
            accountId: acct.account_id,
            busy: busyRows.has(rowKey),
            noQuota,
            /* The account line's two verdicts, never re-read. `canDeprovision` is `isOrgAdmin`: the per-node route is
            `require_account_writer`, like its account-level twin. */
            canLogIn: rowCanLogIn,
            noLoginReason,
            canDeprovision: isOrgAdmin,
            onRelogin: node => { if (slug) navigate(accountLoginPath(slug, node)) },
            onEnrol: node => { if (slug) navigate(accountLoginPath(slug, node, true)) },
            onRemove: (node, label) => { void confirmAndRemoveNode(rowKey, acct.account_id, node, label, nodeRows.length) },
          })
        : undefined,
    }
  })
  const dashedEverywhere = besideDetail && dashSlots.length > 0
    ? [...dashSlots[0]].filter(key => dashSlots.every(set => set.has(key)))
    : []
  const statusRows = dashedEverywhere.length === 0 ? builtRows
    : builtRows.map(row => ({ ...row, slots: row.slots?.filter(slot => !dashedEverywhere.includes(slot.key)) }))
  /* A seat refused the status read (`refused`) still reads the records (`accounts:view`): their names, the status
  cells blank, rather than an empty page. Only while no status row was ever read; cached rows keep their place. */
  const recordRows: FeedRowSpec[] = refused && accounts.length === 0 && !recordsQuery.isError
    ? (recordsQuery.data ?? []).map((row): FeedRowSpec => {
        const provider = providerCell(row, providerCatalogue)
        return {
          key: row.id,
          title: (
            <Box component="span" data-testid="acct-name" sx={NAME_SX} style={nameWidthStyle(row.name)} title={row.description ? `${row.name} — ${row.description}` : row.name}>
              {row.name}
            </Box>
          ),
          titleText: row.name,
          href: accountEditPath(row.id),
          indicator: REFUSED_MARK,
          data: { status: 'unread' },
          slots: [
            ...orgSlots(orgVisible, row.org, { gate: 'acctOrg' }),
            { key: 'sessions', width: SESSIONS_SLOT, gate: 'acctSessions', node: null },
            { key: 'provider', width: PROVIDER_SLOT, gate: 'acctProvider', align: 'left', title: provider.title, node: provider.label ?? NO_ACTOR },
            { key: 'five', width: QUOTA_SLOT, node: null },
            { key: 'week', width: QUOTA_SLOT, gate: 'acctWeek', node: null },
          ],
        }
      })
    : []
  const accountRows = recordRows.length > 0 ? recordRows : statusRows

  // The ⋯ item's arm of the enable verb (`setAccountEnabled`).
  async function toggleEnabled(rowKey: string, account: ClaudeAccount, enabled: boolean, otherOrg: OrgRef | null = null) {
    const accountId = account.account_id
    if (accountId == null) return
    await run(rowKey, 'Failed to update the account', async () => {
      await setAccountEnabled(accountId, !enabled, actAsOnce(otherOrg))
      await fetchResources()
    })
  }

  return (
    /* The roster's own width gates its slots (`ACCOUNTS_LIST_CONTAINER`): an open account beside it narrows it. */
    <Box sx={ACCOUNTS_LIST_CONTAINER_SX}>
      {/* Add account, on the Machines strip via a titleless header (`components/TabActionsSlot`).
      Labelled, not a bare `+`: sibling tabs offer their primary action in the same shape ("Add node"). Visible text and `aria-label` match: `Add account` is asserted by
      `Accounts.test.tsx`, and WCAG 2.5.3 Label in Name requires it. `tapTargetSx` gives the 44px phone floor.
      Absent without the key — only a verb, so it goes (contrast `components/NodeEnableToggle.tsx`).
      `canLogIn`, not `isOrgAdmin`: a `system:create` holder completes `account_add` (`LOGIN_PERMISSION`);
      `kindField` gates the API-key form. Beside an open account the list is narrow: Add says only "Add" (a word,
      not a bare `+`), so it keeps Share's line beside the strip. */}
      <PageHeader
        actions={canLogIn || shareShown ? (
          <Box sx={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'flex-end', gap: 1 }}>
          {/* Absent without `credentials:edit` or the plan's NodeLink term (the NodeLink list is that upsell's
          one home). */}
          {shareShown && <ShareSubscriptionsAction />}
          {/* An account exported from another console (`AccountTransfer.tsx`); the create door decides who may. */}
          {canLogIn && !besideDetail && <ImportAccountAction />}
          {canLogIn && (
          <DisabledReason reason={addBlocked}>
            <Button
              type="button"
              /* Beside an open account both compact actions are outlined, Share's weight: the open editor's Save
              is that view's primary action, and a lone contained `+` read as louder than its neighbour. */
              variant={besideDetail ? 'outlined' : 'contained'}
              color="primary"
              onClick={() => navigate(NEW_ACCOUNT_PATH)}
              disabled={addBlocked !== undefined}
              aria-label="Add account"
              title={addBlocked ?? 'Add account — log a new account in on a node'}
              data-testid={besideDetail ? 'add-account-compact' : undefined}
              sx={besideDetail ? tapTargetSx : compactVerbProps('Add account', tapTargetSx).sx}
            >
              {besideDetail ? 'Add' : <CompactVerbLabel label="Add account" />}
            </Button>
          </DisabledReason>
          )}
          </Box>
        ) : undefined}
      />

      <Stack spacing={1} sx={{ mb: 1 }}>
        {/* A failed read with no rows says it in `LoadFailedState` below, not twice. */}
        {error && accountRows.length > 0 && <Banner severity="error">{error}</Banner>}
        {/* Refused after an earlier read: the cached rows stay, so the refusal says it over them. */}
        {refused && accountRows.length > 0 && <Banner severity="warning">{REFUSED_TITLE}</Banner>}
        {writeError && <Banner severity="error" onDismiss={() => setWriteError('')}>{writeError}</Banner>}
      </Stack>

      <ListStates
        loading={loading || (refused && accounts.length === 0 && recordsQuery.isPending)}
        // A skeleton the table body's height, not a spinner, to avoid a layout jump.
        skeleton={{ rows: 5 }}
        failed={!accountRows.length && error && (
          <LoadFailedState title="Accounts unavailable" error={error} onRetry={() => { void resQuery.refetch() }} />
        )}
        empty={!accountRows.length}
        noMatch={false}
        /* One empty state. The roster is narrowed server-side to the viewer's plane (`tenancy/seam.py`) and to
        granted accounts, so the title says "visible". */
        emptyTitle={refused ? REFUSED_TITLE : 'No runner accounts visible'}
        /* One `ui/feed/FeedList`; no full-bleed wrapper (no horizontal scrollport). Full content width, as every
        other list page. */
        feed={{
          ariaLabel: 'Accounts',
          testId: 'accounts-table',
          columns: accountColumns(quotaColumnWindows(rowWindows)),
          /* The records-only rows are flat (no children), so no thread joins their discs. */
          sections: [{ key: 'accounts', rows: accountRows, rail: recordRows.length > 0 ? false : undefined }],
        }}
      />

      {/* One confirm idiom for the page. */}
      {confirmDialog}
      {changeOrg.dialog}
    </Box>
  )
}
