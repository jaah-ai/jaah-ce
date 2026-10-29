// The Agents surface's API client: SDK agents (`/api/agents`), the connector catalog (`/api/agent-catalog`),
// the ACP registry (`/api/agent-registry`) and the one agent picker's entries (`GET /api/agents/choices`). The
// wire contract is `apps/api/app/routes/agents.py` and `routes/acp_registry.py`; these types describe it and
// nothing more.

import { ApiError, apiDetail, del, get, patch, post } from '../../api'
import { readAsOnce } from '../../lib/actAs'
import type { OrgRef } from '../../types'

/** `AgentOut`: an org's own agent, or a catalog connector (`is_catalog`, with this org's `enabled`). */
export type Agent = {
  id: string
  slug: string
  display_name: string
  description: string
  agent_type: string
  language: string
  entrypoint: string
  version: string
  maintainer: string
  personas: string[]
  repo_url: string
  subpath: string
  ref: string
  resolved_commit: string | null
  bundle_sha256: string | null
  bundle_bytes: number | null
  /** `active` | `disabled` (an org agent) | `removed` (a withdrawn connector). */
  status: string
  /** Why the last refresh failed; the last good snapshot keeps serving. */
  resolve_error: string | null
  resolved_at: string | null
  is_catalog: boolean
  /** `acp_registry`: installed from the ACP registry, pinned at install (no ref to edit, nothing to refresh). */
  source: 'repo' | 'acp_registry'
  /** Set on the agent's process at launch; not secrets. */
  env: Record<string, string>
  /** A registry agent's bound org API-key account: its key reaches the agent through ACP `authenticate` at run
   *  time, never the environment. Null when unbound, and always on a `repo` agent. */
  key_account_id: string | null
  /** The owning org; an operator's list spans orgs. `org` labels an org agent there only. */
  org_id: string
  org?: OrgRef | null
  enabled?: boolean | null
  /** An SDK v1 (manifest 1) agent's sentence saying it no longer runs; null otherwise. The server omits such an
   *  agent from the picker's choices. */
  unsupported?: string | null
  created_at: string | null
  updated_at: string | null
}

/* An agent's stored vocabulary as people read it: the manifest's `type` and `language` (`tools/manifest_check.py`'s
 * `AGENT_TYPES` and `LANGUAGES`), and a registry agent's distribution kind, which it stores as its language. Languages
 * are named as the console's other lists name them (`compliance/AddRulePage.tsx`). */
const AGENT_TYPE_LABEL: Readonly<Record<string, string>> = { coding: 'Coding', non_coding: 'Non-coding', cli_wrapper: 'CLI wrapper' }

/** The wire's distribution kinds as people say them: an npx package is npm's, a uvx one Python's. */
export const DISTRIBUTION_LABEL: Readonly<Record<NonNullable<RegistryEntry['distribution']>, string>> = {
  binary: 'Binary', npx: 'npm', uvx: 'Python',
}

const LANGUAGE_LABEL: Readonly<Record<string, string>> = { python: 'Python', typescript: 'TypeScript', ...DISTRIBUTION_LABEL }

/** A value a newer server added: its words, capitalised ("some_kind" → "Some kind"), never the raw token. */
const spelled = (value: string) => {
  const words = value.replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

export const agentTypeLabel = (value: string): string => (value ? AGENT_TYPE_LABEL[value] ?? spelled(value) : '')
export const languageLabel = (value: string): string => (value ? LANGUAGE_LABEL[value] ?? spelled(value) : '')

/** `ChoiceOut`: one picker entry. A disabled one carries the sentence saying why. */
export type AgentChoice = {
  kind: 'builtin' | 'agent'
  display_name: string
  provider?: string | null
  id?: string | null
  agent_type?: string | null
  version?: string | null
  is_catalog?: boolean
  disabled?: boolean
  /** Withdrawn from the catalog: offered only to the row that already names it. */
  removed?: boolean
  reason?: string | null
}

export type AgentRegistration = { repo_url: string; subpath: string; ref: string; env?: Record<string, string> }
export type AgentEdit = {
  ref?: string
  subpath?: string
  status?: 'active' | 'disabled'
  env?: Record<string, string>
  /** A registry agent's key account (org admins only); null unbinds. */
  key_account_id?: string | null
}

/** The grammar the server holds an env name to (`routes/agents.py::_checked_env`, the project environment's). */
export const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/

/** What a delete's 409 lists beside its sentence (`routes/agents.py::delete_agent`): the first few of each. */
export type InUse = { tasks: { id: number; title: string }[]; projects: { id: number; name: string }[] }

export function inUseOf(err: unknown): InUse {
  const body = err instanceof ApiError && err.body && typeof err.body === 'object'
    ? err.body as Partial<InUse>
    : {}
  return {
    tasks: Array.isArray(body.tasks) ? body.tasks : [],
    projects: Array.isArray(body.projects) ? body.projects : [],
  }
}

export const REMOVED = 'removed'
export const DISABLED = 'disabled'

/** Where connectors are contributed (the private catalog repo; its CONTRIBUTING.md is the procedure). */
export const CONNECTORS_REPO_URL = 'https://github.com/jaah-ai/agent-connectors'

/* `org`: another org's (an operator's), read as it (`lib/actAs.ts::readAsOnce`). */
export const listAgents = (org: OrgRef | null = null) => get<Agent[]>('/api/agents', ...readAsOnce(org))
export const registerAgent = (body: AgentRegistration) => post<Agent>('/api/agents', body)
export const updateAgent = (id: string, body: AgentEdit) => patch<Agent>(`/api/agents/${encodeURIComponent(id)}`, body)
export const refreshAgent = (id: string) => post<Agent>(`/api/agents/${encodeURIComponent(id)}/refresh`)
export const deleteAgent = (id: string) => del(`/api/agents/${encodeURIComponent(id)}`)

export const listCatalog = () => get<Agent[]>('/api/agent-catalog')
export const setConnectorEnabled = (id: string, enabled: boolean) =>
  post<Agent>(`/api/agent-catalog/${encodeURIComponent(id)}/${enabled ? 'enable' : 'disable'}`)
export const publishConnector = (body: AgentRegistration) => post<Agent>('/api/agent-catalog', body)
export const removeConnector = (id: string) =>
  patch<Agent>(`/api/agent-catalog/${encodeURIComponent(id)}`, { status: REMOVED })

/** `RegistryEntryOut`: one ACP registry entry, with whether this org can install it and what it installed. */
export type RegistryEntry = {
  id: string
  name: string
  version: string
  description: string
  authors: string[]
  license: string | null
  website: string | null
  repository: string | null
  /** The icon inlined as a `data:image/svg+xml|png;base64,…` URI (`app/acp_registry_icons.py`), or null. */
  icon: string | null
  distribution: 'binary' | 'npx' | 'uvx' | null
  key_delivery: string | null
  installable: boolean
  /** Why it cannot be installed (e.g. "Needs key in environment, not supported"); null when installable. */
  refusal: string | null
  installed_agent_id: string | null
  installed_version: string | null
}

export const ACP_REGISTRY = 'acp_registry'

export const listRegistry = () => get<RegistryEntry[]>('/api/agent-registry')
export const installRegistryAgent = (id: string) =>
  post<Agent>(`/api/agent-registry/${encodeURIComponent(id)}/install`)

export const listAgentChoices = (persona?: string | null, org: OrgRef | null = null) =>
  get<AgentChoice[]>(`/api/agents/choices${persona ? `?persona=${encodeURIComponent(persona)}` : ''}`, ...readAsOnce(org))

/** A refusal as the sentences to list. A failed snapshot answers `{detail, errors: [sentence, …]}`
 *  (`routes/agents.py::_refused`), each its own line; anything else is `apiDetail`'s one sentence. */
export function refusalSentences(err: unknown, fallback: string): string[] {
  if (err instanceof ApiError && err.body && typeof err.body === 'object') {
    const errors = (err.body as { errors?: unknown }).errors
    if (Array.isArray(errors)) {
      const sentences = errors.filter((e): e is string => typeof e === 'string' && e !== '')
      if (sentences.length > 0) return sentences
    }
  }
  return [apiDetail(err, fallback)]
}

/** The commit as people read it: seven characters, or null before the first snapshot. */
export function shortCommit(sha: string | null | undefined): string | null {
  return sha ? sha.slice(0, 7) : null
}

/** "Name · v1.2.0" — an agent named for people, never by id or slug (AC-4). */
export function agentVersionLabel(name: string, version: string | null | undefined): string {
  return version ? `${name} · v${version.replace(/^v/, '')}` : name
}
