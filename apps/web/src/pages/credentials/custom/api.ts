// Custom connectors' ONE API client (`apps/api/app/routes/integrations_custom.py`): an org's own REST APIs and MCP
// servers, and the connections (secrets) made to them. Every member reads; defining one is an Org Owner/Admin's.

import { del, get, post, put } from '../../../api'
import type { SealedSecretEnvelope } from '../../../lib/credentialSeal'

export type ConnectorKind = 'rest' | 'mcp'
export type AuthScheme = 'bearer' | 'header_key' | 'basic' | 'oauth2_client' | 'none'
export type McpTransport = 'streamable_http' | 'sse'
/** The inputs Connect may ask for (`custom.SECRET_INPUTS`). */
export type SecretInput = 'api_key' | 'login' | 'password' | 'client_id' | 'client_secret' | 'bearer_token'

export type CustomConnection = {
  id: string
  name: string
  owner: 'org' | 'personal'
  last_tested_at: string | null
  last_test_ok: boolean | null
  last_test_message: string | null
  needs_reconnect: boolean
  /** The caller may Test (and edit) it. */
  editable: boolean
}

/** A definition as read: its `CustomConnectorBody` plus what the server owns (ids, stamps, its connections). */
export type CustomConnector = CustomConnectorBody & {
  id: string
  slug: string
  credential_type: string
  /** `{input: required}`: what Connect asks for. */
  secret_inputs: Partial<Record<SecretInput, boolean>>
  /** What a connection's secrets are sealed to in the browser. */
  sealing_destination: string
  created_at: string
  updated_at: string
  connections: CustomConnection[]
}

/** A whole definition (PUT replaces it): a field the kind and scheme do not take is sent as null. */
export type CustomConnectorBody = {
  name: string
  description: string
  kind: ConnectorKind
  base_url: string
  auth_scheme: AuthScheme
  header_name: string | null
  value_prefix: string | null
  oauth_authorize_url: string | null
  oauth_token_url: string | null
  oauth_scope: string | null
  test_path: string | null
  mcp_transport: McpTransport | null
}

/** A new connection: secrets sealed in the browser under `id` (`lib/credentialSeal.ts`); a login or client id as typed. */
export type ConnectionBody = { name: string; owner: 'org' | 'personal'; id: string }
  & Partial<Record<SecretInput, string | SealedSecretEnvelope>>

export type ConnectionTest = { ok: boolean; status: 'success' | 'failure' | 'unsupported'; message: string; detail: string | null }

/** Where the org's connectors are listed: the New secret gallery's Custom category, each connector its own card. */
export const CUSTOM_GALLERY_URL = '/credentials/new?category=custom'
const CUSTOM_BASE = '/credentials/custom'
export const newConnectorUrl = (kind: ConnectorKind) => `${CUSTOM_BASE}/new?kind=${kind}`
export const connectorUrl = (id: string, tab?: 'connections') =>
  `${CUSTOM_BASE}/${encodeURIComponent(id)}${tab ? `?tab=${tab}` : ''}`
/** A connection is a secret: its own page (Connect, rename, delete) is the secret editor. */
export const connectionUrl = (id: string) => `/credentials/${encodeURIComponent(id)}`

const PATH = '/api/integrations/custom'
const one = (id: string) => `${PATH}/${encodeURIComponent(id)}`

export const getConnector = (id: string) => get<CustomConnector>(one(id))
export const createConnector = (body: CustomConnectorBody) => post<CustomConnector>(PATH, body)
export const updateConnector = (id: string, body: CustomConnectorBody) => put<CustomConnector>(one(id), body)
export const deleteConnector = (id: string) => del(one(id))
export const createConnection = (id: string, body: ConnectionBody) =>
  post<CustomConnection>(`${one(id)}/connections`, body)
export const testConnection = (id: string, credentialId: string) =>
  post<ConnectionTest>(`${one(id)}/test`, { credential_id: credentialId })

export const KIND_LABEL: Record<ConnectorKind, string> = { rest: 'REST API', mcp: 'MCP server' }

export const SCHEME_LABEL: Record<AuthScheme, string> = {
  bearer: 'Bearer token',
  header_key: 'Header key',
  basic: 'Basic',
  oauth2_client: 'OAuth2',
  none: 'None',
}

/** The schemes each kind takes (`custom.SCHEMES_BY_KIND`), the default first. */
export const SCHEMES_BY_KIND: Record<ConnectorKind, readonly AuthScheme[]> = {
  rest: ['bearer', 'header_key', 'basic', 'oauth2_client'],
  mcp: ['none', 'bearer', 'oauth2_client'],
}

/** The REST schemes whose Base URL takes any text (`custom.FREE_FORM_SCHEMES`): the form warns, never refuses. */
export const FREE_FORM_SCHEMES: ReadonlySet<AuthScheme> = new Set(['bearer', 'header_key', 'basic'])

/** The inputs that are secrets, sealed before they leave the page; a login and a client id are not. */
export const SEALED_INPUTS: ReadonlySet<SecretInput> = new Set(['api_key', 'password', 'client_secret', 'bearer_token'])

export const TRANSPORT_LABEL: Record<McpTransport, string> = { streamable_http: 'Streamable HTTP', sse: 'SSE' }
