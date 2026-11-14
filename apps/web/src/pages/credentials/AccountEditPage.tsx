import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Link as RouterLink, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MuiLink from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { ApiError, get, post, put } from '../../api'
import { currentOrgId, useAuth } from '../../auth'
import { useActAsRows } from '../../lib/actAs'
import { customerUrl } from '../../lib/customers'
import { PLAN_PAGE } from '../../lib/companySetup'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { useDirectoryTabs } from '../../lib/directoryTabs'
import { countOf } from '../../lib/humanize'
import AccessTab from '../../components/access/AccessTab'
import Breadcrumb from '../../components/Breadcrumb'
import { useChangeOrgAction } from '../../components/ChangeOrgDialog'
import { TabPanel, Tabs, type TabDef } from '../../components/Tabs'
import { useAgentProviders } from '../../hooks/useAgentProviders'
import { useQuery } from '@tanstack/react-query'
import { useEditRecord } from '../../hooks/useEditRecord'
import { useLeaveGuard } from '../../hooks/useLeaveGuard'
import { useEventStream } from '../../hooks/useEventStream'
import { useSubmit } from '../../hooks/useSubmit'
import { useUrlTab } from '../../hooks/useUrlTab'
import { isAuthRejected } from '../../lib/quota'
import { createSealKeyPair, exportPeerKey, seal, unseal } from '../../lib/loginSeal'
import { shortNode } from '../../lib/nodeName'
import { safeHref } from '../../lib/safeHref'
import { PROVISION_POLL_MS } from '../../lib/pollIntervals'
import type {
  Account, AccountAuthKind, ClaudeAccount, ClaudeResources, Command, ProviderChoice, ProvisioningRecord,
} from '../../types'
/* Provider facts imported from `types.ts` (each points to `apps/jaahd/agent_providers.py`), never
spelled as literals here. */
import { PASTE_CODE } from '../../types'
import { AUTH_KIND_LABEL } from '../../lib/authKindLabel'
import { defaultProviderOf, hiddenProviders, isDefaultProvider } from '../../lib/providerCatalogue'
import AdvancedSection from '../../ui/AdvancedSection'
import AuthorizeLink from '../../ui/AuthorizeLink'
import { ChoiceField, type Choice } from '../../ui/ChoiceField'
import Banner from '../../ui/Banner'
/* The device-auth code's copy affordance, the same primitive `ui/AuthorizeLink` uses for the URL. */
import { CopyableValue } from '../../ui/CopyableValue'
import { DetailCell, DetailField, DetailPage } from '../../ui/detail/DetailPage'
import DetailStatus from '../../ui/detail/DetailStatus'
import { FactsGrid } from '../../ui/FactsGrid'
import EmptyState from '../../ui/EmptyState'
import FieldRow from '../../ui/FieldRow'
import FormBox from '../../ui/FormBox'
import FormActionBar from '../../ui/FormActionBar'
import { EntityNameField } from '../../ui/EntityNameFields'
import FormTextField from '../../ui/FormTextField'
import type { Explain, HelpProp } from '../../ui/explain'
import HelpLabel from '../../ui/HelpLabel'
import { FONT_WEIGHT, LETTER_SPACING, LINE_HEIGHT, grid } from '../../theme'
import { tapTargetSx } from '../../ui/listStyles'
import { ActingForNotice, useOtherOrg } from '../../ui/OrgIndicator'
import LoadingState from '../../ui/LoadingState'
import MetaRow, { MetaActors } from '../../ui/MetaRow'
import SectionCard from '../../ui/SectionCard'
import SettingCheckbox from '../../ui/SettingCheckbox'
import SubmitButton from '../../ui/SubmitButton'
import EnableToggle from '../../ui/EnableToggle'
import { LeaveGuardDialog } from '../../ui/UnsavedChangesDialog'
import {
  accountEditPath, ACCOUNTS_PATH, applyPendingDispatch, AUTO_ASSIGN_HINT, dropPendingDispatch, fetchNodeChoices, holdPendingDispatch,
  MAX_RUNNERS_HINT, MAX_RUNNERS_TIP, AUTO_ASSIGN_TIP, MAX_RUNNERS_HINT_API_KEY, takesRunnerCap, nonBaaSurfaceFor, MAX_RUNNERS_INVALID, type NodeChoice,
  parseMaxRunners, SIGN_IN_NEEDS_PROJECT, slugOf, useCanAddAccount, useCanLogIn, useCanSignInNewAccount,
  hasInlineCode, withInlineCode,
} from './accountModel'
import { getResources, listAccounts, setAccountEnabled } from './api'
import { fetchSettings, SETTINGS_QUERY_KEY } from '../settings/KnobSection'
import { ImportAccountForm, useExportAccountAction } from './AccountTransfer'
import OwnershipRadios from './OwnershipRadios'
import OwnerMemberLookup, { type OwnerMember } from './OwnerMemberLookup'
import NonBaaWarning from '../../components/NonBaaWarning'
import AccountCapLine from '../ce/AccountCapLine'
import { isEnabled } from '../../lib/editionFeatures'
import { type CapRefusal, capRefusal, useCeLimits } from '../../lib/communityEdition'

/* ══ THE RUNNER-ACCOUNT FORM PAGE ═══════════════════════════════════════════════════════════════════
 *
 * One page, three addresses, one field order:
 *   /accounts/new             — add an account, subscription (OAuth) or API key, for any provider;
 *   /accounts/:id             — edit an existing account's row (`PUT /api/accounts/{id}`);
 *   /accounts/login/:slug     — sign an existing account in: a re-login, or `?enrol=1` for a node it is
 *                               missing from; `?node=` pre-selects the node.
 * New account leads with Provider · Authentication, then the credential (API key, or the node the login runs
 * on), and folds the rest — Name, Owner, Description, dispatch (Auto-assign · Max runners) — into Advanced;
 * the node folds there too when it shares its token through the accounts NodeLink. Edit and sign-in keep Name,
 * Slug · Provider · Authentication, then the credential, then dispatch. What a mode cannot change is shown
 * disabled in its row, never moved or dropped. Explanations are hovers (`FormField`'s `help`), not
 * captions; only a consequence the operator must act on (a refusal, a wrong-node warning) is a banner.
 * A new account names no slug: the server draws it (`apps/api/app/account_slug.py`), and a blank name is the slug.
 *
 * Gates are presentation only (`components/RequireFeature.tsx`); the server enforces each write:
 *   · `POST /api/accounts` (API-key create) and `PUT /api/accounts/{id}` are org-admin-only (`require_account_writer`);
 *   · every login verb is `system:create` (`accountModel.ts::LOGIN_PERMISSION`).
 */

/* The page's column. */
const PAGE_MEASURE_SX = { maxWidth: grid(128) } as const

/* The ways the sealed handoff dead-ends on this page, said once. All end in "Start again": the
 * next Start mints a new key pair and the control plane cancels the old attempt. `SEAL_LOST` is keyed by
 * `useLoginFlow`'s `sealError`, so each cause says what actually happened. */
const SEAL_LOST = {
  // No key pair here for this record: an attempt adopted on a 409, started from another page.
  foreign: 'This login was started from another page, so its link cannot be opened here. Start it again.',
  // This page's own attempt, but its blob would not open (bad shape, or sealed to another key).
  unopenable: 'The login link could not be opened. Start again.',
  // The record names no `seal_key`, so a code cannot be sealed to the node.
  no_key: 'This login cannot accept a code. Start again.',
} as const
type SealError = keyof typeof SEAL_LOST
const OFF_VENDOR_LINK = 'The login link is not the vendor’s authorize page. Start again.'
const CATALOGUE_LOST =
  'The provider catalogue could not be loaded, so the login link cannot be checked. Reload and start again.'

// The wizard's terminal error codes → human sentences, never the raw code.
const ERROR_MESSAGES: Record<string, string> = {
  oauth_url_scrape_failed:
    "Couldn't read the login URL from the Claude CLI output — its format may have changed. Report this; retrying won't help.",
  /* Both mode-neutral: an await-mode (device-auth) login pastes nothing, and the node reuses these codes
  on both paths.
  • `login_timeout` is the await path's expiry (the node's `timeout 900` starts at spawn, before the CP's
  deadline), so it claims nothing was owed from the operator.
  • `login_rejected` still fires in await mode (the server refused what landed); it drops "wrong", which
  could only mean a mistyped paste. */
  login_timeout: 'Login timed out — it was not completed in time. You can retry.',
  login_rejected: 'The authorization was rejected — it may have expired or already been used. You can retry.',
  login_process_gone: 'The login session expired before the code was submitted. Close this and start the add again.',
  /* The node appends where it looked (`accounts._login_unreadable_refusal`). Not a rejection: the vendor
  accepted the sign-in, and this node's own reader cannot see the result. */
  credential_unreadable:
    'Signed in, but this node cannot read the account’s credential, so the account cannot run there. Have the '
    + 'node checked',
  no_pending_login: 'No login is waiting for a code for this account — start the add again.',
  omc_install_failed: 'Login succeeded but installing oh-my-claudecode into the account failed. The account was not enabled.',
  in_progress_elsewhere: 'A login flow for this slug is already running. Wait for it to finish.',
  account_add_failed: 'The add-account flow hit an unexpected error. Check the agent logs, then retry.',
  account_reauth_failed: 'The re-login flow hit an unexpected error. Check the agent logs, then retry.',
  account_not_found: 'No such account to re-authenticate — it may have been deleted. Close and refresh.',
  login_cli_missing:
    'This node has no `claude` CLI, so it cannot log an account in at all — nothing about the account is '
    + "wrong. Provision the node's runner toolchain, then retry.",
  login_spawn_failed: 'The login process could not be started on the node.',
  /* The sealed handoff's two refusals. `login_seal_unsupported`: the node answered in the
  clear (an old daemon), and the control plane failed the login rather than relay a plaintext URL; the fix
  is the node's update, not a retry now. `login_seal_invalid`: the node could not use our key or unseal the
  code — a fresh Start mints a fresh pair. */
  login_seal_unsupported:
    'This node’s daemon is too old to seal the login. Wait for it to update, then retry.',
  login_seal_invalid: 'The login could not be sealed to this node. Start again.',
  /* Short: the node appends which path it refused, the actionable half (`errorText` keeps both).
  "Refused": something already sat at the runtime path, possibly planted, and the node would not follow it. */
  login_runtime_unsafe:
    'The node refused to set up this login because something unexpected already exists at its runtime '
    + 'path, and it will not follow or overwrite it. Have that path checked before retrying.',
  login_wrong_uid:
    'The node cannot run the login as the user that must own the credentials, so it refused rather than '
    + 'lock the account out.',
  /* The ways a login refuses to mint a token in the wrong store (node side:
  `apps/jaahd/accounts.py::_login_store_refusal`). `login_store_unknown` is re-auth only
  (`require_existing`; see `_account_add`'s NOTE); `mode="reauth"` renders from this map.
  Short because the node appends the actionable detail. Neither invites a retry; `login_store_mismatch` says
  "report": the two deployables composing a path differently means one runs older code. */
  login_store_unknown:
    'The node could not confirm with the control plane where this account’s token belongs, so it refused '
    + 'rather than guess between the shared token folder and this host’s own disk.',
  login_store_mismatch:
    'The node and the control plane name different token folders for this account, so the login '
    + 'refused to act on either. Report this — it means one of the two is running older code.',
  /* A vendor CLI saying this login can never succeed here, distinct from a scrape miss (both end with no
  URL after thirty seconds); the node asks the adapter first (`agents/login.py::refusal`). Retry and report
  both help nobody. The head is vague on purpose: `REFUSAL_REASONS` turns the appended reason into the
  remedy. */
  login_provider_refused:
    'The agent CLI on that node refused this login: it reported that the login cannot succeed there at '
    + 'all, rather than simply failing. Retrying will not help until the cause it gave is addressed.',
}

/* The publishable device-auth code shape, at module scope (not rebuilt per 2s-poll render); argued at its
use in `useLoginFlow`. */
const DEVICE_CODE_SHAPE = /^[A-Za-z0-9][A-Za-z0-9-]{0,63}$/

/* Refusal reasons → what an operator can do. A second map because `errorText` keys on the code's head, so
every refusal arrives as `login_provider_refused` with the reason in the detail; without this the banner
ends in a raw `codex_device_auth_disabled`. Each entry names the setting to change and any route that
needs none. An unknown reason renders the head sentence plus the raw reason — worse copy, never blank. */
const REFUSAL_REASONS: Record<string, string> = {
  /* Operator decision D13, the most likely way a first Codex registration fails: device-code login is a
  per-workspace setting; off, the device-code request 404s. An admin must turn one switch on — the only
  path now that the loopback alternative D13 once offered is retired, so no other is named. */
  codex_device_auth_disabled:
    'Device-code login is switched OFF for this Codex workspace. A workspace admin has to enable '
    + '“device code login” for the Codex server in the OpenAI admin console before an account can be '
    + 'registered. There is no other login path.',
}

function errorText(code: string | undefined): string {
  if (!code) return 'The add-account flow failed.'
  // A code may carry a node-appended detail (CLI output tail, searched path, unit to repair). Keep both the
  // mapped sentence and the detail rather than falling to the raw-code branch.
  const separator = code.indexOf(': ')
  const head = separator === -1 ? code : code.slice(0, separator)
  const detail = separator === -1 ? '' : code.slice(separator + 2).trim()
  /* Own keys only, via `Object.hasOwn`, on both lookups (as `CredentialEditPage.tsx::oauthErrorText`): both
  maps are object literals, and `head`/`detail` come from the node (`_REFUSAL_CODE_RE` admits
  `constructor`), which rendered `function Object() { [native code] }` into the banner. A guard, not a
  filter: a prototype key degrades down the same path as an unknown code. Kept as literals, not `Map`s, like
  their sibling. */
  const mapped = Object.hasOwn(ERROR_MESSAGES, head) ? ERROR_MESSAGES[head] : undefined
  // The detail is itself a code, looked up the same way; an unmapped reason still renders
  // `${mapped} — ${detail}`.
  const refusal = Object.hasOwn(REFUSAL_REASONS, detail) ? REFUSAL_REASONS[detail] : undefined
  /* A mapped reason is a sentence, so it follows a full stop; the dash introduces a raw token only. */
  if (mapped && refusal) return `${mapped} ${refusal}`
  if (mapped) return detail ? `${mapped} — ${detail}` : mapped
  return `The add-account flow failed (${code}).`
}

function isTerminal(status: string | undefined): boolean {
  return status === 'done' || (typeof status === 'string' && status.startsWith('error'))
}

// Phase 1's synchronous POST reply already carries the authorize URL (`_login_start` in
// apps/jaahd/accounts.py, passed through `CommandOut.result`). Seeding `record` from it shows the link
// and code form immediately, so the flow does not depend on `GET /api/accounts/provisioning` returning a
// record (without it, re-login dead-ended on "Waiting for login URL…"). The poll still owns progression.
// Untrusted agent stdout: anything malformed returns null (keep waiting). A non-empty `sealed_url` is
// required (a plaintext `oauth_url` from an old daemon seeds nothing: the control plane fails that login
// as `login_seal_unsupported`); failure paths reply with empty stdout (`_fail`).
function seedRecordFromReply(cmd: Command | null | undefined, slug: string): ProvisioningRecord | null {
  const stdout = cmd?.result?.['stdout']
  if (typeof stdout !== 'string' || !stdout) return null
  let payload: unknown
  try {
    payload = JSON.parse(stdout)
  } catch {
    return null
  }
  if (!payload || typeof payload !== 'object') return null
  const {
    sealed_url: sealedUrl, seal_key: sealKey, user_code: userCode, status, config_dir: dir,
  } = payload as Record<string, unknown>
  if (typeof sealedUrl !== 'string' || !sealedUrl) return null
  return {
    slug,
    config_dir: typeof dir === 'string' ? dir : '',
    // The payload's `status` is the step name; the record's `status` stays in_progress.
    step: typeof status === 'string' && status ? status : 'awaiting_code',
    status: 'in_progress',
    sealed_url: sealedUrl,
    // The node's key for the pasted code; absent, `submitCode` refuses rather than post plaintext.
    ...(typeof sealKey === 'string' ? { seal_key: sealKey } : {}),
    /* The device-auth code rides the seed too: an RFC 8628 login needs both URL and code. A non-string seeds
    nothing. An empty string is seeded deliberately: "a device-auth adapter produced nothing", rendered as a
    warning, so seed and poll agree. Spread, not set-to-undefined, so a login without a code keeps its
    existing record shape. */
    ...(typeof userCode === 'string' ? { user_code: userCode } : {}),
  }
}

/* What the provider's DECLARED `login_completion` says, and the one home for what an unresolvable row
 * means — read by `loginNeedsCode` (the entry point) and `redirectMode`'s degraded arm, so they agree.
 * `true` for a row this build cannot resolve: paste mode shows a possibly unneeded field, while await mode
 * hides it from an operator holding a code and hangs to the 900s timeout. Every uncertain answer is
 * "needs a code", as the control plane decides.
 * The other copy, by entry point — change one, change both:
 *   • the control plane: `apps/api/app/durable/accounts.py::_declared_completion`
 * A repo test pins both copies' pointers. */
function declaredPasteMode(provider?: ProviderChoice | null): boolean {
  return !provider || provider.login_completion === PASTE_CODE
}

/* How this login finishes — the wizard's entry point, asked of the provider's declaration for every
 * provider, Claude included (Claude's row declares `paste_code`; the loopback path is retired).
 * Asked, never inspected: a device-auth login opens no local listener, so its URL says nothing about how
 * it completes, and the URL is sealed to this page and the control plane never sees
 * it at all. Which rows are device-auth is `apps/jaahd/agent_providers.py`'s fact (`login_completion`),
 * also read by the node. Null (a failed catalogue) is `declaredPasteMode`'s answer. */
export function loginNeedsCode(provider?: ProviderChoice | null): boolean {
  return declaredPasteMode(provider)
}

/* The `driver` value the control plane puts in a non-terminal `awaiting_redirect` payload: "the CP's own
 * workflow is polling the node; you need not". Spelled once (a cross-layer wire value). Otherwise each
 * browser await POST buys only a round trip (~450 per 15-minute login). Absence means keep driving, so an
 * older control plane is served correctly.
 */
const AWAIT_DRIVER_CP = 'cp'

/* Should the browser ask again after this `*_await_login` reply? A boolean: the two non-driving outcomes
 * behave identically (exit; the 2s provisioning poll renders the outcome).
 * The verb is non-terminal: while the operator is still in their browser it answers
 * `{"status":"awaiting_redirect"}` with exit 0 and keeps the login alive. Anything else means stop (see
 * `submitCode`). Untrusted stdout, parsed defensively: unusable ⇒ stop.
 */
function shouldKeepDrivingAwait(cmd: Command | null | undefined): boolean {
  const stdout = cmd?.result?.['stdout']
  if (typeof stdout !== 'string' || !stdout) return false
  let payload: unknown
  try {
    payload = JSON.parse(stdout)
  } catch {
    return false
  }
  if (!payload || typeof payload !== 'object') return false
  const { status, driver } = payload as Record<string, unknown>
  // Status first: `driver` rides only the non-terminal reply, so a terminal reply stops on its own terms.
  if (status !== 'awaiting_redirect') return false
  // Non-terminal; the CP marker means its durable workflow is polling — stand down.
  return driver !== AWAIT_DRIVER_CP
}

/* Is a failed `*_await_login` dispatch worth retrying? Against a control plane without the `driver`
 * marker this effect is the only actor that reaches finalize, and a failed dispatch writes no terminal
 * state, so giving up strands the login. Transient: no status (network, abort), 5xx, 408, 429. Anything
 * else (422 unknown verb, 404, 403) is a refusal that retrying repeats: stop and say so.
 */
function awaitDispatchIsTransient(err: unknown): boolean {
  if (!(err instanceof ApiError)) return true
  return err.status >= 500 || err.status === 408 || err.status === 429
}

/* Consecutive transient failures retried; a success resets it. The gap doubles from `PROVISION_POLL_MS`,
 * capped: a persistent outage costs seven attempts over six gaps — 2+4+8+16+30+30 at the 2s base — ~90s.
 */
export const AWAIT_RETRY_LIMIT = 6
const AWAIT_RETRY_MAX_GAP_MS = 30_000

/* ── Hover copy: one line each, a fact rather than a definition ── */
const ACCOUNTS_DOCS = 'using-jaah/model-accounts/how-model-accounts-work'
const AUTH_KIND_HELP: Explain = { tip: 'Sets sign-in, billing and quota; fixed once created.', docs: ACCOUNTS_DOCS }
const AUTH_HELP: Record<AccountAuthKind, string> = {
  oauth: 'A person signs in per node; reports the plan quota.',
  api_key: 'Stored key: any node, no quota, billed per token.',
}
const PROVIDER_HELP = 'Which agent runs this account’s tasks; fixed once created.'
const API_KEY_NEW_HELP = 'Held by the credential broker; never shown again.'
/* An edit's key field is empty on every open (never read back); the placeholder says what blank means. */
const API_KEY_KEEP_PLACEHOLDER = 'Stored — leave blank to keep'
const API_KEY_ABSENT_PLACEHOLDER = 'No key stored'
/* An Anthropic key not scoped to a workspace needs one named on every request. A UX mirror of the one home,
`apps/jaahd/account_auth.py::is_valid_workspace_id`, which the API enforces; it rides a header, so no whitespace. */
const WORKSPACE_ID_RE = /^wrkspc_[A-Za-z0-9]+$/
const WORKSPACE_ID_HELP = 'Sent with every request by a key without a workspace.'
const WORKSPACE_ID_INVALID = 'Starts with wrkspc_, then letters and digits.'
const workspaceIdValid = (v: string) => !v.trim() || WORKSPACE_ID_RE.test(v.trim())
const SLUG_HELP = 'Picked by Jaah; names the sign-in’s folder on each node.'
/* An enrolment on the node its link names. */
const ENROL_NODE_HELP = 'This node gets its own sign-in for the account.'
/* "Add to node" from the Models page: an enrolment that picks its node among those the account is not on. */
const ADD_NODE_HELP: Explain = {
  tip: 'Adds a sign-in on this node; it stays there.',
  more: 'Only nodes the account is not on yet are listed.',
}
/* The '' node option: no `node_id`, the API places the login (`default_dispatch_node`). */
const AUTO_NODE_HELP = 'Jaah places the sign-in on the default node.'

/* The node hover: where the login runs. A `node_pinned` provider keeps its credential on that node, and a
 * provider whose CLI must be attested lists only nodes reporting it (`addLoginNodes`). */
function addNodeHelp(provider: ProviderChoice | null): HelpProp {
  const tip = provider?.node_pinned
    ? 'Credential stays on this node; other nodes need own sign-in.'
    : 'The login runs on this node.'
  return provider?.needs_attested_binary ? { tip, more: `Only nodes reporting \`${provider.binary}\` are listed.` } : tip
}
const REAUTH_NODE_HELP = 'Signs in again on this node; sessions don’t transfer.'
/* The two reasons the node list offers nothing to choose, as the disabled option's hover. */
const NODES_UNREAD_HELP: Explain = {
  tip: 'Node list unavailable; Jaah chooses the node and pins it.',
  more: 'The node list did not load (reading it needs the Nodes permission), so the node is chosen automatically '
    + 'and the account is pinned there. Reload to choose.',
}
const NODES_EMPTY_HELP: Explain = {
  tip: 'No visible node; Jaah chooses one and pins the account.',
  more: 'No node is visible to you, so the node is chosen automatically and the account is pinned there.',
}

/* ── Which node a login lands on ──
 * `addLoginNodes`: the nodes a NEW account's login may run on — the whole fleet, less nodes whose OS cannot run
 * the provider (`osRunsProvider`), narrowed again for a provider whose CLI must be attested
 * (`needs_attested_binary`; never-attested `runner_clis === null` is not offered). An enrolment narrows to its
 * one node: substituting another would start a login whose success the page could not observe. */
/** A node's short display name by uuid; the uuid itself only when the node list does not name it. */
function nodeLabel(nodes: NodeChoice[], nodeId: string): string {
  return shortNode(nodes.find(n => n.node_id === nodeId)?.name ?? nodeId)
}
function attestsBinary(node: NodeChoice, binary: string): boolean {
  return Array.isArray(node.runner_clis) && node.runner_clis.includes(binary)
}
/* The node's OS runs this provider's accounts: a Windows node lists `runner_cli:<provider>` in `unsupported` for
 * every vendor whose login is not ported there (`node_platform.py`), and its node refuses that sign-in. */
function osRunsProvider(node: NodeChoice, provider?: ProviderChoice | null): boolean {
  return !provider || !node.unsupported?.[`runner_cli:${provider.value}`]
}
function addLoginNodes(
  nodes: NodeChoice[], requirePresentOnNode?: string, provider?: ProviderChoice | null,
): NodeChoice[] {
  if (requirePresentOnNode) return nodes.filter(n => n.node_id === requirePresentOnNode)
  const runnable = nodes.filter(n => osRunsProvider(n, provider))
  if (provider?.needs_attested_binary) return runnable.filter(n => attestsBinary(n, provider.binary))
  return runnable
}

/** A refusal is never shipped without the move that lifts it. */
type AddBlock = { reason: string; remedy: string }

/* Why a new OAuth account cannot be created at all, or `null`: no node the seat can see runs the provider on its
 * OS, or none reports the vendor's CLI, which the control plane would 409. Never computed off an unread node list (a failed GET is not evidence).
 * Every node refusing with the one sentence (`unsupported['runner_cli:<p>']`) shows that sentence; mixed ones, the count's. */
function addBlockedReason(nodes: NodeChoice[], provider?: ProviderChoice | null): AddBlock | null {
  if (provider && nodes.length > 0 && !nodes.some(n => osRunsProvider(n, provider))) {
    const said = new Set(nodes.map(n => n.unsupported?.[`runner_cli:${provider.value}`]))
    return {
      reason: said.size === 1 ? [...said][0]! : `No node you can see runs ${provider.label} accounts on its operating system.`,
      remedy: 'Add a Linux node, or use an API key.',
    }
  }
  if (!provider?.needs_attested_binary) return null
  if (nodes.some(n => osRunsProvider(n, provider) && attestsBinary(n, provider.binary))) return null
  return {
    reason: `No node you can see reports the \`${provider.binary}\` binary, so no node can run a `
      + `${provider.label} login.`,
    remedy: `Install \`${provider.binary}\` for user \`jaah\` on a node; it appears here once that node `
      + 'next reports in. Or use an API key.',
  }
}

/* Would this login land on a node that cannot run the vendor's CLI — or `null`. For re-login and
 * enrolment, which bypass `addLoginNodes`' filter; the control plane refuses both arms. */
function binaryAttestationWarning(nodes: NodeChoice[], nodeId: string, provider: ProviderChoice | null): string | null {
  if (!provider?.needs_attested_binary) return null
  const target = nodes.find(n => n.node_id === nodeId)
  if (!target || attestsBinary(target, provider.binary)) return null
  const remedy = `Install \`${provider.binary}\` for user \`jaah\` there, or pick a node that reports it.`
  if (!Array.isArray(target.runner_clis)) {
    return `${nodeLabel(nodes, nodeId)} has never reported its agent CLIs, and the control plane refuses a `
      + `${provider.label} login on such a node. ${remedy}`
  }
  return `${nodeLabel(nodes, nodeId)} does not report the \`${provider.binary}\` binary, so the control plane will refuse `
    + `this login. ${remedy}`
}

/* The org default a subscription account's blank cap falls back to (`max_runners_per_account`), for the
 * placeholder. Best-effort: `GET /api/settings` is `system:view`; without it the placeholder names no number. */
function useOrgRunnerLimit(): number | null {
  // The error is dropped: the placeholder names no number.
  const { data } = useQuery({ queryKey: SETTINGS_QUERY_KEY, queryFn: fetchSettings })
  const value = data?.settings?.find(k => k.key === 'max_runners_per_account')?.value
  return typeof value === 'number' ? value : null
}

/* ══ The rows ══════════════════════════════════════════════════════════════════════════════════════
 * Each a `FormTextField` (label + hover help + the one error slot) around a native select or a text field.
 * Native selects: on a phone the platform's own picker, and the suite reads `.value`. */

/* The account's label (`accounts.name`). Optional on an add (blank shows the slug); an edit cannot blank it. */
function NameField({ value, onChange, disabled, required }: {
  value: string
  onChange: (v: string) => void
  disabled?: boolean
  required?: boolean
}) {
  const blank = !!required && value.trim() === ''
  return (
    <EntityNameField
      help={required ? 'Shown wherever the account appears.' : 'Shown wherever the account appears. Blank: the slug.'}
      error={blank ? 'Required' : undefined}
      required={!!required}
      id="account-name"
      value={value}
      disabled={disabled}
      onChange={onChange}
      placeholder="e.g. Team Claude"
    />
  )
}

/* The account's note (`accounts.description`, NOT NULL `''`): the roster's expanded "Description — …" line.
 * Blank clears it. `readOnly` shows it as that line does (backticked spans as code), not as a disabled source box.
 * Editable, the hint previews that rendering whenever the source differs from it: the admin sees what the roster
 * will show. */
function DescriptionField({ value, onChange, disabled, readOnly }: {
  value: string
  onChange: (v: string) => void
  disabled?: boolean
  readOnly?: boolean
}) {
  if (readOnly) {
    return (
      <FactsGrid line>
        <DetailField label="Description" testId="account-description-readonly">
          {withInlineCode(value || '—')}
        </DetailField>
      </FactsGrid>
    )
  }
  return (
    <FormTextField
      label="Description"
      help="Shown on the Agents list; backticked text renders as code."
      id="account-description"
      value={value}
      disabled={disabled}
      onChange={onChange}
      multiline
      minRows={2}
      hint={hasInlineCode(value)
        ? <Box component="span" data-testid="account-description-preview">Shows as: {withInlineCode(value)}</Box>
        : undefined}
    />
  )
}

function ProviderField({ value, onChange, providers, disabled, locked, fallbackLabel }: {
  value: string
  onChange?: (v: string) => void
  providers: ProviderChoice[]
  disabled?: boolean
  locked?: boolean
  /** The option label when the catalogue did not list `value` (unread, or a newer server). */
  fallbackLabel?: string
}) {
  const listed = providers.some(p => p.value === value)
  return (
    <FormTextField
      label="Provider"
      help={PROVIDER_HELP}
      id="account-provider"
      value={value}
      onChange={v => onChange?.(v)}
      disabled={disabled || locked}
      options={providers}
    >
      {!listed && <option value={value}>{fallbackLabel ?? value}</option>}
    </FormTextField>
  )
}

/* Why New account offers fewer providers than the catalogue lists: the org's provider grid (the plan
term `providers`) leaves out every auth kind they have — the API's `allowed_auth_kinds`. One muted line, the count
and the way out (`PlanRemedy`); the names, each with the auth kinds it has, on the count's hover. */
function HiddenProvidersNote({ providers }: { providers: ProviderChoice[] }) {
  const hidden = hiddenProviders(providers)
  if (hidden.length === 0) return null
  const labels = hidden.map(row => `${row.label} (${row.auth_kinds.map(kind => AUTH_KIND_LABEL[kind]).join(' or ')})`)
  const names = labels.length < 2 ? labels.join('') : `${labels.slice(0, -1).join(', ')} and ${labels.at(-1)}`
  return (
    <Typography variant="body2" data-testid="hidden-providers" sx={{ color: 'text.secondary' }}>
      <HelpLabel label={`${hidden.length} more ${hidden.length > 1 ? 'providers' : 'provider'}`} help={names} />
      {' '}{hidden.length > 1 ? 'are' : 'is'} {isEnabled('billing') ? 'not on your plan' : 'off'}. <PlanRemedy />
    </Typography>
  )
}

/* The way out of "not on your plan": the operator gets the Providers tab of the org's Overrides; an org admin, the plan page under
Billing; anyone else, whom to ask (`lib/planSwitches::NotOnPlan` words it the same). */
function PlanRemedy() {
  const { admin } = useAuth()
  // No plan on one machine: its one user turns providers on in Settings.
  if (!isEnabled('billing')) return <MuiLink component={RouterLink} to="/settings?area=providers" sx={{ fontWeight: FONT_WEIGHT.semibold }}>Turn on in Providers</MuiLink>
  const orgId = admin?.is_operator === true ? currentOrgId(admin) : null
  if (orgId) {
    return <MuiLink component={RouterLink} to={customerUrl(orgId, { tab: 'overrides', query: { overrides: 'providers' } })} sx={{ fontWeight: FONT_WEIGHT.semibold }}>Turn on in Overrides</MuiLink>
  }
  return isOrgAdminSeat(admin)
    ? <MuiLink component={RouterLink} to={PLAN_PAGE} sx={{ fontWeight: FONT_WEIGHT.semibold }}>Change plan</MuiLink>
    : <>Ask an admin to change the plan.</>
}

/* Who owns a new account (G2): the org, or the creating admin alone. Offered to an org admin only. */
type Ownership = 'org' | 'personal'
// Drawn in `OWNERSHIP_OPTIONS` order (Personal first); `org` stays the default value.
const ACCOUNT_OWNERS: readonly Ownership[] = ['personal', 'org']

function AuthKindField({ value, onChange, kinds, disabled }: {
  value: AccountAuthKind
  onChange?: (kind: AccountAuthKind) => void
  /** The kinds this provider offers this seat; one kind (or a fixed record) renders the row disabled. */
  kinds: AccountAuthKind[]
  disabled?: boolean
}) {
  const options = kinds.includes(value) ? kinds : [value, ...kinds]
  return (
    <FormTextField
      label="Authentication"
      help={AUTH_KIND_HELP}
      id="account-auth-kind"
      value={value}
      disabled={disabled || options.length < 2}
      /* Fail closed on an unrecognised value: a new kind without an arm here lands on 'oauth'. */
      onChange={v => onChange?.(v === 'api_key' ? 'api_key' : 'oauth')}
      options={options.map(k => ({ value: k, label: AUTH_KIND_LABEL[k], help: AUTH_HELP[k] }))}
    />
  )
}

/* The node a login runs on. `''` = no `node_id`: the API places it by plane (`default_dispatch_node`), so the
 * label says only that it is chosen automatically. `allowUnpinned` offers that '' beside real nodes (re-login);
 * an add never does, since the login's node is the new account's pin — unless there is no node to
 * name at all, when '' is the only (disabled) option and the hover says why. */
function NodeField({ value, onChange, nodes, disabled, help, allowUnpinned, emptyHelp }: {
  value: string
  onChange: (v: string) => void
  nodes: NodeChoice[]
  disabled?: boolean
  help: HelpProp
  allowUnpinned?: boolean
  /** The hover when `nodes` is empty. */
  emptyHelp?: HelpProp
}) {
  const empty = nodes.length === 0
  return (
    <FormTextField
      label="Node"
      help={empty && emptyHelp ? emptyHelp : help}
      id="account-node"
      value={empty ? '' : value}
      onChange={onChange}
      disabled={disabled || empty}
      options={[
        ...(allowUnpinned || empty ? [{ value: '', label: 'Chosen automatically', help: AUTO_NODE_HELP }] : []),
        ...nodes.map(n => ({
          value: n.node_id,
          // Short name only when unambiguous (two domains can shorten to one host name).
          label: nodes.filter(o => shortNode(o.name) === shortNode(n.name)).length > 1 ? n.name : shortNode(n.name),
        })),
      ]}
    />
  )
}

/* The key: write-only everywhere — in state while typed, sent once, dropped; never rendered back.
 * `type="password"` + `autoComplete="new-password"` (as `CredentialEditPage`), no reveal toggle. */
function ApiKeyField({ value, onChange, disabled, label, placeholder, required }: {
  value: string
  onChange: (v: string) => void
  disabled?: boolean
  label: string
  placeholder?: string
  required?: boolean
}) {
  return (
    <FormTextField
      label={label}
      help={API_KEY_NEW_HELP}
      required={required}
      id="account-api-key"
      type="password"
      autoComplete="new-password"
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      onChange={onChange}
    />
  )
}

function WorkspaceIdField({ value, onChange, disabled }: {
  value: string
  onChange: (v: string) => void
  disabled?: boolean
}) {
  return (
    <FormTextField
      label="Workspace ID"
      help={WORKSPACE_ID_HELP}
      error={workspaceIdValid(value) ? undefined : WORKSPACE_ID_INVALID}
      id="account-workspace-id"
      value={value}
      placeholder="wrkspc_…"
      disabled={disabled}
      onChange={onChange}
    />
  )
}

function MaxRunnersField({ value, onChange, disabled, apiKey }: {
  value: string
  onChange: (value: string) => void
  disabled?: boolean
  /** An API-key account: a blank cap means no limit, not the org default. */
  apiKey: boolean
}) {
  const orgLimit = useOrgRunnerLimit()
  const invalid = parseMaxRunners(value) === undefined
  const placeholder = apiKey ? 'No limit' : orgLimit != null ? `Org default: ${orgLimit}` : 'Org default'
  return (
    <FormTextField
      label="Max runners"
      help={{ tip: MAX_RUNNERS_TIP, more: apiKey ? MAX_RUNNERS_HINT_API_KEY : MAX_RUNNERS_HINT, docs: ACCOUNTS_DOCS }}
      error={invalid ? MAX_RUNNERS_INVALID : undefined}
      id="account-max-runners"
      value={value}
      disabled={disabled}
      onChange={onChange}
      placeholder={placeholder}
      slotProps={{ htmlInput: { inputMode: 'numeric' } }}
    />
  )
}

/* What this provider cannot do compared with the baseline CLI, as ONE line whose hover holds the list
 * (`capability` — `consequence`, verbatim from the catalogue). Nothing for the baseline (no degradations). */
function ProviderLimitations({ provider }: { provider: ProviderChoice | null }) {
  if (!provider?.degradations.length) return null
  const n = provider.degradations.length
  return (
    <Box data-testid="provider-limitations" sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        <HelpLabel
          label={`${countOf(n, 'limitation')} vs Claude Code`}
          help={provider.degradations.map(d => `${d.capability} — ${d.consequence}`).join('; ')}
        />
      </Typography>
    </Box>
  )
}

/* The provider row whose limitations show: none for the baseline CLI. */
function nonBaselineRow(providerRow: ProviderChoice | null): ProviderChoice | null {
  return providerRow && !isDefaultProvider(providerRow.value) ? providerRow : null
}

/* Auto-assign + Max runners (`maxRunners` null = no per-account cap), then the provider's limitations. */
function DispatchFields({ dispatch, autoAssign, onAutoAssign, maxRunners, onMaxRunners, apiKey, locked, providerRow, limitations = true }: {
  dispatch: boolean; autoAssign: boolean; onAutoAssign: (value: boolean) => void; maxRunners: string | null
  onMaxRunners: (value: string) => void; apiKey: boolean; locked: boolean; providerRow: ProviderChoice | null
  /** Off where the caller shows the provider's limitations itself (New account, outside Advanced). */
  limitations?: boolean
}) {
  return (
    <>
      {dispatch && (
        <FieldRow rowGap={grid(2)} align="center">
          <DetailCell>
            <SettingCheckbox
              name="account-auto-assign"
              label="Auto-assign"
              help={{ tip: AUTO_ASSIGN_TIP, more: AUTO_ASSIGN_HINT, docs: ACCOUNTS_DOCS }}
              checked={autoAssign}
              disabled={locked}
              onChange={onAutoAssign}
            />
          </DetailCell>
          {maxRunners !== null && (
            <DetailCell basis={grid(21)} max={grid(32)}>
              <MaxRunnersField value={maxRunners} onChange={onMaxRunners} disabled={locked} apiKey={apiKey} />
            </DetailCell>
          )}
        </FieldRow>
      )}
      {limitations && <ProviderLimitations provider={nonBaselineRow(providerRow)} />}
    </>
  )
}

/* The kinds the catalogue offers for `provider`, narrowed to what the ORG allows (`allowed_auth_kinds`,
 * absent = all), when this SEAT may add an account at all (`useCanAddAccount`): a key is a `POST /api/accounts`,
 * a subscription a login (`useCanSignInNewAccount`: it needs a node), each personal for a non-administrator.
 * An unread catalogue row is the baseline's pair. */
function offeredKinds(provider: ProviderChoice | null, canAdd: boolean, canSignIn: boolean): AccountAuthKind[] {
  if (!canAdd) return []
  return allowedKinds(provider).filter(k => k === 'api_key' || canSignIn)
}

function allowedKinds(provider: ProviderChoice | null): AccountAuthKind[] {
  const kinds = provider?.auth_kinds?.length ? provider.auth_kinds : (['oauth', 'api_key'] as AccountAuthKind[])
  const allowed = provider?.allowed_auth_kinds ?? kinds
  return kinds.filter(k => allowed.includes(k))
}

/* ══ The sign-in flow ═════════════════════════════════════════════════════════════════════════════
 * Every subscription login — a new add, an enrolment, a re-login — is a `POST /api/commands` with an
 * `account_*` verb, sealed end to end: a fresh key pair per attempt, the node seals the authorize
 * URL to it, and the pasted code goes back sealed to the node's key. The poll on
 * `GET /api/accounts/provisioning` drives progression; replies are never relied on. */

type LoginMode = 'add' | 'reauth'

type LoginFlowArgs = {
  mode: LoginMode
  /** The account's slug; '' for a new add, whose slug the server draws and the start reply carries. */
  slug: string
  /** A new add's label (`accounts.name`); blank leaves it to the server (the slug). */
  name?: string
  /** The node this login names; '' lets the API place it. */
  nodeId: string
  providerValue: string
  providerRow: ProviderChoice | null
  /** The catalogue's default provider (`defaultProviderOf`): a login for it omits `provider`. */
  defaultProvider: string
  /** An enrolment's node: success is "this node now holds a session", not "the account is enabled". */
  requirePresentOnNode?: string
  /** An add's owner, sent only when an org admin chose it. */
  ownership?: Ownership
  onDone: () => void
}

function useLoginFlow({ mode, slug: givenSlug, name, nodeId, providerValue, providerRow, defaultProvider, requirePresentOnNode, ownership, onDone }: LoginFlowArgs) {
  const isReauth = mode === 'reauth'
  /* A new add names no slug: the server draws one (`apps/api/app/account_slug.py`) and echoes it in the
  start reply's `args`, and every later step (poll, code, await) addresses it. */
  const [drawnSlug, setDrawnSlug] = useState('')
  const slug = givenSlug || drawnSlug
  const startCommand = isReauth ? 'account_reauth' : 'account_add'
  const submitCommand = isReauth ? 'account_reauth_submit_code' : 'account_add_submit_code'
  const awaitCommand = isReauth ? 'account_reauth_await_login' : 'account_add_await_login'
  const defaultLogin = isDefaultProvider(providerValue)
  const [phase, setPhase] = useState<'input' | 'submitting' | 'polling'>('input')
  const [submitError, setSubmitError] = useState('')
  /* The community edition's account cap, when the start was refused over it: shown as its one line (`AccountCapLine`). */
  const [capRefused, setCapRefused] = useState<CapRefusal | null>(null)
  const [record, setRecord] = useState<ProvisioningRecord | null>(null)
  const [code, setCode] = useState('')
  const [codeSubmitting, setCodeSubmitting] = useState(false)
  const [codeError, setCodeError] = useState('')
  /* The code went to the node: Confirm becomes Close while the poll waits for the outcome. */
  const [codeSent, setCodeSent] = useState(false)
  /* Open the vendor's page once this attempt's link arrives: the start click asked for it, but the link is
  sealed and lands seconds later, so the open waits for it (a browser that blocks a late open leaves the
  button). Once per attempt. */
  const autoOpenRef = useRef(false)
  /* The vendor's tab, opened blank on the start click itself (a popup blocker lets an open through only inside the
  click), cut from this page at once (`opener = null`, the `noopener` an open after the link would pass) and pointed at
  the link when it lands. Closed if the attempt ends without one. */
  const vendorTabRef = useRef<Window | null>(null)
  function openVendorTab() {
    const tab = window.open('', '_blank')
    if (tab) tab.opener = null
    vendorTabRef.current = tab
  }
  function dropVendorTab() {
    vendorTabRef.current?.close()
    vendorTabRef.current = null
  }
  /* This login's key pair (`lib/loginSeal.ts`), held only here: the private half is non-extractable and dies
   * with the page — no other tab can open this login's URL. A ref: nothing renders it. */
  const pairRef = useRef<CryptoKeyPair | null>(null)
  const [oauthUrl, setOauthUrl] = useState<string | null>(null)
  /* Why the sealed handoff dead-ended (`SEAL_LOST`), or null: the URL cannot be shown (or the code cannot be
   * sealed), and the only way on is a fresh Start. */
  const [sealError, setSealError] = useState<SealError | null>(null)
  const [awaitError, setAwaitError] = useState('')
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Whether this poll run has seen the slug's record; success clears it, so a vanish after a sighting is done.
  const sawRecordRef = useRef(false)
  const onDoneRef = useRef(onDone)
  onDoneRef.current = onDone

  /** Starts the login; resolves its slug once it is running (or an in-flight attempt was adopted), else `null`. */
  async function begin(): Promise<string | null> {
    setPhase('submitting')
    setSubmitError('')
    setCapRefused(null)
    setSealError(null)
    setCodeSent(false)
    autoOpenRef.current = true
    // Before the first await: still inside the click. Not when no link could be checked (`catalogueLost`).
    if (!catalogueLost) openVendorTab()
    try {
      /* A fresh pair per attempt, before the dispatch: a different `peer_key` also cancels a live attempt on
      the control plane, so a restarted page always restarts. */
      const pair = await createSealKeyPair()
      pairRef.current = pair
      const peerKey = await exportPeerKey(pair)
      /* `node_id` rides the body's top level (the dispatch address); `provider` is omitted for the default
      (absent means it server-side). */
      const cmd = await post<Command>('/api/commands', {
        command_type: startCommand,
        args: {
          /* A restart keeps the slug its first start drew, so it supersedes that login as a typed slug did. */
          ...(slug ? { slug } : {}),
          ...(!isReauth && name?.trim() ? { name: name.trim() } : {}),
          peer_key: peerKey,
          ...(providerValue && providerValue !== defaultProvider ? { provider: providerValue } : {}),
          ...(!isReauth && ownership ? { ownership } : {}),
        },
        ...(nodeId ? { node_id: nodeId } : {}),
      })
      // Seed from the synchronous reply; must not arm `sawRecordRef` (a first empty poll would read as done).
      const started = typeof cmd.args?.slug === 'string' && cmd.args.slug ? cmd.args.slug : slug
      setDrawnSlug(started)
      const seeded = seedRecordFromReply(cmd, started)
      if (seeded) setRecord(seeded)
      setPhase('polling')
      return started
    } catch (err) {
      /* A 409 can mean a prior attempt for this (slug, node) is still running: adopt it (it lands in
      `sealError`'s "Start again", since its URL was sealed to another page; an await-mode wait carries on).
      Dropping this attempt's pair is what marks the record as another page's (`sealError: 'foreign'`). */
      if (err instanceof ApiError && err.status === 409) {
        try {
          const existing = await get<ProvisioningRecord[]>('/api/accounts/provisioning')
          const resumable = existing.find(r => r.slug === slug
            && (!r.node_id || !nodeId || r.node_id === nodeId)
            && !isTerminal(r.status))
          if (resumable) {
            // Another page's attempt: its link never opens here.
            autoOpenRef.current = false
            dropVendorTab()
            pairRef.current = null
            setRecord(resumable)
            setPhase('polling')
            return slug
          }
        } catch {
          // Fall through to the error below.
        }
      }
      setCapRefused(capRefusal(err))
      setSubmitError(err instanceof ApiError ? String(err.detail ?? err.message)
        : `Failed to start ${isReauth ? 're-login' : 'the login'}`)
      // Back to the form to correct and retry: nothing was started.
      autoOpenRef.current = false
      dropVendorTab()
      setPhase('input')
      return null
    }
  }

  // Extra poll ticks re-checking /api/resources after a non-`done` terminal step: 30 × 2s = 60s.
  const TERMINAL_RECHECK_BUDGET = 30
  const terminalRechecksRef = useRef(0)

  useEffect(() => {
    if (phase !== 'polling') return
    let cancelled = false
    sawRecordRef.current = false
    terminalRechecksRef.current = 0

    /* Merge the device-authorization code rather than replace the record: it has two sources (the seed and
    the poll), and `null` on the wire means "said nothing" — only then is a known code kept. `''` (an adapter
    produced nothing) wins, so its warning shows. */
    const setPolledRecord = (mine: ProvisioningRecord) => setRecord(prev => (
      mine.user_code == null && prev?.user_code != null ? { ...mine, user_code: prev.user_code } : mine
    ))

    // The vanished-record fallback's probe: 'add' also requires enabled; 'reauth' presence alone.
    async function landedInResources(): Promise<boolean> {
      const res = await getResources().catch(() => null)
      if (!res) return false
      const landed = (a: ClaudeAccount) => (
        requirePresentOnNode
          ? (a.nodes ?? []).some(n => n.node_id === requirePresentOnNode && n.present !== false)
          : isReauth || a.enabled
      )
      return res.accounts.some(a => slugOf(a) === slug && landed(a))
    }

    // Stricter: the target node must report a real, non-rejected session. No concrete node → false.
    async function landedOnTargetNode(): Promise<boolean> {
      const targetNode = requirePresentOnNode || nodeId
      if (!targetNode) return false
      const res = await getResources().catch(() => null)
      if (!res) return false
      const acct = res.accounts.find(a => slugOf(a) === slug)
      const node = acct?.nodes?.find(n => n.node_id === targetNode)
      if (!node || node.present === false) return false
      if (isReauth) return node.status !== 'no_credentials' && !isAuthRejected(node.quota)
      return true
    }

    async function poll() {
      try {
        const records = await get<ProvisioningRecord[]>('/api/accounts/provisioning')
        // Match the node too: one slug can be in flight on several nodes.
        const mine = records.find(r => r.slug === slug && (!r.node_id || !nodeId || r.node_id === nodeId))
        if (cancelled) return
        if (mine) {
          sawRecordRef.current = true
          if (isTerminal(mine.status) && mine.status !== 'done' && (requirePresentOnNode || nodeId)) {
            /* A `timeout`/`error:*` step is not proof of failure: the CP's dispatch wait can give up while a slow
            finalize succeeds. Keep re-checking /api/resources for a bounded window. */
            if (await landedOnTargetNode()) {
              if (cancelled) return
              setRecord({ ...mine, step: 'done', status: 'done', error: undefined })
              onDoneRef.current()
              return
            }
            if (cancelled) return
            if (terminalRechecksRef.current < TERMINAL_RECHECK_BUDGET) {
              terminalRechecksRef.current += 1
              setPolledRecord(mine)
              pollRef.current = setTimeout(() => void poll(), PROVISION_POLL_MS)
              return
            }
          }
          setPolledRecord(mine)
          if (isTerminal(mine.status)) {
            if (mine.status === 'done') onDoneRef.current()
            return
          }
        } else if (sawRecordRef.current) {
          /* The record vanished: success clears it, every failure persists an error block; a mid-flight
          deprovision also vanishes it, so confirm in /api/resources before calling it done. */
          if (await landedInResources()) {
            if (cancelled) return
            setRecord(prev => (prev
              ? { ...prev, step: 'done', status: 'done', error: undefined }
              : { slug, config_dir: '', step: 'done', status: 'done' }))
            onDoneRef.current()
            return
          }
          if (cancelled) return
        }
      } catch {
        /* transient — keep polling */
      }
      if (!cancelled) pollRef.current = setTimeout(() => void poll(), PROVISION_POLL_MS)
    }

    void poll()
    return () => {
      cancelled = true
      if (pollRef.current) clearTimeout(pollRef.current)
    }
  }, [phase, slug])

  const terminal = isTerminal(record?.status)
  // The login process is waiting for the pasted authorization code.
  const awaitingCode = !terminal && record?.step === 'awaiting_code'

  // Phase 2: hand the pasted code to the waiting login, sealed to the node's `seal_key`. The poll drives the UI.
  async function submitCode(e: FormEvent) {
    e.preventDefault()
    const trimmed = code.trim()
    if (!trimmed || codeSubmitting) return
    const sealKey = record?.seal_key
    // No key to seal to (an old daemon's record): a dead end, never a code posted in the clear.
    if (!sealKey) {
      setSealError('no_key')
      return
    }
    // The node's `_OAUTH_CODE_RE`: it refuses the shape terminally, so catch a mistyped paste first.
    if (!/^[A-Za-z0-9._~#\/+=-]{1,512}$/.test(trimmed)) {
      setCodeError('Paste only the code the page showed — no spaces or URL.')
      return
    }
    setCodeSubmitting(true)
    setCodeError('')
    try {
      const sealed = await seal(sealKey, trimmed)
      await post('/api/commands', {
        command_type: submitCommand, args: { slug, sealed_code: sealed },
        ...(nodeId ? { node_id: nodeId } : {}),
      })
      setCode('')
      setCodeSent(true)
    } catch (err) {
      setCodeError(err instanceof ApiError ? String(err.detail ?? err.message) : 'Failed to submit the code')
    } finally {
      setCodeSubmitting(false)
    }
  }

  /* The URL in the clear: `record.sealed_url` opened with this page's pair, keyed on the blob (the poll
  replaces the record every 2s with the same blob). No pair is `'foreign'`; a rejected `unseal` is
  `'unopenable'`, and a later blob that opens clears either (a `'no_key'` from the code form stays). */
  const sealedUrl = record?.sealed_url
  useEffect(() => {
    setOauthUrl(null)
    if (!sealedUrl) return
    const pair = pairRef.current
    if (!pair) {
      setSealError('foreign')
      return
    }
    let cancelled = false
    unseal(pair, sealedUrl).then(
      url => {
        if (cancelled) return
        setOauthUrl(url)
        setSealError(prev => (prev === 'no_key' ? prev : null))
      },
      (err: unknown) => {
        if (cancelled) return
        // The error's name only, for support in devtools: never the blob or the URL.
        console.warn('login link could not be unsealed:', err instanceof Error ? err.name : typeof err)
        setOauthUrl(null)
        setSealError('unopenable')
      },
    )
    return () => { cancelled = true }
  }, [sealedUrl])

  /* Back to the form with nothing of this attempt: the next Start mints a new pair and cancels the old one. */
  function startAgain() {
    pairRef.current = null
    setCodeSent(false)
    setCode('')
    setRecord(null)
    setSealError(null)
    setSubmitError('')
    setPhase('input')
  }

  /* The one verdict on the authorization URL: `safeHref`, then the vendor anchor — a provider declaring
  `authorize_url_prefix` gets a link only when the URL starts with it (EQUALS it under `authorize_url_exact`).
  A non-default provider with no catalogue row has no anchor to apply, so it fails closed. */
  const unsealedHref = safeHref(oauthUrl)
  const vendorPrefix = providerRow?.authorize_url_prefix
  const offVendor = !!unsealedHref && typeof vendorPrefix === 'string' && vendorPrefix !== ''
    && (providerRow?.authorize_url_exact ? unsealedHref !== vendorPrefix : !unsealedHref.startsWith(vendorPrefix))
  const catalogueLost = !defaultLogin && !providerRow
  const oauthHref = offVendor || catalogueLost || sealError ? undefined : unsealedHref
  useEffect(() => {
    if (!oauthHref || !autoOpenRef.current) return
    autoOpenRef.current = false
    const tab = vendorTabRef.current
    vendorTabRef.current = null
    if (!tab || tab.closed) {
      window.open(oauthHref, '_blank', 'noopener,noreferrer')
      return
    }
    /* Navigated by a `noreferrer` link inside the tab itself, so the vendor is not sent this page's address. */
    try {
      const link = tab.document.createElement('a')
      link.href = oauthHref
      link.rel = 'noreferrer'
      tab.document.body.appendChild(link)
      link.click()
    } catch {
      // The tab left `about:blank` meanwhile (its document is not ours): replace it rather than lend it a referrer.
      tab.close()
      window.open(oauthHref, '_blank', 'noopener,noreferrer')
    }
  }, [oauthHref])
  /* No link is coming: the attempt dead-ended (`sealError`, an off-vendor or unchecked link) or finished without one. */
  const linkLost = !!sealError || offVendor || catalogueLost || terminal
  useEffect(() => {
    if (linkLost) dropVendorTab()
  }, [linkLost])
  // Nor when the page goes (Cancel, a route change) before the link lands.
  useEffect(() => () => dropVendorTab(), [])

  /* The device-authorization one-time code (RFC 8628), shape-checked before it is painted large beside a
  Copy button. Absent = a paste-code login; `''` (or a failed shape) = a device-auth adapter produced nothing,
  a defect shown as a warning. */
  const reportedCode = record?.user_code?.trim() ?? ''
  const deviceCode = DEVICE_CODE_SHAPE.test(reportedCode) ? reportedCode : ''
  const deviceCodeMissing = typeof record?.user_code === 'string' && !deviceCode

  /* Is this login finishing on the node (device auth)? The vendor's declared mode; a code on the record
  overrides an unresolvable catalogue row (only a device authorization produces one). */
  const redirectMode = awaitingCode && (!loginNeedsCode(providerRow) || (!providerRow && record?.user_code != null))

  /* Drive the wait, unless the CP says its workflow is driving: `*_await_login` answers `awaiting_redirect`
   * while the operator is still in their browser, so it is re-fired, paced by `PROVISION_POLL_MS` (the loop's
   * only rate limit), until the reply changes. Transient failures retry with backoff; refusals stop. */
  useEffect(() => {
    if (!redirectMode) return
    let cancelled = false
    let gap: ReturnType<typeof setTimeout> | null = null
    let inFlight = false
    const showError = (msg: string) => setAwaitError(prev => (prev === msg ? prev : msg))
    const pause = (ms: number) => new Promise<void>(resolve => { gap = setTimeout(resolve, ms) })

    async function driveAwaitLogin() {
      if (inFlight) return
      inFlight = true
      let failures = 0
      try {
        while (!cancelled) {
          let cmd: Command
          try {
            cmd = await post<Command>('/api/commands', {
              command_type: awaitCommand, args: { slug },
              ...(nodeId ? { node_id: nodeId } : {}),
            })
          } catch (err) {
            if (cancelled) return
            const detail = err instanceof ApiError ? String(err.detail ?? err.message) : 'the request failed'
            if (!(awaitDispatchIsTransient(err) && failures < AWAIT_RETRY_LIMIT)) {
              showError(`Cannot wait for the login to come back: ${detail}`)
              return
            }
            failures += 1
            showError(`Still waiting for the login to come back — ${detail}`)
            await pause(Math.min(PROVISION_POLL_MS * 2 ** (failures - 1), AWAIT_RETRY_MAX_GAP_MS))
            continue
          }
          if (cancelled) return
          failures = 0
          showError('')
          if (!shouldKeepDrivingAwait(cmd)) return
          await pause(PROVISION_POLL_MS)
        }
      } finally {
        inFlight = false
      }
    }

    void driveAwaitLogin()
    return () => {
      cancelled = true
      if (gap) clearTimeout(gap)
      setAwaitError(prev => (prev ? '' : prev))
    }
  }, [redirectMode, slug, awaitCommand, nodeId])

  return {
    isReauth, phase, started: phase !== 'input', startInFlight: phase === 'submitting', submitError, capRefused, record,
    terminal, awaitingCode, redirectMode, oauthHref, sealError, offVendor, catalogueLost, deviceCode,
    deviceCodeMissing, awaitError, code, setCode, codeError, codeSubmitting, codeSent, submitCode, begin, startAgain,
    slug,
  }
}

type LoginFlow = ReturnType<typeof useLoginFlow>

/* How long a finished login's success banner shows before the page closes itself. */
const AUTO_CLOSE_MS = 1500

/* The sign-in, drawn once a login has started: the vendor link, then either the code and Confirm
 * (paste-code) or the one-time code and a wait (device auth), and the outcome. Confirm turns into Close once the
 * code is sent; `autoClose` (the caller's say: its follow-up writes landed) closes the page after a success. */
function LoginProgress({ flow, doneLabel, autoClose }: { flow: LoginFlow; doneLabel: string; autoClose: boolean }) {
  const navigate = useNavigate()
  const { record, terminal } = flow
  const done = terminal && record?.status === 'done'
  useEffect(() => {
    if (!done || !autoClose) return
    const timer = setTimeout(() => navigate(ACCOUNTS_PATH), AUTO_CLOSE_MS)
    return () => clearTimeout(timer)
  }, [done, autoClose, navigate])
  const showCodeForm = !flow.redirectMode
    && (flow.codeSent || (flow.awaitingCode && !!flow.oauthHref && !flow.sealError))
  return (
    <Stack spacing={1.5} data-testid="account-sign-in">
      {flow.oauthHref && !terminal && !flow.codeSent && <AuthorizeLink url={flow.oauthHref} label="Authenticate ↗" inline />}

      {!terminal && (flow.sealError || flow.offVendor || flow.catalogueLost) && (
        <Stack spacing={1}>
          <Banner severity="error">
            {flow.sealError ? SEAL_LOST[flow.sealError] : flow.offVendor ? OFF_VENDOR_LINK : CATALOGUE_LOST}
          </Banner>
          <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
            <Button type="button" variant="contained" color="primary" onClick={flow.startAgain}>Start again</Button>
          </Box>
        </Stack>
      )}

      {flow.redirectMode && (
        <Stack spacing={0.5}>
          {flow.deviceCode ? (
            <>
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                Open the link on any device, then enter this code:
              </Typography>
              {/* Transcribed by hand across devices: display size, mono, tracked. */}
              <CopyableValue
                value={flow.deviceCode}
                copyLabel="Copy code"
                sx={{
                  flex: '0 1 auto', pl: 2, pr: 1, py: 1, fontSize: t => t.typography.h5.fontSize, fontWeight: FONT_WEIGHT.bold,
                  letterSpacing: LETTER_SPACING.caps, lineHeight: LINE_HEIGHT.snug,
                }}
              />
              <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                Keep this page open — it finishes on its own.
              </Typography>
            </>
          ) : flow.deviceCodeMissing && (
            <Banner severity="warning">
              The node reported no one-time code, so this login cannot be completed. Start it again; if it
              repeats, the node’s adapter for this provider is not reporting the code.
            </Banner>
          )}
          {flow.awaitError && <Banner severity="error">{flow.awaitError}</Banner>}
          <LoadingState label="Waiting for you to authorize…" />
        </Stack>
      )}

      {/* `oauthHref` too: a code is sealed only to a record whose link this page has opened. */}
      {showCodeForm && (
        <Box component="form" id="account-auth-code-form" onSubmit={e => void flow.submitCode(e)}>
          <Stack spacing={1}>
            {flow.codeError && <Banner severity="error">{flow.codeError}</Banner>}
            {/* Centred, with the button's `mt` matching the theme's dense field margin (8px top, 4px bottom),
                so the button's middle meets the input's. */}
            <FieldRow columnGap={grid(1)} rowGap={grid(2)} align="center">
              <DetailCell basis={grid(42.5)} max={grid(74.5)}>
                <FormTextField
                  label="Authorization code"
                  help="Finishes the sign-in; paste the code the browser shows."
                  id="account-auth-code"
                  autoFocus
                  value={flow.code}
                  onChange={flow.setCode}
                  placeholder="Paste the code"
                  disabled={flow.codeSubmitting || flow.codeSent}
                />
              </DetailCell>
              <DetailCell>
                {flow.codeSent ? (
                  <Button
                    component={RouterLink}
                    to={ACCOUNTS_PATH}
                    variant="contained"
                    color="primary"
                    data-testid="account-login-close"
                    sx={{ mt: 0.5 }}
                  >
                    Close
                  </Button>
                ) : (
                  <SubmitButton
                    busy={flow.codeSubmitting}
                    busyLabel="Confirming…"
                    disabled={!flow.code.trim()}
                    sx={{ mt: 0.5 }}
                  >
                    Confirm
                  </SubmitButton>
                )}
              </DetailCell>
            </FieldRow>
          </Stack>
        </Box>
      )}

      {!terminal && flow.codeSent && <LoadingState label="Finishing sign-in…" />}

      {!terminal && !flow.oauthHref && !flow.codeSent && !flow.sealError && !flow.offVendor && !flow.catalogueLost && (
        <LoadingState label="Waiting for login URL…" />
      )}

      {done && <Banner severity="success">{doneLabel}</Banner>}
      {terminal && !done && <Banner severity="error">{errorText(record?.error)}</Banner>}
    </Stack>
  )
}

/* The action row, one shape for every mode: Cancel (back to the roster), the primary verb. */
function ActionBar({ primary, cancelLabel = 'Cancel' }: {
  primary?: ReactNode
  cancelLabel?: string
}) {
  return (
    <FormActionBar testId="account-form-actions" stretchTestIds={['account-form-submit']}>
      <Button component={RouterLink} to={ACCOUNTS_PATH} variant="text" color="inherit" sx={tapTargetSx}>
        {cancelLabel}
      </Button>
      {primary}
    </FormActionBar>
  )
}

/* The action row under a running login; none once the code is sent (Close sits beside the code). */
function LoginActions({ flow }: { flow: LoginFlow }) {
  if (flow.codeSent) return null
  return <ActionBar cancelLabel={flow.terminal ? 'Close' : flow.redirectMode ? 'Back to accounts (keeps running)' : 'Cancel'} />
}

/* Apply a new login's held Auto-assign/cap/description once its row exists (`accountModel.tsx::applyPendingDispatch`). */
async function flushPendingDispatch(): Promise<string> {
  const rows = await listAccounts().catch(() => null)
  if (!rows) return ''
  return (await applyPendingDispatch(Object.fromEntries(rows.map(row => [row.id, row])))).failure
}

/* ── Shared by the three forms ────────────────────────────────────────────────────────────────────── */

const FORM_SX = { display: 'flex', flexDirection: 'column', gap: 1.5 } as const

/* An existing account's slug, provider and auth kind: shown in their rows, disabled (none has a change path). */
function LockedIdentityRow({ slug, provider, kind, providers, fallbackLabel }: {
  slug: string; provider: string; kind: AccountAuthKind; providers: ProviderChoice[]; fallbackLabel?: string
}) {
  return (
    <FieldRow rowGap={grid(2)}>
      <DetailCell basis={grid(29)}>
        {/* Never editable: a rename would move a live credential dir. A new account has none to show: the server
            draws it (`apps/api/app/account_slug.py`). */}
        <FormTextField
          label="Slug"
          help={SLUG_HELP}
          id="account-slug"
          value={slug}
          onChange={() => {}}
          disabled
          slotProps={{ htmlInput: { spellCheck: false, autoCapitalize: 'none' } }}
        />
      </DetailCell>
      <DetailCell basis={grid(32)}>
        <ProviderField value={provider} providers={providers} locked fallbackLabel={fallbackLabel} />
      </DetailCell>
      <DetailCell basis={grid(32)}>
        <AuthKindField value={kind} kinds={[]} disabled />
      </DetailCell>
    </FieldRow>
  )
}

/* An API-key account's key row and, for the default provider only (`workspaceId` non-null), its workspace row. */
function ApiKeyRows({ apiKey, onApiKey, workspaceId, onWorkspaceId, disabled, required, placeholder, align }: {
  apiKey: string; onApiKey: (v: string) => void; workspaceId: string | null; onWorkspaceId: (v: string) => void
  disabled: boolean; required?: boolean; placeholder?: string; align?: 'center' | 'start'
}) {
  return (
    <>
      <FieldRow rowGap={grid(2)} align={align}>
        <DetailCell basis={grid(48)} max={grid(85)}>
          <ApiKeyField label="API key" value={apiKey} onChange={onApiKey} disabled={disabled} required={required} placeholder={placeholder} />
        </DetailCell>
      </FieldRow>
      {workspaceId !== null && (
        <FieldRow rowGap={grid(2)}>
          <DetailCell basis={grid(48)} max={grid(85)}>
            <WorkspaceIdField value={workspaceId} onChange={onWorkspaceId} disabled={disabled} />
          </DetailCell>
        </FieldRow>
      )}
    </>
  )
}

/* Below the form once a login is polling: its progress, an optional error, and the actions. */
function LoginTail({ flow, doneLabel, autoClose, error }: { flow: LoginFlow; doneLabel: string; autoClose: boolean; error?: string }) {
  if (!flow.started || flow.phase !== 'polling') return null
  return (
    <>
      <LoginProgress flow={flow} doneLabel={doneLabel} autoClose={autoClose} />
      {error && <Banner severity="error">{error}</Banner>}
      <LoginActions flow={flow} />
    </>
  )
}

/* ══ New account ═══════════════════════════════════════════════════════════════════════════════════ */

function NewAccountForm({ nodes, providers, isOrgAdmin, canAdd, canSignIn }: {
  nodes: NodeChoice[] | null
  providers: ProviderChoice[]
  isOrgAdmin: boolean
  canAdd: boolean
  canSignIn: boolean
}) {
  const navigate = useNavigate()
  /* `null` = untouched, following provider + auth kind ("Claude Code Subscription"), so a new account is never
  named by its random slug. Cleared on purpose, it is left to the server (the slug). */
  const [nameEdit, setName] = useState<string | null>(null)
  const [description, setDescription] = useState('')
  // '' = no pick yet: the first offered provider stands in (the catalogue's default where allowed).
  const [providerPick, setProviderValue] = useState('')
  /* Offered providers: those with at least one kind the org allows and this seat may create. An unread
  catalogue offers the baseline alone. A pick the org does not allow (the default, say) yields to the first
  offered one. */
  const offeredProviders = providers.filter(p => offeredKinds(p, canAdd, canSignIn).length > 0)
  const providerValue = providers.length === 0 || offeredProviders.some(p => p.value === providerPick)
    ? providerPick
    : offeredProviders[0]?.value ?? providerPick
  const noneOffered = providers.length > 0 && offeredProviders.length === 0
  // Why nothing is offered: the org allows nothing, or this seat may create none of what it allows.
  const orgAllowsNone = !providers.some(p => (p.allowed_auth_kinds ?? p.auth_kinds).length > 0)
  const providerRow = providers.find(p => p.value === providerValue) ?? null
  const kinds = offeredKinds(providerRow, canAdd, canSignIn)
  /* The org allows a subscription here, but this member reaches no node to sign it in on. */
  const signInWithheld = canAdd && !canSignIn && allowedKinds(providerRow).includes('oauth')
  /* `?auth=api_key` (Jaah AI's "Add an Anthropic API key") lands on the key form, where the provider offers it. */
  const [params] = useSearchParams()
  const [kindPick, setKindPick] = useState<AccountAuthKind | null>(() => params.get('auth') === 'api_key' ? 'api_key' : null)
  /* The pick if the provider offers it, else the catalogue's default (OAuth wherever allowed) when offered,
  else the provider's first (a key-only vendor lands on its key). */
  const defaultKind = providerRow?.default_auth_kind
  const kind: AccountAuthKind = kindPick && kinds.includes(kindPick)
    ? kindPick
    : defaultKind && kinds.includes(defaultKind) ? defaultKind : kinds[0] ?? 'oauth'
  const name = nameEdit ?? `${providerRow?.label ?? 'Claude Code'} ${AUTH_KIND_LABEL[kind]}`
  const [apiKey, setApiKey] = useState('')
  const [workspaceId, setWorkspaceId] = useState('')
  const offersWorkspace = kind === 'api_key' && isDefaultProvider(providerValue)
  const [pickedNode, setPickedNode] = useState('')
  /* `null` = untouched, following the provider's default, so a change of provider re-seeds it. */
  const [autoAssignEdit, setAutoAssignEdit] = useState<boolean | null>(null)
  const autoAssign = autoAssignEdit ?? providerRow?.auto_assign_default ?? true
  const [maxRunners, setMaxRunners] = useState('')
  const hasCap = takesRunnerCap(providerRow, kind)
  const parsedMaxRunners = hasCap ? parseMaxRunners(maxRunners) : null
  /* The settings a login cannot carry (`account_add` writes none: Auto-assign, cap, description) are a
  follow-up `PUT` — an org admin's write — so only that seat is offered them on a subscription add. */
  const offersDispatch = kind === 'api_key' || isOrgAdmin
  const { busy: saving, formError: saveError, run } = useSubmit()
  /* An API-key add the community edition's account cap refused; the subscription path's is the flow's `capRefused`. */
  const [keyCapRefused, setKeyCapRefused] = useState<CapRefusal | null>(null)
  const [dispatchError, setDispatchError] = useState('')
  /* The held settings' write finished: only then may a finished login close the page (a failure stays shown). */
  const [dispatchSettled, setDispatchSettled] = useState(false)
  const [ownership, setOwnership] = useState<Ownership>('org')

  function pickProvider(value: string) {
    setProviderValue(value)
    setAutoAssignEdit(null)
    setMaxRunners('')
  }

  const knownNodes = nodes ?? []
  const addNodes = addLoginNodes(knownNodes, undefined, providerRow)
  const addBlocked = kind === 'oauth' && nodes !== null ? addBlockedReason(knownNodes, providerRow) : null
  /* Derived, not defaulted: the list can land after mount, and an add never leaves the host to the API while
  it can name one (the login's node is the account's pin). */
  /* A node on the accounts token folder mints a token every node of that NodeLink reads, so the pick matters
  little: it folds into Advanced and defaults to such a node. */
  const sharedNode = addNodes.find(n => n.accounts_nodelink)
  const nodeId = nodes === null ? ''
    : addNodes.some(n => n.node_id === pickedNode) ? pickedNode : (sharedNode ?? addNodes[0])?.node_id ?? ''

  const flow = useLoginFlow({
    mode: 'add',
    slug: '',
    name,
    nodeId,
    providerValue,
    defaultProvider: defaultProviderOf(providers),
    providerRow,
    ownership: isOrgAdmin ? ownership : undefined,
    onDone: () => { void flushPendingDispatch().then(error => { setDispatchError(error); setDispatchSettled(true) }) },
  })
  /* Locked once a start has drawn a slug too: a restart reuses it, and its row already holds the first name
  and owner (a different owner on the restart is refused). */
  const locked = saving || flow.started
  const nameLocked = locked || !!flow.slug

  const keyEntered = apiKey.trim().length > 0
  const canSubmit = parsedMaxRunners !== undefined && !locked && kinds.length > 0
    && (kind === 'api_key' ? keyEntered && (!offersWorkspace || workspaceIdValid(workspaceId)) : !addBlocked)

  const capHit = keyCapRefused ?? flow.capRefused
  /* The community edition's cap, already full: said before anything is typed, in the refusal an add would meet. */
  const capFull = useCeLimits().data?.accounts.refusal ?? null

  async function submit() {
    if (!canSubmit) return
    if (kind === 'oauth') {
      /* Anything but what the row is born with (Auto-assign on, no cap) is written once the row exists, held
      under the slug the start drew. The row lands only when the pasted code completes, after this. */
      const started = await flow.begin()
      if (!started) return
      // A restart reuses the slug: what an earlier start held for it must not outlive this answer.
      dropPendingDispatch(started)
      if (offersDispatch && (!autoAssign || parsedMaxRunners != null || description.trim())) {
        holdPendingDispatch(started, {
          auto_assign: autoAssign, max_runners: parsedMaxRunners ?? null, description: description.trim(),
        })
      }
      return
    }
    // The vendor's own refusal ("… rejected this API key") in the API's words; the form keeps what was typed.
    setKeyCapRefused(null)
    await run('Failed to create the account', async () => {
      // `provider` rides only for a non-default vendor (absent means the default server-side).
      // No slug: the server draws it. A blank name is left to the server (the slug).
      const body: Record<string, unknown> = { auth_kind: 'api_key', api_key: apiKey.trim(), auto_assign: autoAssign }
      if (name.trim()) body.name = name.trim()
      if (description.trim()) body.description = description.trim()
      if (offersWorkspace && workspaceId.trim()) body.workspace_id = workspaceId.trim()
      if (providerValue && providerValue !== defaultProviderOf(providers)) body.provider = providerValue
      if (parsedMaxRunners != null) body.max_runners = parsedMaxRunners
      if (isOrgAdmin) body.ownership = ownership
      try {
        await post<Account>('/api/accounts', body)
      } catch (err) {
        const cap = capRefusal(err)
        if (!cap) throw err
        setKeyCapRefused(cap)
        return
      }
      setApiKey('')
      navigate(ACCOUNTS_PATH)
    })
  }

  const nodeRow = (
    <FieldRow rowGap={grid(2)}>
      <DetailCell basis={grid(42.5)} max={grid(69)}>
        <NodeField
          value={nodeId}
          onChange={setPickedNode}
          nodes={addNodes}
          disabled={locked}
          help={addNodeHelp(providerRow)}
          emptyHelp={nodes === null ? NODES_UNREAD_HELP : NODES_EMPTY_HELP}
        />
      </DetailCell>
    </FieldRow>
  )
  /* A login that ended without the row, or was restarted, will never apply what was held for it. */
  const loginFailed = flow.terminal && flow.record?.status !== 'done'
  const flowSlug = flow.slug
  useEffect(() => {
    if (loginFailed && flowSlug) dropPendingDispatch(flowSlug)
  }, [loginFailed, flowSlug])
  if (capFull && !flow.started) return <AccountCapLine refusal={capFull} />
  return (
    <>
      <FormBox onSubmit={submit} sx={FORM_SX}>
        {capHit ? <AccountCapLine refusal={capHit} />
          : (saveError || flow.submitError) && <Banner severity="error">{saveError || flow.submitError}</Banner>}
        {noneOffered && (
          <Banner severity="warning">
            {orgAllowsNone ? <>{isEnabled('billing') ? 'No provider is on your plan.' : 'Every provider is off.'} <PlanRemedy /></> : 'You can’t add any allowed account. Ask an admin.'}
          </Banner>
        )}
        {/* Provider · Authentication: narrow enough to share one line, wrapping only below it. */}
        <FieldRow rowGap={grid(2)} testId="account-identity-row">
          <DetailCell basis={grid(26.5)}>
            <ProviderField
              value={providerValue}
              onChange={pickProvider}
              providers={offeredProviders}
              disabled={locked}
              fallbackLabel="Claude Code"
            />
          </DetailCell>
          <DetailCell basis={grid(26.5)}>
            <AuthKindField value={kind} onChange={setKindPick} kinds={kinds} disabled={locked} />
          </DetailCell>
        </FieldRow>
        <FieldRow rowGap={grid(2)}>
          <DetailCell basis={grid(48)} max={grid(85)}>
            <NameField value={name} onChange={setName} disabled={nameLocked} />
          </DetailCell>
        </FieldRow>
        {!noneOffered && <HiddenProvidersNote providers={providers} />}
        {signInWithheld && (
          <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="account-sign-in-needs-project">{SIGN_IN_NEEDS_PROJECT}</Typography>
        )}
        <ProviderLimitations provider={nonBaselineRow(providerRow)} />
        <NonBaaWarning surface={nonBaaSurfaceFor(providerValue, kind)} />
        {kind === 'api_key'
          ? <ApiKeyRows apiKey={apiKey} onApiKey={setApiKey} workspaceId={offersWorkspace ? workspaceId : null} onWorkspaceId={setWorkspaceId} disabled={locked} required />
          : !sharedNode && nodeRow}
        {/* A refusal may name a field folded in here (a name too long): open, so the operator can fix it. */}
        <AdvancedSection testId="account-advanced" forceOpen={!!(saveError || flow.submitError)}>
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, pt: 1 }}>
            {isOrgAdmin && (
              <OwnershipRadios
                labelId="account-ownership-label"
                options={ACCOUNT_OWNERS}
                value={ownership}
                onChange={next => setOwnership(next === 'personal' ? 'personal' : 'org')}
                disabled={nameLocked}
              />
            )}
            {offersDispatch && <DescriptionField value={description} onChange={setDescription} disabled={locked} />}
            {kind === 'oauth' && sharedNode && nodeRow}
            <DispatchFields dispatch={offersDispatch} autoAssign={autoAssign} onAutoAssign={setAutoAssignEdit} maxRunners={hasCap ? maxRunners : null}
              onMaxRunners={setMaxRunners} apiKey={kind === 'api_key'} locked={locked} providerRow={providerRow} limitations={false} />
          </Box>
        </AdvancedSection>
        {addBlocked && !flow.started && (
          <Banner severity="warning">{addBlocked.reason} {addBlocked.remedy}</Banner>
        )}
        {flow.phase !== 'polling' && (
          <ActionBar
            primary={<SubmitButton testId="account-form-submit" disabled={!canSubmit}>{kind === 'api_key' ? (saving ? 'Adding…' : 'Add') : (flow.startInFlight ? 'Starting…' : 'Authenticate ↗')}</SubmitButton>}
          />
        )}
      </FormBox>
      <LoginTail flow={flow} doneLabel="Account added and enabled." autoClose={dispatchSettled && !dispatchError} error={dispatchError} />
    </>
  )
}

/* ══ Edit account ══════════════════════════════════════════════════════════════════════════════════
 * `PUT /api/accounts/{id}` carries only what was touched (`exclude_unset`): the record is re-read under the
 * form, so echoing a snapshot would undo another operator's write. */

function EditAccountForm({ record, providers, readOnly }: {
  record: Account
  providers: ProviderChoice[]
  readOnly: boolean
}) {
  const isApiKey = record.auth_kind === 'api_key'
  const providerRow = providers.find(p => p.value === record.provider) ?? null
  /* `null` = untouched, mirroring the live record; only a change is sent. */
  const [nameEdit, setNameEdit] = useState<string | null>(null)
  const name = nameEdit ?? record.name
  const [descriptionEdit, setDescriptionEdit] = useState<string | null>(null)
  const description = descriptionEdit ?? record.description
  const [apiKeyEdit, setApiKeyEdit] = useState('')
  const offersWorkspace = isApiKey && isDefaultProvider(record.provider)
  const [workspaceIdEdit, setWorkspaceIdEdit] = useState<string | null>(null)
  const workspaceId = workspaceIdEdit ?? record.workspace_id ?? ''
  const [autoAssignEdit, setAutoAssignEdit] = useState<boolean | null>(null)
  const autoAssign = autoAssignEdit ?? record.auto_assign
  const hasCap = takesRunnerCap(providerRow, record.auth_kind)
  /* `null` = untouched: mirrors the live record, and is not sent (another operator's cap change survives). */
  const [maxRunnersEdit, setMaxRunnersEdit] = useState<string | null>(null)
  const maxRunners = maxRunnersEdit ?? (record.max_runners != null ? String(record.max_runners) : '')
  const setMaxRunners = (v: string) => setMaxRunnersEdit(v)
  const parsedMaxRunners = hasCap ? parseMaxRunners(maxRunners) : null
  /* The type and a personal account's owner; `null` = untouched. */
  const { admin } = useAuth()
  const savedOwnership: Ownership = record.ownership === 'personal' ? 'personal' : 'org'
  const [ownershipEdit, setOwnershipEdit] = useState<Ownership | null>(null)
  const ownership = ownershipEdit ?? savedOwnership
  const [ownerEdit, setOwnerEdit] = useState<OwnerMember | null>(null)
  const owner = ownerEdit ?? (record.owner_id ? { key: record.owner_id, label: record.owner ?? record.owner_id } : null)
  const { busy: saving, formError: saveError, run } = useSubmit()
  const locked = saving || readOnly
  const canSave = !locked && parsedMaxRunners !== undefined && name.trim() !== ''
    && (!offersWorkspace || workspaceIdValid(workspaceId))

  /* What a Save sends: only what was touched and differs from the live record. Non-empty is "dirty", which the leave
     gate asks about. */
  function changes(): Record<string, unknown> {
    const body: Record<string, unknown> = {}
    if (nameEdit !== null && nameEdit.trim() !== record.name) body.name = nameEdit.trim()
    // `''` clears it; `null` would be dropped server-side as "leave alone".
    if (descriptionEdit !== null && descriptionEdit.trim() !== record.description) body.description = descriptionEdit.trim()
    // Blank means unchanged; trimmed, since a pasted trailing newline authenticates nowhere.
    const rotatedKey = apiKeyEdit.trim()
    if (isApiKey && rotatedKey) body.api_key = rotatedKey
    // `''` clears it.
    if (offersWorkspace && workspaceIdEdit !== null && workspaceIdEdit.trim() !== (record.workspace_id ?? '')) {
      body.workspace_id = workspaceIdEdit.trim()
    }
    if (autoAssignEdit !== null && autoAssignEdit !== record.auto_assign) body.auto_assign = autoAssignEdit
    if (ownership !== savedOwnership) body.ownership = ownership
    if (ownership === 'personal' && owner !== null && owner.key !== (record.owner_id ?? null)) {
      body.ownership = ownership
      body.owner_user_id = owner.key
    }
    // `null` clears the cap back to the org default (a subscription) or no limit (an API key).
    if (hasCap && maxRunnersEdit !== null && parsedMaxRunners !== (record.max_runners ?? null)) {
      body.max_runners = parsedMaxRunners
    }
    return body
  }
  const dirty = !readOnly && Object.keys(changes()).length > 0

  /* The form's Save, and the leave dialog's: true once the write landed (or there was nothing to write). */
  async function save(): Promise<boolean> {
    if (!canSave) return false
    const landed = await run('Failed to save the account', async () => {
      const body = changes()
      // Nothing touched: nothing to write.
      if (Object.keys(body).length) await put(`/api/accounts/${record.id}`, body)
      setApiKeyEdit('')
    })
    return landed !== null
  }

  // Leaving with unsaved edits asks first (`hooks/useLeaveGuard`).
  const guard = useLeaveGuard(dirty, save)

  async function submit() {
    if (await save()) guard.leaveNow(ACCOUNTS_PATH)
  }

  return (
    <FormBox onSubmit={submit} sx={FORM_SX}>
      {saveError && <Banner severity="error">{saveError}</Banner>}
      {readOnly && <Banner severity="info">Read-only — only an administrator can change an account.</Banner>}
      <FieldRow rowGap={grid(2)}>
        <DetailCell basis={grid(37)} max={grid(64)}>
          <NameField value={name} onChange={setNameEdit} disabled={locked} required />
        </DetailCell>
      </FieldRow>
      <FieldRow rowGap={grid(2)}>
        <DetailCell basis={grid(48)} max={grid(106.5)}>
          <DescriptionField value={description} onChange={setDescriptionEdit} disabled={locked} readOnly={readOnly} />
        </DetailCell>
      </FieldRow>
      {!readOnly && (
        <FieldRow rowGap={grid(2)}>
          <DetailCell basis={grid(48)} max={grid(106.5)}>
            <OwnershipRadios
              labelId="account-ownership-label"
              options={ACCOUNT_OWNERS}
              value={ownership}
              onChange={next => setOwnershipEdit(next === 'personal' ? 'personal' : 'org')}
              disabled={locked}
              personalOwner={<OwnerMemberLookup value={owner} onChange={setOwnerEdit} selfId={admin?.id} disabled={locked} />}
            />
          </DetailCell>
        </FieldRow>
      )}
      <LockedIdentityRow slug={record.slug} provider={record.provider} kind={isApiKey ? 'api_key' : 'oauth'} providers={providers} />
      {isApiKey && (
        <ApiKeyRows apiKey={apiKeyEdit} onApiKey={setApiKeyEdit} workspaceId={offersWorkspace ? workspaceId : null} onWorkspaceId={setWorkspaceIdEdit}
          disabled={locked} placeholder={record.api_key_set ? API_KEY_KEEP_PLACEHOLDER : API_KEY_ABSENT_PLACEHOLDER} align="center" />
      )}
      <DispatchFields dispatch autoAssign={autoAssign} onAutoAssign={setAutoAssignEdit} maxRunners={hasCap ? maxRunners : null}
        onMaxRunners={setMaxRunners} apiKey={isApiKey} locked={locked} providerRow={providerRow} />
      {/* Who touched this record (names only: `Account` carries no timestamps). Neither known: a row of two
      em-dashes says nothing, so there is no row. */}
      {(record.created_by || record.updated_by) && (
        <MetaRow>
          <MetaActors createdBy={record.created_by} updatedBy={record.updated_by} />
        </MetaRow>
      )}
      <ActionBar primary={readOnly ? undefined : <SubmitButton testId="account-form-submit" busy={saving} disabled={!canSave}>Save</SubmitButton>} />
      <LeaveGuardDialog leave={guard} message="This account has changes that have not been saved. Save them before leaving?" />
    </FormBox>
  )
}

/* ══ Sign an existing account in: re-login, or enrol it on a node it is missing from ═══════════════════
 * Re-login targets the dead session's node; an enrolment's node is fixed by the row it was opened from. Its provider is the
 * account's own (immutable), deciding how the login finishes. */

function LoginAccountForm({ slug, enrol, nodeParam, account, record, nodes, providers }: {
  slug: string
  enrol: boolean
  nodeParam: string
  account: ClaudeAccount | undefined
  record: Account | undefined
  nodes: NodeChoice[] | null
  providers: ProviderChoice[]
}) {
  const knownNodes = nodes ?? []
  const providerValue = record?.provider ?? defaultProviderOf(providers)
  const providerRow = providers.find(p => p.value === providerValue) ?? null
  /* An enrolment with no node named is "Add to node": the nodes this account is not on, none of them a host
  (a host runs no work). */
  const pickEnrol = enrol && !nodeParam
  const missingFrom = new Set((account?.nodes ?? [])
    .filter(n => n.present === false && n.hosting !== 'host')
    .map(n => n.node_id))
  const nodeChoices = pickEnrol ? knownNodes.filter(n => missingFrom.has(n.node_id))
    : enrol ? knownNodes.filter(n => n.node_id === nodeParam) : knownNodes
  const defaultNode = (): string => pickEnrol ? (nodeChoices[0]?.node_id ?? '')
    : account?.node_id ?? ''
  const [pickedNode, setPickedNode] = useState(() => nodeParam || defaultNode())
  /* The default can only be computed once the node list lands; follow it until the operator picks. */
  const [nodeTouched, setNodeTouched] = useState(!!nodeParam)
  const nodeId = enrol && !pickEnrol ? nodeParam : pickedNode
  /* The enrolment's pin may be a host `/api/nodes` did not list; it is still the node, shown as itself. */
  const enrolNodes = enrol && nodeChoices.length === 0 && nodeParam
    ? [{
        node_id: nodeParam, name: account?.nodes?.find(n => n.node_id === nodeParam)?.node_name ?? nodeParam,
        runner_clis: null,
      }]
    : nodeChoices

  const flow = useLoginFlow({
    mode: enrol ? 'add' : 'reauth',
    slug,
    nodeId,
    providerValue,
    defaultProvider: defaultProviderOf(providers),
    providerRow,
    requirePresentOnNode: enrol ? nodeId : undefined,
    onDone: () => {},
  })
  /* Frozen once the login starts: a refreshed account must not re-point the code or the wait at a different
  node from the one the login runs on. */
  useEffect(() => {
    if (!nodeTouched && nodes && !flow.started) setPickedNode(defaultNode())
    // `defaultNode` reads only the inputs listed here.
  }, [nodes, account, nodeTouched, flow.started])
  const locked = flow.started
  const binaryWarning = nodes !== null ? binaryAttestationWarning(knownNodes, nodeId, providerRow) : null
  const doneWithLogin = flow.terminal && flow.record?.status === 'done'

  return (
    <>
      <FormBox onSubmit={() => { if (!locked) void flow.begin() }} sx={FORM_SX}>
        {flow.submitError && <Banner severity="error">{flow.submitError}</Banner>}
        {/* Only the default's name is known without the catalogue; any other stored value shows as itself. */}
        <LockedIdentityRow slug={slug} provider={providerValue} kind="oauth" providers={providers}
          fallbackLabel={isDefaultProvider(providerValue) ? 'Claude Code' : undefined} />
        <NonBaaWarning surface={nonBaaSurfaceFor(providerValue, 'oauth')} />
        <FieldRow rowGap={grid(2)}>
          <DetailCell basis={grid(42.5)} max={grid(69)}>
            <NodeField
              value={nodeId}
              onChange={v => { setNodeTouched(true); setPickedNode(v) }}
              nodes={enrol && !pickEnrol ? enrolNodes : nodeChoices}
              disabled={locked || (enrol && !pickEnrol)}
              allowUnpinned={!enrol}
              help={pickEnrol ? ADD_NODE_HELP : enrol ? ENROL_NODE_HELP : REAUTH_NODE_HELP}
              emptyHelp={nodes === null ? NODES_UNREAD_HELP : NODES_EMPTY_HELP}
            />
          </DetailCell>
        </FieldRow>
        {binaryWarning && !flow.started && <Banner severity="warning">{binaryWarning}</Banner>}
        {flow.phase !== 'polling' && (
          <ActionBar
            primary={<SubmitButton testId="account-form-submit" disabled={locked || (enrol && !nodeId)}>{flow.startInFlight ? 'Starting…' : 'Authenticate ↗'}</SubmitButton>}
          />
        )}
      </FormBox>
      <LoginTail flow={flow} doneLabel={enrol ? 'Signed in on this node.' : 'Re-login complete — credentials refreshed.'} autoClose={doneWithLogin} />
    </>
  )
}

/* ══ The page ══════════════════════════════════════════════════════════════════════════════════════ */

type PageData = {
  resources: ClaudeResources | null
  records: Account[] | null
  nodes: NodeChoice[] | null
}

async function loadPageData(): Promise<PageData> {
  const [resources, records, nodes] = await Promise.all([
    getResources().catch(() => null),
    listAccounts().catch(() => null),
    fetchNodeChoices(),
  ])
  return { resources, records, nodes }
}

type AccountTab = 'details' | 'access'
const ACCOUNT_TABS: readonly TabDef<AccountTab>[] = [{ id: 'details', label: 'Details' }, { id: 'access', label: 'Access' }]

/** New account's two doors: the form here, or a file exported from another console (`AccountTransfer.tsx`). */
const VIA_CHOICES: readonly Choice<'new' | 'import'>[] = [
  { value: 'new', label: 'Set up here', help: null, testId: 'account-new-via-new' },
  { value: 'import', label: 'Import a file', help: 'An account exported from another console', testId: 'account-new-via-import' },
]

export default function AccountEditPage() {
  const { id, slug: loginSlug } = useParams()
  const location = useLocation()
  const [params, setParams] = useSearchParams()
  const { admin } = useAuth()
  const isOrgAdmin = isOrgAdminSeat(admin)
  const canLogIn = useCanLogIn()
  const canAdd = useCanAddAccount()
  const canSignIn = useCanSignInNewAccount()
  const catalogue = useAgentProviders()
  // The operator's "Change org…": an existing account only, gated inside the hook.
  const changeOrg = useChangeOrgAction('account')
  const mode: 'new' | 'edit' | 'login' = loginSlug != null ? 'login'
    : location.pathname.replace(/\/+$/, '') === '/accounts/new' ? 'new' : 'edit'
  const enrol = params.get('enrol') === '1'
  // The tab lives in the URL (hooks/useUrlTab.ts).
  const [accountTab, selectAccountTab] = useUrlTab(ACCOUNT_TABS, 'details')
  const nodeParam = params.get('node') ?? ''

  /* Every mode reads the same three lists (each part falls back to null on its own), keyed by the route's id and
     login slug, so a new one re-reads. The form stays on the last answer until the next lands (`!data` only). */
  const [data, setData] = useState<PageData | null>(null)
  useEditRecord(
    `${id ?? ''}|${loginSlug ?? ''}`,
    loadPageData,
    setData,
    { loadFallback: 'The account list did not load. Reload to try again.' },
  )
  /* Another operator's write lands under the open form (an `account` event re-reads the records): an
  untouched control mirrors the live row, and nothing untouched is sent back (`EditAccountForm`). */
  const { lastEvent } = useEventStream('/api/stream', ['account'])
  useEffect(() => {
    if (!lastEvent) return
    let active = true
    listAccounts().then(
      records => { if (active) setData(prev => (prev ? { ...prev, records } : prev)) },
      () => { /* keep what the form shows */ },
    )
    return () => { active = false }
  }, [lastEvent])

  const record = mode === 'edit' ? data?.records?.find(r => r.id === id)
    : mode === 'login' ? data?.records?.find(r => r.slug === loginSlug) : undefined
  const account = mode === 'login' ? data?.resources?.accounts.find(a => slugOf(a) === loginSlug) : undefined
  /* Another org's account, for an operator: edited on that org's behalf. Every write below it — Save,
     the enabled toggle, Export — goes as its org for the one request (`lib/actAs.ts::useActAsRows`), the Access tab's
     grants too (`actAsOnce`), and `ActingForNotice` says so. Change org… stays the operator's own verb. */
  const otherOrg = useOtherOrg(mode === 'edit' ? record?.org : null)
  useActAsRows(mode === 'edit' && record ? [{ path: `/api/accounts/${record.id}`, org: otherOrg }] : [])
  const changeOrgButton = mode === 'edit' && record
    ? changeOrg.button({ id: record.id, name: record.name, href: accountEditPath(record.id) }, { strip: true })
    : null
  const setRecordEnabled = (enabled: boolean) => setData(prev => prev && {
    ...prev,
    records: prev.records?.map(r => (r.id === id ? { ...r, enabled } : r)) ?? null,
  })
  // Export is an administrator's act: the bundle carries the key (`require_account_writer`).
  const exportAction = useExportAccountAction(mode === 'edit' && isOrgAdmin ? record : undefined)
  /* On the tab strip: a phone's roster row has no toggle (its ⋯ does), so the account's page reaches it in one tap. */
  const [enabledError, setEnabledError] = useState('')
  const enabledToggle = mode === 'edit' && record && isOrgAdmin
    ? (
      <EnableToggle
        enabled={record.enabled}
        write={next => setAccountEnabled(record.id, next)}
        onToggled={next => { setEnabledError(''); setRecordEnabled(next) }}
        onError={setEnabledError}
        failMessage={next => `Failed to ${next ? 'enable' : 'disable'} the account`}
      />
    )
    : null

  const agentsTab = useDirectoryTabs().find(t => t.id === 'accounts')
  const title = mode === 'new' ? 'New account'
    : mode === 'edit' ? record?.name || record?.slug || 'Account'
    : enrol && !nodeParam ? `Add ${record?.name || loginSlug} to a node`
    : enrol ? `Add ${record?.name || loginSlug} on ${shortNode(
      account?.nodes?.find(n => n.node_id === nodeParam)?.node_name
        ?? data?.nodes?.find(n => n.node_id === nodeParam)?.name ?? nodeParam)}`
    : `Re-login ${record?.name || loginSlug}`
  /* Rooted at the Directory tab the strip shows (Agents), as People and Secrets' editors are. Its first sub-tab is
     this roster under the same name, so one crumb names both levels and links to the roster itself. */
  const crumbs = (
    <Breadcrumb items={[{ label: agentsTab?.label ?? 'AI Accounts', to: ACCOUNTS_PATH }, { label: title }]} />
  )

  /* Before the record (every mode reads the same lists): loading, the list that did not load, or a missing row —
     under the trail alone (`ui/detail/DetailStatus`), as the other edit pages. */
  if (!data || !catalogue.settled) return <DetailStatus trail={crumbs} loading />
  if (mode === 'edit' && data.records === null) {
    return <DetailStatus trail={crumbs} error="The account list did not load. Reload to try again." />
  }
  if (mode === 'edit' && !record) {
    return <DetailStatus trail={crumbs} notFound="Account not found" locate={{ objectType: 'account', objectId: id }} />
  }

  /* The record's tabs sit on the page, outside any card. Each panel carries its own card, as `GroupEditPage`'s do. */
  const tabbed = mode === 'edit'
  let body: ReactNode
  if (mode === 'new') {
    /* Import a file is offered where the roster's Import is (`canLogIn`); the import route decides who may. */
    const via = canLogIn && params.get('via') === 'import' ? 'import' : 'new'
    body = !canAdd
      ? <EmptyState title="Not allowed" />
      : (
        <Box sx={FORM_SX}>
          {canLogIn && (
            <ChoiceField label="Add by" row value={via} options={VIA_CHOICES} testId="account-new-via"
              onChange={next => setParams(next === 'import' ? { via: 'import' } : {}, { replace: true })} />
          )}
          {via === 'import' ? <ImportAccountForm providers={catalogue.rows} /> : (
            <NewAccountForm
              nodes={data.nodes}
              providers={catalogue.rows}
              isOrgAdmin={isOrgAdmin}
              canAdd={canAdd}
              canSignIn={canSignIn}
            />
          )}
        </Box>
      )
  } else if (mode === 'edit' && record) {
    body = (
      <>
        <ActingForNotice org={otherOrg} testId="account-acting-for" />
        {enabledError && <Banner severity="error" testId="account-enabled-error">{enabledError}</Banner>}
        {/* "Change org…" is a record verb, so it sits on the record's tab strip. `compactActions` +
            `overflowMenu`: the verb stays on the tabs' row while both fit (a 390px phone) and wraps under them only
            when they do not (an empty compact slot takes no room). */}
        <Tabs
          tabs={ACCOUNT_TABS}
          tab={accountTab}
          onSelect={selectAccountTab}
          label="Account sections"
          idPrefix="account"
          actions={(enabledToggle || exportAction.button || changeOrgButton) && (
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
              {enabledToggle}{exportAction.button}{changeOrgButton}
            </Box>
          )}
          compactActions
          overflowMenu
          sticky="default"
        />
        <TabPanel id="details" current={accountTab} idPrefix="account">
          <Box sx={{ mt: 1 }}>
            <SectionCard>
              <EditAccountForm key={record.id} record={record} providers={catalogue.rows} readOnly={!isOrgAdmin} />
            </SectionCard>
          </Box>
        </TabPanel>
        {/* Mounted only with its tab: it fetches. */}
        <TabPanel id="access" current={accountTab} idPrefix="account">
          <Box sx={{ mt: 1 }}>
            <SectionCard>
              {accountTab === 'access' && <AccessTab scope={{ type: 'account', id: record.id }} otherOrg={otherOrg} />}
            </SectionCard>
          </Box>
        </TabPanel>
      </>
    )
  } else {
    body = !canLogIn
      ? <EmptyState title="Not allowed" />
      : record?.auth_kind === 'api_key'
        ? <EmptyState title="Nothing to sign in" />
        /* Add to node with nowhere to add it — never an unpinned add: no node that runs work lacks it. */
        : enrol && !nodeParam && !(account?.nodes ?? []).some(
          n => n.present === false && n.hosting !== 'host')
          ? <EmptyState title="No node to add it to" />
        : (
          <LoginAccountForm
            key={`${loginSlug}|${nodeParam}|${enrol ? 'enrol' : 'reauth'}`}
            slug={loginSlug ?? ''}
            enrol={enrol}
            nodeParam={nodeParam}
            account={account}
            record={record}
            nodes={data.nodes}
            providers={catalogue.rows}
          />
        )
  }

  return (
    // The form keeps its reading measure; the Access table takes the page width, or its columns scroll away.
    <DetailPage
      breadcrumb={crumbs}
      title={title}
      sx={mode === 'edit' && accountTab === 'access' ? undefined : PAGE_MEASURE_SX}
      testId="account-page"
    >
      {tabbed ? body : <SectionCard>{body}</SectionCard>}
      {changeOrg.dialog}
      {exportAction.dialog}
    </DetailPage>
  )
}
