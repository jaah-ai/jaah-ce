import { useState } from 'react'
import { Link as RouterLink, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import MuiLink from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { apiDetail, ApiError } from '../../api'
import { useAuth } from '../../auth'
import { type ConfirmOptions, danger, useConfirm } from '../../components/ConfirmDialog'
import { useApiRead } from '../../hooks/useApiRead'
import { useBusyIds } from '../../hooks/useBusyIds'
import { shortSha } from '../../lib/shortSha'
import type { Account } from '../../types'
import Banner from '../../ui/Banner'
import ListStates from '../../ui/feed/ListStates'
import type { FeedRowSpec, OverflowItem } from '../../ui/feed/types'
import FormDialog from '../../ui/FormDialog'
import { PHONE, tapTargetSx } from '../../ui/listStyles'
import { ORG_SLOT, OrgIndicator, useOrgVisible } from '../../ui/OrgIndicator'
import { StatusChip, type Tone } from '../../ui/StatusChip'
import { listAccounts } from '../credentials/api'
import PageHeader from '../../ui/PageHeader'
import { RegistryOffPlanNote, useAgentsVerbs } from './agentsTabs'
import {
  ACP_REGISTRY,
  type Agent,
  agentTypeLabel,
  deleteAgent,
  DISABLED,
  type InUse,
  inUseOf,
  languageLabel,
  listAgents,
  refreshAgent,
  REMOVED,
  updateAgent,
} from './api'
import { type Notice, NoticeBanner } from './agentFields'
import { runWithNotice } from './noticeAction'
import NewButton from '../../ui/NewButton'

/* ── CUSTOMIZE > AGENTS: THE ORG'S SDK AGENTS (AC-9) ───────────────────────────────────────────────────────
   `GET /api/agents` — the org's own agents, then the catalog connectors it enabled. Register takes a git repo
   URL, an optional sub-path and a ref on the agent's page (`AgentEditPage`, `/agents/new` and `/agents/:id`, beside
   this list), which a row's name opens in every state. A row's ⋯
   refreshes (re-resolve the ref; a failure keeps the last good snapshot and shows as `resolve_error`), disables
   or re-enables, and deletes — a 409 names who still uses it, links the first few, and offers Disable instead.
   An agent's `env` (name → value, e.g. a connector's model) is set on its process at launch; not secrets.
   Writes are `agents:edit`; a holder on one project only changes their own registrations, which the server
   enforces (`_require_editor`) and this page surfaces as the refusal it sends. A connector's row is read-only
   here: it is turned on or off on the Catalog tab. Names wrap to two lines rather than clip (AC-9).
   An operator's Orgs filter narrows the list (the read seam); another org's agent carries its org and no
   actions, since writes stay on the operator's own org. An ACP registry agent (`source`, installed on the
   Registry tab) is pinned at install: its row says "ACP registry · pinned v<x>" in place of commit facts, its
   status reads Active or Disabled like any other, and it offers Disable and Delete only — no ref
   to edit, nothing to refresh (the server refuses both). Its row also names its key account ("Key: <name>", or
   "No key account"): an org API-key account whose key reaches the agent through ACP `authenticate` at run time,
   never env. An org admin binds or unbinds it on the agent's page (its Key account field, `PATCH key_account_id`),
   choosing among the org's enabled API-key accounts, the agent's own vendor's first (`vendorMatches`); the accounts
   load here only while a registry agent is listed. */

const ACTION_ICON = { refresh: '⟳', disable: '⏸', enable: '▶', delete: '🗑' } as const

/** Bindable to a registry agent (`routes/agents.py` refuses any other). */
export const bindable = (account: Account) => account.auth_kind === 'api_key' && account.enabled

export const pinned = (agent: Agent) => agent.source === ACP_REGISTRY

/** The agent's page: its name opens it from the list in every state. */
export const agentPath = (id: string) => `/agents/${encodeURIComponent(id)}`

/** The delete confirm, the list's and the page's. */
export const deleteAgentConfirm = (agent: Agent): ConfirmOptions => danger('Delete', {
  title: `Delete ${agent.display_name}?`,
  message: 'Its registration is removed for everyone. Past runs keep their record.',
})

/** What an SDK v1 agent's owner does about it, said on the row, not only on hover. */
export const UNSUPPORTED_FIX = 'Re-register on SDK v2'
/** An SDK v1 agent's mark: its state chip's label, and its row's pill. */
export const UNSUPPORTED = 'Unsupported'

export function statusOf(agent: Agent): { tone: Tone; glyph: string; label: string } {
  if (agent.status === REMOVED) return { tone: 'error', glyph: '✕', label: 'Removed from catalog' }
  if (agent.status === DISABLED) return { tone: 'neutral', glyph: '■', label: 'Disabled' }
  // A registry agent is pinned, with no commit to fetch: enabled is active.
  if (pinned(agent)) return { tone: 'success', glyph: '✓', label: 'Active' }
  // An SDK v1 agent's refresh fails for that reason alone: the disc says what is wrong, not the symptom.
  if (agent.unsupported) return { tone: 'warning', glyph: '!', label: UNSUPPORTED }
  if (agent.resolve_error) return { tone: 'warning', glyph: '!', label: 'Last refresh failed' }
  if (!agent.resolved_commit) return { tone: 'warning', glyph: '?', label: 'Never fetched' }
  return { tone: 'success', glyph: '✓', label: 'Active' }
}

/** "Coding · Python · v0.1.0 · 3f2a9c1": the facts that tell two agents apart, in one wrapping line, each stored
 *  value by its label. A registry agent's: "Coding · npm · ACP registry · pinned v1.2.0". */
export function agentFacts(agent: Agent): string {
  if (pinned(agent)) {
    return [agentTypeLabel(agent.agent_type), languageLabel(agent.language), `ACP registry · pinned v${agent.version.replace(/^v/, '')}`]
      .filter(Boolean).join(' · ')
  }
  return [
    agentTypeLabel(agent.agent_type),
    languageLabel(agent.language),
    agent.version ? `v${agent.version.replace(/^v/, '')}` : null,
    shortSha(agent.resolved_commit),
    agent.is_catalog ? 'catalog' : null,
  ].filter(Boolean).join(' · ')
}

/** Where it comes from: "github.com/acme/bots / path @ main" (a commit-sha ref, a connector's, shortened). */
export function agentSource(agent: Agent): string {
  const repo = agent.repo_url.replace(/^https?:\/\//, '')
  // A registry agent's ref is its version, already in the facts.
  if (pinned(agent)) return repo
  const ref = /^[0-9a-f]{40}$/.test(agent.ref) ? shortSha(agent.ref) : agent.ref
  return `${repo}${agent.subpath ? ` / ${agent.subpath}` : ''} @ ${ref}`
}

type Dialog = { kind: 'in-use'; agent: Agent; sentence: string; inUse: InUse }

export default function AgentsPage() {
  const { admin } = useAuth()
  const navigate = useNavigate()
  const orgVisible = useOrgVisible()
  const { canRegister: canEdit, canAdd } = useAgentsVerbs()
  const { data, loading, error, refresh } = useApiRead(['agents-page-agents'], () => listAgents(), 'Could not load agents')
  const [dialog, setDialog] = useState<Dialog | null>(null)
  const [busy, withBusy] = useBusyIds()
  const [notice, setNotice] = useState<Notice | null>(null)
  const [confirm, confirmDialog] = useConfirm()
  const hasRegistry = (data ?? []).some(pinned)
  // Only to name a registry agent's key account; a failed read leaves the caption generic.
  const accounts = useApiRead(['agents-page-accounts', hasRegistry], () => listAccounts(), 'Could not load accounts', { enabled: hasRegistry })
  const accountName = (id: string) => accounts.data?.find(account => account.id === id)?.name

  const act = (agent: Agent, verb: string, run: () => Promise<unknown>) => withBusy(agent.id, () =>
    runWithNotice(setNotice, run, `${verb} ${agent.display_name}.`, `Could not update ${agent.display_name}`),
  ).finally(refresh)

  async function remove(agent: Agent) {
    if (!(await confirm(deleteAgentConfirm(agent)))) return
    await withBusy(agent.id, async () => {
      setNotice(null)
      try {
        await deleteAgent(agent.id)
        setNotice({ severity: 'success', lines: [`Deleted ${agent.display_name}.`] })
        await refresh()
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          setDialog({
            kind: 'in-use', agent, sentence: apiDetail(err, `${agent.display_name} is still in use`), inUse: inUseOf(err),
          })
        } else {
          setNotice({ severity: 'error', lines: [apiDetail(err, `Could not delete ${agent.display_name}`)] })
        }
      }
    })
  }

  const rows: FeedRowSpec[] = (data ?? []).map(agent => {
    const state = statusOf(agent)
    // Another org's agent (an operator's filtered list) is read-only here: writes stay on the caller's org.
    const unresolvedId = agent.org ? undefined : agent.org_id
    const orgChip = orgVisible && !(agent.is_catalog && !agent.org)
    const own = !agent.is_catalog && (admin?.is_operator !== true || agent.org_id === admin.org?.id)
    // A registry agent is pinned at install: no ref to edit, nothing to refresh.
    const snapshotActions: OverflowItem[] = pinned(agent) ? [] : [
      { key: 'refresh', icon: ACTION_ICON.refresh, label: 'Refresh', ariaLabel: `Refresh ${agent.display_name}`,
        onClick: () => void act(agent, 'Refreshed', () => refreshAgent(agent.id)) },
    ]
    const keyAccount = pinned(agent) && agent.key_account_id && accountName(agent.key_account_id)
      ? agent.key_account_id : null
    const actions: OverflowItem[] = [
      ...(keyAccount ? [{ key: 'open-key', label: 'Open key account', ariaLabel: `Open key account for ${agent.display_name}`,
        testId: 'agent-key-account', to: `/accounts/${encodeURIComponent(keyAccount)}` }] : []),
      ...(own && canEdit ? [
      ...snapshotActions,
      agent.status === DISABLED
        ? { key: 'enable', icon: ACTION_ICON.enable, label: 'Enable', ariaLabel: `Enable ${agent.display_name}`,
            onClick: () => void act(agent, 'Enabled', () => updateAgent(agent.id, { status: 'active' })) }
        : { key: 'disable', icon: ACTION_ICON.disable, label: 'Disable', ariaLabel: `Disable ${agent.display_name}`,
            onClick: () => void act(agent, 'Disabled', () => updateAgent(agent.id, { status: 'disabled' })) },
      { key: 'delete', icon: ACTION_ICON.delete, label: 'Delete', danger: true, ariaLabel: `Delete ${agent.display_name}`,
        onClick: () => void remove(agent) },
      ] : []),
    ]
    return {
      key: agent.id,
      indicator: { tone: state.tone, glyph: state.glyph, label: state.label, title: state.label, data: { status: agent.status } },
      title: agent.display_name,
      titleText: agent.display_name,
      href: agentPath(agent.id),
      // Two lines, never a clip to a few characters (AC-9).
      titleLines: 2,
      muted: agent.status !== 'active',
      busy: busy.has(agent.id),
      data: { agent: agent.slug },
      /* The org hugs the name, off on a phone. A catalog connector belongs to no tenant and draws none; a tenant
         agent whose org did not resolve still shows its id. An SDK v1 agent's Unsupported pill and its fix stay at
         every width, the server's sentence on hover. */
      titleAside: (orgChip || agent.unsupported) && (
        <>
          {orgChip && (
            <Box component="span" sx={{ display: 'inline-flex', maxWidth: ORG_SLOT, [PHONE]: { display: 'none' } }}>
              <OrgIndicator org={agent.org} unresolvedId={unresolvedId} testId="col-org" />
            </Box>
          )}
          {agent.unsupported && (
            <Box component="span" data-testid="agent-unsupported" sx={{ display: 'inline-flex', alignItems: 'center', flexShrink: 0 }}>
              <StatusChip label={UNSUPPORTED} tone="neutral" title={agent.unsupported} data-status="unsupported" />
              <Box component="span" data-testid="agent-unsupported-fix" sx={{ ml: 1, color: 'text.secondary', whiteSpace: 'nowrap' }}>
                {UNSUPPORTED_FIX}
              </Box>
            </Box>
          )}
        </>
      ),
      actions,
      /* The facts on hover: the state is the disc's, the org the name's aside. */
      notes: [
        { key: 'facts', text: agentFacts(agent), tone: 'muted' },
        { key: 'source', text: agentSource(agent), tone: 'muted' },
        ...(pinned(agent) ? [{
          key: 'key-account',
          text: agent.key_account_id
            ? accountName(agent.key_account_id) ? `Key: ${accountName(agent.key_account_id)}` : 'Key account set'
            : 'No key account',
          tone: 'muted' as const,
        }] : []),
        ...(agent.description ? [{ key: 'description', text: agent.description, tone: 'muted' as const }] : []),
        ...(agent.resolve_error
          ? [{ key: 'resolve-error', text: `Last refresh failed: ${agent.resolve_error}`, tone: 'error' as const }]
          : []),
      ],
    }
  })

  /* The strip's Register only beside a list: the empty state holds the one entry when nothing is listed (a phone
     showed both), and a strip verb shown while loading would vanish under the reader's tap. */
  const firstLoad = loading && data === null
  const empty = !firstLoad && rows.length === 0
  return (
    <Box>
      {/* Titleless: Register portals onto the Agents strip (`components/TabActionsSlot`), wordless on a phone. */}
      <PageHeader
        actions={canAdd && !firstLoad && !empty && (
          <NewButton label="Add agent" onClick={() => navigate('/agents/new')} sx={tapTargetSx} />
        )}
      />
      <RegistryOffPlanNote />
      {error && <Banner severity="error">{error}</Banner>}
      <NoticeBanner notice={notice} onDismiss={() => setNotice(null)} testId="agents-notice" />
      <ListStates
        loading={firstLoad}
        skeleton={{ rows: 4 }}
        empty={empty}
        noMatch={false}
        emptyTitle="No agents yet"
        emptyAction={canAdd && <NewButton label="Add agent" worded onClick={() => navigate('/agents/new')} sx={tapTargetSx} />}
        feed={{ ariaLabel: 'Agents', testId: 'agents-list', sections: [{ key: 'agents', rows }] }}
      />

      {dialog && (
        <FormDialog
          title={`${dialog.agent.display_name} is in use`}
          submitLabel="Disable"
          // Already disabled: nothing left to offer but the sentence.
          hideSubmit={dialog.agent.status === DISABLED}
          cancelLabel="Close"
          testId="agent-in-use-dialog"
          onClose={() => setDialog(null)}
          onSubmit={() => {
            const agent = dialog.agent
            setDialog(null)
            void act(agent, 'Disabled', () => updateAgent(agent.id, { status: 'disabled' }))
          }}
        >
          <Typography data-testid="agent-in-use-sentence">{dialog.sentence}</Typography>
          <InUseLinks inUse={dialog.inUse} />
        </FormDialog>
      )}
      {confirmDialog}
    </Box>
  )
}

/** Whether an account is of the agent's vendor: its provider (`accounts.provider`) is one of the words of the
 *  agent's slug, name or repo path ("<provider>-code-acp", "<Provider> CLI"). The wire carries no vendor for a registry
 *  agent, so this is a guess that only orders the list; it never refuses a binding. */
export function vendorMatches(agent: Agent, account: Account): boolean {
  const provider = account.provider.toLowerCase()
  return !!provider && `${agent.slug} ${agent.display_name} ${agent.repo_url}`.toLowerCase().split(/[^a-z0-9]+/).includes(provider)
}

/** The accounts the dialog offers, the agent's own vendor's first; otherwise in the roster's order. */
export function orderForAgent(agent: Agent, accounts: Account[]): Account[] {
  return [...accounts].sort((a, b) => Number(vendorMatches(agent, b)) - Number(vendorMatches(agent, a)))
}

/** The save notice: what changed, by account name ("an account" when the roster did not load). `same` is decided by
 *  id by the caller: two accounts may share a name. */
export function keyAccountNotice(agentName: string, before: string | null, after: string | null, same = before === after): string {
  if (same) return `${agentName}'s key account is unchanged.`
  if (!after) return `Removed ${before} as ${agentName}'s key account.`
  if (!before) return `${agentName} now uses ${after} as its key account.`
  return `${agentName}'s key account changed from ${before} to ${after}.`
}

/** Who still uses an agent a delete was refused for: the projects defaulting to it and the tasks naming it, linked. */
export function InUseLinks({ inUse }: { inUse: InUse }) {
  if (inUse.projects.length + inUse.tasks.length === 0) return null
  return (
    <Box component="ul" sx={{ m: 0, mt: 1, pl: 2.5 }} data-testid="agent-in-use-links">
      {inUse.projects.map(project => (
        <li key={`project-${project.id}`}>
          <MuiLink component={RouterLink} to={`/projects/${project.id}`}>{project.name}</MuiLink> (default)
        </li>
      ))}
      {inUse.tasks.map(task => (
        <li key={`task-${task.id}`}>
          <MuiLink component={RouterLink} to={`/tasks/${task.id}`}>{task.title}</MuiLink>
        </li>
      ))}
    </Box>
  )
}
