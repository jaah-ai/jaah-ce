import { del, get, post, put } from '../../../api'

/* THE INTEGRATION CATALOG ON THE WIRE — `apps/api/app/routes/integrations.py` (the gallery and one-click Connect) and
 * `routes/credentials/mcp_links.py` (project MCP links). The server owns every vendor fact; nothing here lists one. */

/** `IntegrationOut.auth_methods[].kind`, the spec's `[[auth]] kind`. */
export type AuthKind = 'oauth' | 'key' | 'basic' | 'service_account' | 'db' | 'mcp'

/** How a vendor's connections read on its card, worst first (`routes/integrations.py::WorstState`). */
export type WorstState = 'needs_reconnect' | 'test_failed' | 'untested' | 'ok'

export type Integration = {
  slug: string
  label: string
  category: string
  category_label: string
  summary: string
  /** The type a connection is saved as (`GET /api/credentials/types`). */
  credential_type: string
  logo_url: string | null
  /** In the spec's order: the first is the primary method. */
  auth_methods: { kind: AuthKind | string; primary: boolean }[]
  /** Present for a vendor with an OAuth method. `app_configured` false: Connect needs the person's own app. */
  /** `hooked`: the vendor's Connect fixes the row's owner and takes no project (a forge, Slack): no such choice. */
  /** `origin_field`: the type's url field Connect asks for first (Backlog's space URL), sent as `origin`. */
  oauth: {
    app_configured: boolean
    byo_allowed: boolean
    state: 'ready' | 'app_not_configured'
    hooked?: boolean
    origin_field?: string | null
  } | null
  mcp: { url: string; url_editable: boolean; transport: string } | null
  /** The reader page; '' when this deployment names no docs site. */
  docs_url: string
  vendor_docs_url: string
  /** The caller's visible connections of this type; null when there are none. */
  connected: { count: number; worst_state: WorstState } | null
  /** Whether the caller has voted for this Beta vendor (`voteIntegration`). */
  voted?: boolean
  custom?: boolean
  /** A custom connector's card (`custom: true`): the connector its define/detail page edits. */
  custom_connector_id?: string | null
}

export const INTEGRATIONS_KEY = ['integrations'] as const

export const listIntegrations = () => get<Integration[]>('/api/integrations')

/** Beta: no Jaah app, so the card offers Vote instead of Connect (the missing app itself is never named). */
export const isBeta = (item: Integration): boolean => item.oauth?.state === 'app_not_configured'

/** Can a Beta vendor still be connected, with an API key or the person's own OAuth app? */
export const connectableWithoutApp = (item: Integration): boolean =>
  item.oauth?.byo_allowed === true || item.auth_methods.some(method => method.kind !== 'oauth')

/** Cast (`true`) or withdraw (`false`) the caller's one vote for a Beta vendor. */
export const voteIntegration = (slug: string, on: boolean) => {
  const path = `/api/integrations/${encodeURIComponent(slug)}/vote`
  return on ? put<{ voted: boolean }>(path, {}) : del<{ voted: boolean }>(path)
}

/** One vendor on the operator's Settings › Integrations › OAuth apps list (`routes/integrations.py::IntegrationAppOut`). */
export type IntegrationApp = {
  slug: string
  label: string
  logo_url: string | null
  /** The `oauth_app.<slug>` knob that designates Jaah's app; null when it is set elsewhere or never. */
  app_knob: string | null
  beta: boolean
  votes: number
  connections: number
}

export const INTEGRATION_APPS_KEY = ['integration-apps'] as const

/** Most voted first. */
export const listIntegrationApps = () => get<IntegrationApp[]>('/api/integrations/apps')

export type IntegrationConnectStart = {
  name: string
  owner: 'org' | 'personal'
  project_ids?: number[]
  /** A person's own OAuth app (BYO); absent uses the deployment's. */
  client_id?: string
  client_secret?: string
  scopes?: string[]
  /** An origin-scoped vendor's host (`oauth.origin_field`), such as https://acme.backlog.com. */
  origin?: string
}

/** Begin a one-click Connect: no row exists until the vendor grants; the callback creates it. */
export const startIntegrationConnect = (slug: string, body: IntegrationConnectStart) =>
  post<{ authorize_url: string }>(`/api/integrations/${encodeURIComponent(slug)}/connect/start`, body)

/* ── Project MCP links ─────────────────────────────────────────────────────────────────────────────── */

export type ProjectMcpLink = {
  project_id: number
  credential: { id: string; name: string; type: string; type_label: string }
  url: string | null
  transport: string | null
  created_at: string
}

export type CredentialMcpLink = { project_id: number; project_name: string; created_at: string }

export const listProjectMcpLinks = (projectId: number) =>
  get<ProjectMcpLink[]>(`/api/projects/${projectId}/mcp-links`)

export const linkProjectMcp = (projectId: number, credentialId: string) =>
  put<ProjectMcpLink>(`/api/projects/${projectId}/mcp-links/${encodeURIComponent(credentialId)}`, {})

export const unlinkProjectMcp = (projectId: number, credentialId: string) =>
  del(`/api/projects/${projectId}/mcp-links/${encodeURIComponent(credentialId)}`)

export const listCredentialMcpLinks = (credentialId: string) =>
  get<CredentialMcpLink[]>(`/api/credentials/${encodeURIComponent(credentialId)}/mcp-links`)

/* ── Shared vocabulary ─────────────────────────────────────────────────────────────────────────────── */

/** A card's auth chips: OAuth, Key (every pasted kind), MCP — once each, in the spec's order. */
export function authChips(integration: Integration): string[] {
  const chips: string[] = []
  for (const method of integration.auth_methods) {
    const chip = method.kind === 'oauth' ? 'OAuth' : method.kind === 'mcp' ? 'MCP' : 'Key'
    if (!chips.includes(chip)) chips.push(chip)
  }
  if (integration.mcp && !chips.includes('MCP')) chips.push('MCP')
  return chips
}

/** A connection state's pill: label and tone (`ui/StatusChip`). One table for the card and the detail page. */
export const CONNECTION_STATE: Record<WorstState, { label: string; tone: 'success' | 'error' | 'warning' | 'neutral' }> = {
  ok: { label: 'Connected', tone: 'success' },
  test_failed: { label: 'Test failed', tone: 'error' },
  needs_reconnect: { label: 'Needs reconnect', tone: 'warning' },
  untested: { label: 'Untested', tone: 'neutral' },
}

/** The vendor Connect screen's path; `returnTo` (an in-app path) rides along so Save lands back there. */
export function integrationPath(slug: string, returnTo?: string | null): string {
  const params = new URLSearchParams({ integration: slug })
  if (returnTo) params.set('return', returnTo)
  return `/credentials/new?${params}`
}
