import { type FormEvent, type JSX, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { apiDetail, ApiError } from '../../api'
import { useAuth } from '../../auth'
import Breadcrumb from '../../components/Breadcrumb'
import { useConfirm } from '../../components/ConfirmDialog'
import { TabPanel, Tabs, type TabDef } from '../../components/Tabs'
import { useApiRead } from '../../hooks/useApiRead'
import { useBaselineDirty } from '../../hooks/useBaselineDirty'
import { useLeaveGuard } from '../../hooks/useLeaveGuard'
import { useUrlTab } from '../../hooks/useUrlTab'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import type { Account } from '../../types'
import Banner from '../../ui/Banner'
import CancelSaveBar from '../../ui/CancelSaveBar'
import CredentialSelect, { type CredentialOption } from '../../ui/CredentialSelect'
import { credentialMark } from '../../ui/credentialMarks'
import { DetailPage, DetailSection } from '../../ui/detail/DetailPage'
import { DetailStatus } from '../../ui/detail/DetailStatus'
import { matchesAny } from '../../ui/FilterRow'
import type { OverflowItem } from '../../ui/feed/types'
import { KeyValue, KeyValueList } from '../../ui/KeyValueList'
import LoadingState from '../../ui/LoadingState'
import { OverflowMenu } from '../../ui/OverflowMenu'
import { OrgIndicator } from '../../ui/OrgIndicator'
import { StatusChip } from '../../ui/StatusChip'
import { LeaveGuardDialog } from '../../ui/UnsavedChangesDialog'
import { tapTargetSx } from '../../ui/listStyles'
import { listAccounts } from '../credentials/api'
import { useAgentsTabs, useAgentsVerbs } from './agentsTabs'
import {
  type Agent,
  type AgentRegistration,
  deleteAgent,
  DISABLED,
  type InUse,
  inUseOf,
  listAgents,
  refreshAgent,
  refusalSentences,
  registerAgent,
  updateAgent,
} from './api'
import {
  EnvFields,
  type EnvRow,
  envMap,
  envRowError,
  envRowsOf,
  filledEnv,
  ENV_HELP,
  type Notice,
  NoticeBanner,
  RefusalBanner,
  SourceFields,
} from './agentFields'
import {
  agentFacts,
  agentPath,
  agentSource,
  bindable,
  deleteAgentConfirm,
  InUseLinks,
  keyAccountNotice,
  orderForAgent,
  pinned,
  statusOf,
  UNSUPPORTED,
  vendorMatches,
} from './AgentsPage'

/* ── ONE AGENT'S PAGE (`/agents/:id`, and `/agents/new` in create mode) — beside the Custom agents list
   (`lib/listDetail.ts`), opened by a row's name in every state. Register takes a git repo URL, an optional sub-path
   and a ref; the server fetches and validates it on Save, which takes seconds (Save spins, a refusal's sentences are
   listed, never an HTTP code). An existing agent keeps its repo; its ref, sub-path and env change here. An ACP
   registry agent is pinned at install: no ref to edit, nothing to refresh — its one setting is its KEY ACCOUNT (an
   org API-key account whose key reaches the agent through ACP `authenticate` at run time, never env), an org
   admin's field on this page (AD-Q1). A catalog connector, and another org's agent in an operator's view, are
   read-only here. The record's verbs (Refresh, Enable/Disable, Delete, Open key account) sit in the header's More. */

type AgentTab = 'agent' | 'environment'
const AGENT_TAB: TabDef<AgentTab> = { id: 'agent', label: 'Agent' }
const ENV_TAB: TabDef<AgentTab> = { id: 'environment', label: 'Environment' }

const BLANK: AgentRegistration = { repo_url: '', subpath: '', ref: 'main' }

export default function AgentEditPage({ mode }: { mode?: 'create' } = {}): JSX.Element {
  const creating = mode === 'create'
  const { id = '' } = useParams()
  const { admin } = useAuth()
  const { canRegister: canEdit, canAdd } = useAgentsVerbs()
  const listTab = useAgentsTabs().find(tab => tab.id === 'agents')
  // The list's own read (same key): a cache hit when opened from it, and Save refreshes both.
  const { data, loading, error, refresh } = useApiRead(['agents-page-agents'], () => listAgents(), 'Could not load agents',
    { enabled: !creating })
  const agent = creating ? null : data?.find(a => a.id === id) ?? null
  const title = creating ? 'New agent' : agent?.display_name ?? 'Agent'
  // The trail ends at the list; the title names the agent, its state beside it (as a compliance cell's page).
  const crumbs = <Breadcrumb endsAtParent items={[{ label: listTab?.label ?? 'Custom agents', to: '/agents' }]} />

  const trail = <Breadcrumb items={[{ label: listTab?.label ?? 'Custom agents', to: '/agents' }, { label: title }]} />
  if (!creating && loading && data === null) return <DetailStatus trail={trail} loading />
  if (!creating && error && data === null) return <DetailStatus trail={trail} error={error} />
  if (!creating && !agent) return <DetailStatus trail={trail} notFound="Agent not found" />
  if (creating && !canAdd) return <DetailStatus trail={trail} notFound="Not allowed" />

  return (
    <AgentForm
      // A move to another agent (or New) starts a fresh draft and baseline.
      key={agent?.id ?? 'new'}
      agent={agent}
      crumbs={crumbs}
      title={title}
      canEdit={canEdit}
      orgAdmin={isOrgAdminSeat(admin)}
      ownOrgId={admin?.is_operator === true ? admin.org?.id ?? null : undefined}
      refresh={refresh}
    />
  )
}

type Draft = { form: AgentRegistration; env: EnvRow[]; keyAccount: string | null }

function AgentForm({ agent, crumbs, title, canEdit, orgAdmin, ownOrgId, refresh }: {
  agent: Agent | null
  crumbs: JSX.Element
  title: string
  canEdit: boolean
  orgAdmin: boolean
  /** The operator's own org (another org's agent is read-only here); `undefined` for a tenant seat. */
  ownOrgId: string | null | undefined
  refresh: () => Promise<unknown>
}) {
  const queryClient = useQueryClient()
  const [confirm, confirmDialog] = useConfirm()
  const creating = agent === null
  const isPinned = agent !== null && pinned(agent)
  // A catalog connector is turned on or off on the Catalog tab; another org's agent stays that org's.
  const own = creating || (!agent.is_catalog && (ownOrgId === undefined || agent.org_id === ownOrgId))
  const readOnly = !canEdit || !own
  const keyEditable = isPinned && own && orgAdmin
  // A registry agent's one setting is its key account, an org admin's.
  const barReadOnly = isPinned ? !keyEditable : readOnly

  const [draft, setDraft] = useState<Draft>(() => ({
    form: agent ? { repo_url: agent.repo_url, subpath: agent.subpath, ref: agent.ref } : BLANK,
    env: envRowsOf(agent?.env),
    keyAccount: agent?.key_account_id || null,
  }))
  const [busy, setBusy] = useState(false)
  // A source write (register / save) fetches the repo; the More verbs only set `busy`.
  const [fetching, setFetching] = useState(false)
  const [errors, setErrors] = useState<string[]>([])
  const [notice, setNotice] = useState<Notice | null>(null)
  const [inUse, setInUse] = useState<{ sentence: string; inUse: InUse } | null>(null)

  const tabs = isPinned ? [AGENT_TAB] : [AGENT_TAB, ENV_TAB]
  const [tab, selectTab] = useUrlTab(tabs, 'agent')

  // Only to name and bind a registry agent's key account.
  const accounts = useApiRead(['agents-page-accounts', isPinned], () => listAccounts(), 'Could not load accounts', { enabled: isPinned })

  // The body a Save would send, as JSON: what dirty compares.
  const body = isPinned
    ? { key_account_id: draft.keyAccount }
    : { repo_url: draft.form.repo_url.trim(), subpath: draft.form.subpath.trim(), ref: draft.form.ref.trim(), env: envMap(draft.env) }
  const { dirty, reset: rebase } = useBaselineDirty(JSON.stringify(body))
  const incomplete = !isPinned && (draft.form.repo_url.trim() === '' || draft.form.ref.trim() === ''
    || filledEnv(draft.env).some(row => envRowError(draft.env, row) !== undefined))

  async function write(): Promise<Agent | null> {
    setBusy(true)
    setFetching(!isPinned)
    setErrors([])
    setNotice(null)
    try {
      if (creating) {
        // No env key at all when no variable is set (the register body is as the API documents it).
        return await registerAgent({ repo_url: draft.form.repo_url.trim(), subpath: draft.form.subpath.trim(),
          ref: draft.form.ref.trim(), ...(filledEnv(draft.env).length > 0 ? { env: envMap(draft.env) } : {}) })
      }
      if (isPinned) {
        const saved = await updateAgent(agent.id, { key_account_id: draft.keyAccount })
        const nameOf = (accountId: string | null) =>
          !accountId ? null : accounts.data?.find(a => a.id === accountId)?.name ?? 'an account'
        setNotice({ severity: 'success', lines: [keyAccountNotice(saved.display_name, nameOf(agent.key_account_id),
          nameOf(saved.key_account_id), agent.key_account_id === saved.key_account_id)] })
        return saved
      }
      const saved = await updateAgent(agent.id, { ref: draft.form.ref.trim(), subpath: draft.form.subpath.trim(), env: envMap(draft.env) })
      setNotice({ severity: 'success', lines: [`Saved ${saved.display_name}.`] })
      return saved
    } catch (err) {
      setErrors(refusalSentences(err, creating ? 'Could not save the agent' : `Could not save ${agent.display_name}`))
      return null
    } finally {
      setBusy(false)
      setFetching(false)
    }
  }

  async function save(): Promise<boolean> {
    // The leave dialog's Save on a half-filled form stays, as Save itself is off.
    if (incomplete) return false
    const saved = await write()
    if (!saved) return false
    rebase()
    await queryClient.invalidateQueries({ queryKey: ['agents-page-agents'] })
    return true
  }

  const guard = useLeaveGuard(dirty && !barReadOnly, save)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (busy || incomplete || !dirty) return
    const saved = await write()
    if (!saved) return
    // The saved body is the new baseline: Save stands down until the next change.
    rebase()
    await queryClient.invalidateQueries({ queryKey: ['agents-page-agents'] })
    // Create lands on the new agent's own page, the row already in the shared list (no "not found" flash before the
    // refetch); an edit stays, its saved body the new baseline.
    if (creating) {
      queryClient.setQueryData<Agent[]>(['agents-page-agents'], rows => [...(rows ?? []).filter(a => a.id !== saved.id), saved])
      guard.leaveNow(agentPath(saved.id), { replace: true })
    }
  }

  async function act(verb: string, run: () => Promise<unknown>) {
    if (!agent) return
    setBusy(true)
    setNotice(null)
    try {
      await run()
      setNotice({ severity: 'success', lines: [`${verb} ${agent.display_name}.`] })
    } catch (err) {
      setNotice({ severity: 'error', lines: refusalSentences(err, `Could not update ${agent.display_name}`) })
    } finally {
      setBusy(false)
      await refresh()
    }
  }

  async function remove() {
    if (!agent) return
    if (!(await confirm(deleteAgentConfirm(agent)))) return
    setBusy(true)
    setNotice(null)
    setInUse(null)
    try {
      await deleteAgent(agent.id)
      await queryClient.invalidateQueries({ queryKey: ['agents-page-agents'] })
      guard.leaveNow('/agents', { replace: true })
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setInUse({ sentence: apiDetail(err, `${agent.display_name} is still in use`), inUse: inUseOf(err) })
      } else {
        setNotice({ severity: 'error', lines: [apiDetail(err, `Could not delete ${agent.display_name}`)] })
      }
    } finally {
      setBusy(false)
    }
  }

  const keyAccountId = isPinned && agent.key_account_id && accounts.data?.some(a => a.id === agent.key_account_id)
    ? agent.key_account_id : null
  const more: OverflowItem[] = agent ? [
    ...(keyAccountId ? [{ key: 'open-key', label: 'Open key account', ariaLabel: `Open key account for ${agent.display_name}`,
      testId: 'agent-key-account', to: `/accounts/${encodeURIComponent(keyAccountId)}` }] : []),
    ...(!readOnly ? [
      ...(isPinned ? [] : [{ key: 'refresh', label: 'Refresh', ariaLabel: `Refresh ${agent.display_name}`, testId: 'agent-refresh',
        onClick: () => void act('Refreshed', () => refreshAgent(agent.id)) }]),
      agent.status === DISABLED
        ? { key: 'enable', label: 'Enable', ariaLabel: `Enable ${agent.display_name}`, testId: 'agent-enable',
            onClick: () => void act('Enabled', () => updateAgent(agent.id, { status: 'active' })) }
        : { key: 'disable', label: 'Disable', ariaLabel: `Disable ${agent.display_name}`, testId: 'agent-disable',
            onClick: () => void act('Disabled', () => updateAgent(agent.id, { status: 'disabled' })) },
      { key: 'delete', label: 'Delete', danger: true, ariaLabel: `Delete ${agent.display_name}`, testId: 'agent-delete',
        onClick: () => void remove() },
    ] : []),
  ] : []
  const state = agent ? statusOf(agent) : null

  return (
    <DetailPage
      breadcrumb={crumbs}
      title={<Box component="span" sx={{ overflowWrap: 'anywhere' }}>{title}</Box>}
      titleAside={state && (
        // An SDK v1 agent shows one Unsupported mark, the reason on hover: the state chip when it already reads
        // Unsupported, else (disabled, removed) a second chip beside it.
        <Box sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5, flexShrink: 0 }}>
          <StatusChip label={state.label} tone={state.tone} data-status={agent?.status}
            title={state.label === UNSUPPORTED ? agent?.unsupported ?? undefined : undefined} />
          {agent?.unsupported && state.label !== UNSUPPORTED
            && <StatusChip label={UNSUPPORTED} tone="neutral" title={agent.unsupported} data-status="unsupported" />}
        </Box>
      )}
      actions={more.length > 0 && (
        <OverflowMenu button={{ label: 'More', variant: 'outlined' }} size="small" busy={busy} testId="agent-more-actions"
          ariaLabel={`More for ${title}`} items={more} sx={tapTargetSx} />
      )}
      testId="agent-page"
    >
      <Box component="form" noValidate onSubmit={e => void onSubmit(e)} data-testid="agent-form" sx={{ display: 'grid', gap: 2, mt: 1, minWidth: 0 }}>
        <NoticeBanner notice={notice} onDismiss={() => setNotice(null)} testId="agent-notice" />
        {inUse && (
          <Banner severity="warning" testId="agent-in-use" onDismiss={() => setInUse(null)}>
            <Typography variant="body2" data-testid="agent-in-use-sentence">{inUse.sentence}</Typography>
            <InUseLinks inUse={inUse.inUse} />
            {agent?.status !== DISABLED && <Typography variant="body2" sx={{ mt: 1 }}>Disable it from More instead.</Typography>}
          </Banner>
        )}
        {agent?.resolve_error && <Banner severity="warning" testId="agent-resolve-error">Last refresh failed: {agent.resolve_error}</Banner>}
        {agent && readOnly && (
          <Banner severity="info" testId="agent-read-only">
            {agent.is_catalog ? 'A catalog connector: turn it on or off on the Catalog tab.'
              : !own ? "Another org's agent: changes stay with that org." : 'You can view this agent but not edit it.'}
          </Banner>
        )}
        {tabs.length > 1 && <Tabs tabs={tabs} tab={tab} onSelect={selectTab} label="Agent sections" idPrefix="agent" />}

        <TabPanel id="agent" current={tab} idPrefix="agent">
          <Box sx={{ display: 'grid', gap: 2.5, minWidth: 0 }}>
            <DetailSection title="Source">
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                {isPinned ? (
                  <KeyValueList>
                    <KeyValue label="Repository">{agentSource(agent)}</KeyValue>
                  </KeyValueList>
                ) : (
                  <SourceFields
                    form={draft.form}
                    onChange={patch => setDraft(d => ({ ...d, form: { ...d.form, ...patch } }))}
                    repoEditable={creating}
                    readOnly={readOnly}
                    busy={busy}
                  />
                )}
                {fetching && <LoadingState label="Fetching the repository and checking its manifest…" />}
              </Box>
            </DetailSection>
            {isPinned && (
              <DetailSection title="Key account" help="An org API-key account whose key reaches the agent at run time.">
                <KeyAccountField agent={agent} accounts={accounts.data ?? []} value={draft.keyAccount}
                  onChange={keyAccount => setDraft(d => ({ ...d, keyAccount }))} disabled={busy || !keyEditable} />
              </DetailSection>
            )}
            {agent && (
              <DetailSection title="Details" testId="agent-facts">
                <KeyValueList>
                  <KeyValue label="Facts">{agentFacts(agent) || '—'}</KeyValue>
                  {agent.description && <KeyValue label="Description">{agent.description}</KeyValue>}
                  {agent.org && <KeyValue label="Org"><OrgIndicator org={agent.org} testId="agent-org" /></KeyValue>}
                </KeyValueList>
              </DetailSection>
            )}
          </Box>
        </TabPanel>
        {!isPinned && (
          <TabPanel id="environment" current={tab} idPrefix="agent">
            <DetailSection title="Environment" help={ENV_HELP}>
              <EnvFields rows={draft.env} onChange={env => setDraft(d => ({ ...d, env }))} readOnly={readOnly} busy={busy} />
            </DetailSection>
          </TabPanel>
        )}

        <RefusalBanner lines={errors} testId="agent-form-errors" />
        <CancelSaveBar testId="agent-form-actions" saveTestId="agent-save" cancelTo="/agents" creating={creating}
          busy={busy} dirty={dirty} disabled={incomplete} readOnly={barReadOnly}
          saveLabel={creating ? 'Register' : undefined} />
      </Box>
      {confirmDialog}
      <LeaveGuardDialog leave={guard} message="This agent has changes that have not been saved. Save them before leaving?" />
    </DetailPage>
  )
}

/** Bind a registry agent to one of the org's enabled API-key accounts, or to none. A lookup, not a native select:
 *  each row is the name with its provider beside it, dim, as every credential field reads (`ui/CredentialSelect`);
 *  clearing it is None. A bound account no longer bindable (disabled) is still named, offered disabled, so the field
 *  shows what is set. The agent's own vendor's accounts come first. */
function KeyAccountField({ agent, accounts, value, onChange, disabled }: {
  agent: Agent
  accounts: Account[]
  value: string | null
  onChange: (next: string | null) => void
  disabled: boolean
}) {
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
      ...orderForAgent(agent, accounts.filter(bindable)).map(option),
    ]
  }, [accounts, agent])
  const selected = useMemo(() => options.find(option => option.id === value) ?? null, [options, value])
  return (
    <CredentialSelect
      id="agent-key-account"
      label="API-key account"
      help="Sent to the agent per run; never an environment variable."
      placeholder="None"
      value={value}
      onChange={onChange}
      selected={selected}
      fetchOptions={async query => options.filter(option => matchesAny(query, `${option.name} ${option.hint}`))}
      minChars={0}
      noOptionsText="No matching API-key account"
      disabled={disabled}
      testId="agent-key-select"
    />
  )
}
