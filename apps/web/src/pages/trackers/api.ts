import { del, get, patch, post, put } from '../../api'
import type { OrgRef } from '../../types'

/* ISSUE-TRACKER CONNECTIONS ON THE WIRE — `apps/api/app/routes/trackers.py`, field for field. Every route is
 * `@org_admin`. Server vocabularies stay `string`: a tracker or auth kind this console does not know yet renders as
 * itself. */

export type TrackerMapping = { container_id: string; project_id: number }

export type TrackerSettings = {
  mappings: TrackerMapping[]
  start_label: string
  /** null: the tracker's own done state. */
  done_state: string | null
  allow_unmapped_users: boolean
}

export type TrackerConnection = {
  id: string
  name: string
  tracker: string
  auth_kind: string
  site: string | null
  /** The tenant it belongs to; its Mapping offers only this org's projects. */
  org: OrgRef | null
  external_org_id: string | null
  credential_id: string | null
  trigger_id: string | null
  automation_id: string | null
  settings: TrackerSettings
  claimed: boolean
  /** A site admin adds this connection's webhook by hand: New webhook URL may replace its URL and secret. */
  has_own_webhook: boolean
  /** Where a Jira admin installs Jaah's Forge app; null for any other connection or when none is configured. */
  install_url: string | null
  /** Its automation's owner is no member of the connection's org, so every start is refused until an Owner or Admin
   *  takes ownership (`POST /api/automations/{id}/take-ownership`). */
  owner_outside_org: boolean
  created_at: string | null
  updated_at: string | null
}

/** A PATCH's settings: an absent key keeps its value. */
export type TrackerSettingsPatch = Partial<TrackerSettings>

export type TrackerContainer = { id: string; name: string }

/** One logged event (`TriggerDelivery`); `preview` is the intake's stored copy (see `eventPreview`). */
export type TrackerEvent = {
  delivery_id: number
  state: string
  outcome: string
  reason: string | null
  task_id: number | null
  received_at: string | null
  preview: unknown
}

export const TRACKERS_PATH = '/integrations/trackers'
export const NEW_TRACKER_PATH = `${TRACKERS_PATH}/new`
export const trackerPath = (id: string) => `${TRACKERS_PATH}/${encodeURIComponent(id)}`

export const TRACKERS_KEY = ['tracker-connections'] as const
export const trackerKey = (id: string) => ['tracker-connection', id] as const

const base = (id: string) => `/api/tracker-connections/${encodeURIComponent(id)}`

export const listTrackerConnections = () =>
  get<{ connections: TrackerConnection[] }>('/api/tracker-connections').then(r => r.connections ?? [])
export const getTrackerConnection = (id: string) => get<TrackerConnection>(base(id))
export const updateTrackerConnection = (id: string, settings: TrackerSettingsPatch) =>
  patch<TrackerConnection>(base(id), { settings })
/** A connect's body: `credential_id` null only for a Jira Forge app (it holds its own token). `in_operator_org`: an
 *  operator not acting as an org connects into the operator org only by saying so (else 422). */
export type TrackerConnectIn = {
  tracker: string; auth_kind: string; credential_id: string | null; site: string | null; in_operator_org?: boolean
}
/** A connect's answer. `webhook_secret` (Backlog, a Jira bot user) and `claim_code` (a Jira Forge app) come back
 *  this once and never again; null when the tracker needs none (Linear). */
export type TrackerConnected = {
  connection: TrackerConnection
  ingress_url: string
  webhook_secret: string | null
  claim_code: string | null
}
/** 409 while the tracker's adapter is not shipped (`trackers/connections.py::connect`). */
export const createTrackerConnection = (body: TrackerConnectIn) =>
  post<TrackerConnected>('/api/tracker-connections', body)
export const deleteTrackerConnection = (id: string) => del(base(id))
/** A new ingress URL and signing secret, this once; the old pair stops working at once. Not for Linear or a Jira Forge
 *  app (they post to no URL of their own). */
export const rotateTrackerWebhook = (id: string) =>
  post<{ ingress_url: string; webhook_secret: string }>(`${base(id)}/webhook-secret`)
/** 409 while the tracker's adapter is not installed or cannot list them (its reason the detail): the mapping then
 *  takes a typed container id. */
export const listTrackerContainers = (id: string) =>
  get<{ containers: TrackerContainer[] }>(`${base(id)}/containers`).then(r => r.containers ?? [])
/** One webhook place and what posts from it (`trackers/protocol.py::WebhookCheck`). `url`: the address to paste
 *  there, when the server can rebuild it (Backlog); never for a Jira webhook, whose token is kept hashed. */
export type TrackerWebhookCheck = {
  where: string
  state: 'ok' | 'stale' | 'off' | 'missing' | 'unknown' | string
  detail: string
  url: string | null
}
export type TrackerWebhooks = { checks: TrackerWebhookCheck[] }
/** Read live from the tracker: may take seconds. */
export const getTrackerWebhooks = (id: string) => get<TrackerWebhooks>(`${base(id)}/webhooks`)
export const listTrackerEvents = (id: string) =>
  get<{ events: TrackerEvent[] }>(`${base(id)}/events`).then(r => r.events ?? [])

const TRACKER_LABELS: Record<string, string> = { linear: 'Linear', jira: 'Jira', backlog: 'Backlog' }
const AUTH_LABELS: Record<string, string> = { oauth: 'OAuth', basic: 'API token', forge: 'Forge app' }

export const trackerLabel = (tracker: string): string => TRACKER_LABELS[tracker] ?? tracker
export const authLabel = (kind: string): string => AUTH_LABELS[kind] ?? kind
/** What the tracker calls a container — its own word, e.g. "Jira project", "Linear team". */
export const containerNoun = (tracker: string): string => `${trackerLabel(tracker)} ${tracker === 'linear' ? 'team' : 'project'}`
/** "a Jira project", "an Asana project". */
export const withArticle = (noun: string): string => `${/^[aeiou]/i.test(noun) ? 'an' : 'a'} ${noun}`

/** The start label when none was saved (`trackers/vocabulary.py::DEFAULT_START_LABEL`). */
export const DEFAULT_START_LABEL = 'jaah'

/** The distinct projects a connection maps to. */
export const mappedProjectCount = (conn: TrackerConnection): number =>
  new Set((conn.settings?.mappings ?? []).map(m => m.project_id)).size

export type EventPreview = { kind?: string; issue_key?: string; issue_url?: string; actor?: string; text?: string }

/** The `tracker` block of an event's stored preview — an object, or the intake's `{text: <JSON>, truncated}` copy
 *  (`trigger_intake.preview_of`). A truncated or malformed copy yields nothing rather than a throw. */
export function eventPreview(preview: unknown): EventPreview | null {
  let value = preview
  for (let depth = 0; depth < 3; depth += 1) {
    if (typeof value === 'string') {
      try {
        value = JSON.parse(value)
      } catch {
        return null
      }
      continue
    }
    if (value === null || typeof value !== 'object') return null
    const record = value as Record<string, unknown>
    if (record.tracker && typeof record.tracker === 'object') {
      const t = record.tracker as Record<string, unknown>
      const text = (k: string) => (typeof t[k] === 'string' ? (t[k] as string) : undefined)
      return { kind: text('kind'), issue_key: text('issue_key'), issue_url: text('issue_url'), actor: text('actor'), text: text('text') }
    }
    if (typeof record.text !== 'string') return null
    value = record.text
  }
  return null
}

/* A PROJECT'S TRACKER LINKS — `apps/api/app/routes/project_trackers.py` (`projects:view` reads, `projects:edit` writes).
 * One link model with the Mapping tab: a link made here is a row there, and the reverse. Labels come from the server
 * (`tracker_label`); a container is the tracker's project or team. */

export type ProjectTrackerContainer = { container_id: string; name: string; primary: boolean; shared: number }
export type ProjectTrackerConnection = {
  id: string; tracker: string; tracker_label: string; name: string; auth_kind: string; site: string | null; error: string | null
}
export type ProjectTrackerLink = {
  connection: ProjectTrackerConnection
  containers: ProjectTrackerContainer[]
  /** Linked with no tracker project picked yet. */
  needs_pick: boolean
}
export type ProjectTrackers = { links: ProjectTrackerLink[]; can_edit: boolean; can_connect: boolean }
export type AvailableTracker = { id: string; tracker: string; tracker_label: string; name: string; auth_kind: string }

const projectBase = (projectId: number) => `/api/projects/${projectId}/trackers`
const projectConn = (projectId: number, connectionId: string) => `${projectBase(projectId)}/${encodeURIComponent(connectionId)}`

export const projectTrackersKey = (projectId: number) => ['project-trackers', projectId] as const

export const listProjectTrackers = (projectId: number) => get<ProjectTrackers>(projectBase(projectId))
export const listAvailableTrackers = (projectId: number) =>
  get<{ connections: AvailableTracker[] }>(`${projectBase(projectId)}/available`).then(r => r.connections ?? [])
/** 502 with the tracker's own sentence when it cannot list them. */
export const listProjectTrackerContainers = (projectId: number, connectionId: string) =>
  get<{ containers: { container_id: string; name: string }[] }>(`${projectConn(projectId, connectionId)}/containers`)
    .then(r => (r.containers ?? []).map((c): TrackerContainer => ({ id: c.container_id, name: c.name })))
export const linkProjectTracker = (projectId: number, connectionId: string, containerIds: string[]) =>
  put<ProjectTrackers>(projectConn(projectId, connectionId), { container_ids: containerIds })
export const unlinkProjectTracker = (projectId: number, connectionId: string) => del(projectConn(projectId, connectionId))
export const makeProjectTrackerPrimary = (projectId: number, connectionId: string, containerId: string) =>
  post<ProjectTrackers>(`${projectConn(projectId, connectionId)}/primary`, { container_id: containerId })
