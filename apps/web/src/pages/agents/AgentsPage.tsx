import { useMemo, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MuiLink from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { apiDetail, ApiError } from '../../api'
import { useAuth } from '../../auth'
import { useConfirm } from '../../components/ConfirmDialog'
import { useApiRead } from '../../hooks/useApiRead'
import { useBusyIds } from '../../hooks/useBusyIds'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import type { Account } from '../../types'
import { CompactVerbLabel, compactVerbProps } from '../../ui/CompactVerb'
import Banner from '../../ui/Banner'
import CredentialSelect, { type CredentialOption } from '../../ui/CredentialSelect'
import { credentialMark } from '../../ui/credentialMarks'
import ListStates from '../../ui/feed/ListStates'
import { matchesAny } from '../../ui/FilterRow'
import type { FeedRowSpec, OverflowItem } from '../../ui/feed/types'
import FormDialog from '../../ui/FormDialog'
import { PHONE, tapTargetSx } from '../../ui/listStyles'
import { ORG_SLOT, OrgIndicator, useOrgVisible } from '../../ui/OrgIndicator'
import { StatusChip, type Tone } from '../../ui/StatusChip'
import { listAccounts } from '../credentials/api'
import { AgentsStrip, RegistryOffPlanNote, useAgentsVerbs } from './agentsTabs'
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
  refusalSentences,
  registerAgent,
  REMOVED,
  shortCommit,
  updateAgent,
} from './api'
import { type Notice, NoticeBanner, SnapshotDialog } from './SnapshotDialog'

/* ── DIRECTORY > AGENTS: THE ORG'S SDK AGENTS (AC-9) ───────────────────────────────────────────────────────
   `GET /api/agents` — the org's own agents, then the catalog connectors it enabled. Register takes a git repo
   URL, an optional sub-path and a ref; the server snapshots and validates it there and then, and a refusal is a
   422 of sentences, each listed (`refusalSentences`), never an HTTP code. A row's ⋯ edits ref/sub-path,
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
   never env. An org admin binds or unbinds it from the row's Key account (`PATCH key_account_id`), choosing among
   the org's enabled API-key accounts, the agent's own vendor's first (`vendorMatches`); the accounts load only while a
   registry agent is listed. The save notice names the change (`keyAccountNotice`). */

const ACTION_ICON = { edit: '✎', refresh: '⟳', key: '🔑', disable: '⏸', enable: '▶', delete: '🗑' } as const

/** Bindable to a registry agent (`routes/agents.py` refuses any other). */
const bindable = (account: Account) => account.auth_kind === 'api_key' && account.enabled

const pinned = (agent: Agent) => agent.source === ACP_REGISTRY

function statusOf(agent: Agent): { tone: Tone; glyph: string; label: string } {
  if (agent.status === REMOVED) return { tone: 'error', glyph: '✕', label: 'Removed from catalog' }
  if (agent.status === DISABLED) return { tone: 'neutral', glyph: '■', label: 'Disabled' }
  // A registry agent is pinned, with no commit to fetch: enabled is active.
  if (pinned(agent)) return { tone: 'success', glyph: '✓', label: 'Active' }
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
    shortCommit(agent.resolved_commit),
    agent.is_catalog ? 'catalog' : null,
  ].filter(Boolean).join(' · ')
}

/** Where it comes from: "github.com/acme/bots / path @ main" (a commit-sha ref, a connector's, shortened). */
export function agentSource(agent: Agent): string {
  const repo = agent.repo_url.replace(/^https?:\/\//, '')
  // A registry agent's ref is its version, already in the facts.
  if (pinned(agent)) return repo
  const ref = /^[0-9a-f]{40}$/.test(agent.ref) ? shortCommit(agent.ref) : agent.ref
  return `${repo}${agent.subpath ? ` / ${agent.subpath}` : ''} @ ${ref}`
}

type Dialog =
  | { kind: 'register' }
  | { kind: 'edit'; agent: Agent }
  | { kind: 'in-use'; agent: Agent; sentence: string; inUse: InUse }
  | { kind: 'key'; agent: Agent }

export default function AgentsPage() {
  const { admin } = useAuth()
  const orgVisible = useOrgVisible()
  const { canRegister: canEdit, canAdd } = useAgentsVerbs()
  const { data, loading, error, refresh } = useApiRead(['agents-page-agents'], () => listAgents(), 'Could not load agents')
  const [dialog, setDialog] = useState<Dialog | null>(null)
  const [busy, withBusy] = useBusyIds()
  const [notice, setNotice] = useState<Notice | null>(null)
  const [confirm, confirmDialog] = useConfirm()
  const orgAdmin = isOrgAdminSeat(admin)
  const hasRegistry = (data ?? []).some(pinned)
  // Only to name a registry agent's key account; a failed read leaves the caption generic.
  const accounts = useApiRead(['agents-page-accounts', hasRegistry], () => listAccounts(), 'Could not load accounts', { enabled: hasRegistry })
  const accountName = (id: string) => accounts.data?.find(account => account.id === id)?.name

  const act = (agent: Agent, verb: string, run: () => Promise<unknown>) => withBusy(agent.id, async () => {
    setNotice(null)
    try {
      await run()
      setNotice({ severity: 'success', lines: [`${verb} ${agent.display_name}.`] })
    } catch (err) {
      setNotice({ severity: 'error', lines: refusalSentences(err, `Could not update ${agent.display_name}`) })
    }
  }).finally(refresh)

  async function remove(agent: Agent) {
    const ok = await confirm({
      title: `Delete ${agent.display_name}?`,
      message: 'Its registration is removed for everyone. Past runs keep their record.',
      confirmLabel: 'Delete',
      destructive: true,
    })
    if (!ok) return
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
    const snapshotActions: OverflowItem[] = pinned(agent) ? (orgAdmin ? [
      { key: 'key', icon: ACTION_ICON.key, label: 'Key account', ariaLabel: `Key account for ${agent.display_name}`,
        onClick: () => setDialog({ kind: 'key', agent }) },
    ] : []) : [
      { key: 'edit', icon: ACTION_ICON.edit, label: 'Edit', ariaLabel: `Edit ${agent.display_name}`,
        onClick: () => setDialog({ kind: 'edit', agent }) },
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
      // Two lines, never a clip to a few characters (AC-9).
      titleLines: 2,
      muted: agent.status !== 'active',
      busy: busy.has(agent.id),
      data: { agent: agent.slug },
      /* The org hugs the name, off on a phone. A catalog connector belongs to no tenant and draws none; a tenant
         agent whose org did not resolve still shows its id. An SDK v1 agent's Unsupported pill stays at every
         width, its sentence on hover. */
      titleAside: (orgChip || agent.unsupported) && (
        <>
          {orgChip && (
            <Box component="span" sx={{ display: 'inline-flex', maxWidth: ORG_SLOT, [PHONE]: { display: 'none' } }}>
              <OrgIndicator org={agent.org} unresolvedId={unresolvedId} testId="col-org" />
            </Box>
          )}
          {agent.unsupported && (
            <Box component="span" data-testid="agent-unsupported" sx={{ display: 'inline-flex', flexShrink: 0 }}>
              <StatusChip label="Unsupported" tone="neutral" title={agent.unsupported} data-status="unsupported" />
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
      <AgentsStrip
        tab="agents"
        actions={canAdd && !firstLoad && !empty && (
          <Button variant="contained" color="primary" {...compactVerbProps('Register agent', tapTargetSx)} onClick={() => setDialog({ kind: 'register' })}>
            <CompactVerbLabel label="Register agent" />
          </Button>
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
        emptyAction={canAdd && <Button onClick={() => setDialog({ kind: 'register' })}>Register agent</Button>}
        feed={{ ariaLabel: 'Agents', testId: 'agents-list', sections: [{ key: 'agents', rows }] }}
      />

      {dialog?.kind === 'register' && (
        <SnapshotDialog
          title="Register agent"
          submitLabel="Register"
          initial={{ repo_url: '', subpath: '', ref: 'main', env: {} }}
          onClose={() => setDialog(null)}
          onSave={async body => {
            const agent = await registerAgent(body)
            setNotice({ severity: 'success', lines: [`Registered ${agent.display_name}.`] })
            await refresh()
          }}
        />
      )}
      {dialog?.kind === 'edit' && (
        <SnapshotDialog
          title={`Edit ${dialog.agent.display_name}`}
          submitLabel="Save"
          initial={{
            repo_url: dialog.agent.repo_url, subpath: dialog.agent.subpath, ref: dialog.agent.ref, env: dialog.agent.env ?? {},
          }}
          repoEditable={false}
          onClose={() => setDialog(null)}
          onSave={async body => {
            const agent = await updateAgent(dialog.agent.id, { ref: body.ref, subpath: body.subpath, env: body.env ?? {} })
            setNotice({ severity: 'success', lines: [`Saved ${agent.display_name}.`] })
            await refresh()
          }}
        />
      )}
      {dialog?.kind === 'in-use' && (
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
          {dialog.inUse.projects.length + dialog.inUse.tasks.length > 0 && (
            <Box component="ul" sx={{ m: 0, mt: 1, pl: 2.5 }} data-testid="agent-in-use-links">
              {dialog.inUse.projects.map(project => (
                <li key={`project-${project.id}`}>
                  <MuiLink component={RouterLink} to={`/projects/${project.id}`}>{project.name}</MuiLink> (default)
                </li>
              ))}
              {dialog.inUse.tasks.map(task => (
                <li key={`task-${task.id}`}>
                  <MuiLink component={RouterLink} to={`/tasks/${task.id}`}>{task.title}</MuiLink>
                </li>
              ))}
            </Box>
          )}
        </FormDialog>
      )}
      {dialog?.kind === 'key' && (
        <KeyAccountDialog
          agent={dialog.agent}
          accounts={accounts.data ?? []}
          onClose={() => setDialog(null)}
          onSaved={async line => {
            setNotice({ severity: 'success', lines: [line] })
            await refresh()
          }}
        />
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

/** Bind a registry agent to one of the org's enabled API-key accounts, or to none. A lookup, not a native select:
 *  each row is the name with its provider beside it, dim (a "Name (slug · provider)" option ran long on a phone), as
 *  every credential field reads (`ui/CredentialSelect`); clearing it is None. A bound account no longer bindable
 *  (disabled) is still named, offered disabled, so the field shows what is set. A refusal's sentences are listed. */
function KeyAccountDialog({ agent, accounts, onClose, onSaved }: {
  agent: Agent
  accounts: Account[]
  onClose: () => void
  onSaved: (notice: string) => Promise<void>
}) {
  const [value, setValue] = useState<string | null>(agent.key_account_id || null)
  const [busy, setBusy] = useState(false)
  const [errors, setErrors] = useState<string[]>([])
  const options = useMemo(() => {
    // The slug only where two accounts share a name: the provider alone then would not tell them apart.
    const shared = new Set(accounts.filter((a, i) => accounts.findIndex(b => b.name === a.name) !== i).map(a => a.name))
    const option = (account: Account): CredentialOption => ({
      id: account.id,
      name: account.name,
      hint: [
        account.provider,
        shared.has(account.name) ? account.slug : null,
        vendorMatches(agent, account) ? 'same vendor' : null,
        bindable(account) ? null : account.enabled ? 'not an API key' : 'disabled',
      ].filter(Boolean).join(' · '),
      disabled: !bindable(account),
      icon: credentialMark(account.provider, account.provider),
    })
    const current = accounts.find(account => account.id === agent.key_account_id)
    return [
      ...(current && !bindable(current) ? [option(current)] : []),
      // The agent's own vendor's accounts first.
      ...orderForAgent(agent, accounts.filter(bindable)).map(option),
    ]
  }, [accounts, agent])
  const nameOf = (id: string | null) => !id ? null : accounts.find(account => account.id === id)?.name ?? 'an account'
  const selected = useMemo(() => options.find(option => option.id === value) ?? null, [options, value])

  async function submit() {
    setBusy(true)
    setErrors([])
    try {
      const saved = await updateAgent(agent.id, { key_account_id: value })
      onClose()
      await onSaved(keyAccountNotice(saved.display_name, nameOf(agent.key_account_id), nameOf(saved.key_account_id),
        agent.key_account_id === saved.key_account_id))
    } catch (err) {
      setErrors(refusalSentences(err, `Could not save ${agent.display_name}`))
    } finally {
      setBusy(false)
    }
  }

  return (
    <FormDialog
      title={`Key account for ${agent.display_name}`}
      submitLabel="Save"
      busy={busy}
      onSubmit={() => void submit()}
      onClose={onClose}
      testId="agent-key-dialog"
    >
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
        <CredentialSelect
          id="agent-key-account"
          label="API-key account"
          placeholder="None"
          helperText="Sent to the agent when a task runs, never as an environment variable."
          value={value}
          onChange={setValue}
          selected={selected}
          fetchOptions={async query => options.filter(option => matchesAny(query, `${option.name} ${option.hint}`))}
          minChars={0}
          noOptionsText="No matching API-key account"
          disabled={busy}
          testId="agent-key-select"
        />
        {errors.length > 0 && (
          <Banner severity="error" testId="agent-key-dialog-errors">
            {errors.length === 1 ? errors[0] : (
              <Box component="ul" sx={{ m: 0, pl: 2.5 }}>{errors.map((line, i) => <li key={i}>{line}</li>)}</Box>
            )}
          </Banner>
        )}
      </Box>
    </FormDialog>
  )
}
