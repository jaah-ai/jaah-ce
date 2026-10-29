/* The Add node flow's wire: the hosting catalogue, the hosts a node can join, name suggestions and the order
 * pipeline (Architecture §5, the pinned contracts). Every shape here is the server's; nothing is priced or
 * staged on this side except the live total (`pricing.ts`), which the server re-prices on submit. */
import { get, post } from '../../api'
import type { Page } from '../../hooks/useListPage'
import type { NodeOs } from '../../lib/nodeOs'
import { onboardingScript } from '../NodeOnboardingDialog'

/** How a node is made — a `POST /api/node-orders` path. */
export type OrderPath = 'rent_dedicated' | 'rent_shared' | 'cloud' | 'self_hosted' | 'co_locate'

export type Offering = { enabled: boolean; reason: string | null }

export type CatalogInstanceType = {
  key: string
  name: string
  vcpu: number | null
  ram_gb: number | null
  monthly_cents: number
  /** False for a STALE size — its type is no longer allowed: listed so a picker can show it, never orderable. */
  available: boolean
  /** Why it is not available ("Not available"); null when it is. */
  unavailable_reason: string | null
  /** The monthly Windows license on this type; null = Windows is not sold on it. Absent from an older server. */
  windows_monthly_cents?: number | null
}

export type CatalogStorage = { gb: number; name: string; monthly_cents: number }
/** `disk_gb`: the disk one Shared node on the tier may use (null only on a tier saved before it was required). */
export type CatalogSharedTier = { key: string; name: string; disk_gb: number | null; monthly_cents: number }

/** `GET /api/rentals/catalog` — what the org may host, at what price, and which path to pre-select. */
export type HostingCatalog = {
  currency: string
  offerings: { dedicated_monthly: Offering; shared: Offering }
  instance_types: CatalogInstanceType[]
  storage: CatalogStorage[]
  shared_tiers: CatalogSharedTier[]
  /** The smallest storage a Windows Dedicated host is sold with (its root disk floor). */
  windows_min_storage_gb?: number
  saved_card: { brand: string | null; last4: string | null } | null
  recommended: OrderPath
  /** Operator only: the console path of the setting `offerings.dedicated_monthly.reason` names; null otherwise. */
  setup_href?: string | null
  /** Why no node can be added for this org right now, by any way; null when one can. Said on the first step. */
  refusal?: string | null
  /** Operator only (`/api/billing/plans/product-catalog`): Live Cast's per-project price and Backfill default.
   *  `offered` false = no price yet, so a Live Cast line is refused. */
  live_cast?: LiveCastOffer
}

export type LiveCastOffer = { offered: boolean; monthly_cents: number | null; default_backfill: number }

export type HostState = 'provisioning' | 'ready' | 'failed' | 'stopped' | 'deleting'

/** One `GET /api/hosts` row, the fields this flow reads. A tenant's `shared` group has no id or name. */
export type HostRow = {
  id: string | null
  name: string | null
  /** `failed_order`: a failed Add node whose machine is gone — no id, no host page; `order` says why. */
  kind: 'machine' | 'legacy' | 'shared' | 'failed_order'
  state: HostState
  origin: 'rented' | 'cloud' | 'self_hosted' | 'operator' | null
  /** The operator allocated this machine to you: only the operator changes or deletes it (`app/node_allocation.py`). */
  operator_allocated?: boolean
  can_add_nodes: boolean
  add_nodes_reason: string | null
  nodes: { id: string; name: string }[]
  /** Operator only. */
  org_id?: string
}

/** Where a host's machine comes from, in the words its owner reads (the dialog's host picker and `/nodes`). */
export const HOST_ORIGIN_LABEL: Record<NonNullable<HostRow['origin']>, string> = {
  rented: 'Jaah hosted', cloud: 'Your cloud account', self_hosted: 'Your machine', operator: 'Jaah',
}

export type NameSuggestion = { name: string | null; ok: boolean; message: string | null }

export type OrderStage = 'paying' | 'launching' | 'joining' | 'placing' | 'waiting_for_machine' | 'ready' | 'failed'

export type PaymentOutcome = {
  status: 'paid' | 'requires_action' | 'declined'
  client_secret?: string | null
  message?: string | null
}

/** The answer to a create, a confirm and a retry. */
export type OrderCreated = {
  order: { id: string; stage: OrderStage; error: string | null }
  payment?: PaymentOutcome | null
  /** Self-hosted: the one command the machine runs. Shown once; never re-served. */
  onboarding_command?: string | null
  /** Self-hosted: run the command inside the tailnet join (a console the internet cannot reach). */
  onboarding_joins_tailnet?: boolean
  /** The headscale server to join; null = no tailnet configured. */
  tailnet_login_server?: string | null
}

/** What `OrderProgress` shows a self-hosted machine: the one command it runs. */
export type OrderCommand = {
  command: string
  /** The machine's OS: the steps after the command (`NodeOsGuide`). */
  os?: NodeOs
  /** The command is wrapped in the interactive tailnet join, so it cannot run as user-data. */
  joinsTailnet?: boolean
}

export function orderCommand(created: Omit<OrderCreated, 'order' | 'payment'>, os?: NodeOs): OrderCommand | null {
  if (!created.onboarding_command) return null
  const command = onboardingScript(created.onboarding_command, created.onboarding_joins_tailnet, created.tailnet_login_server ?? null)
  return { command, os, joinsTailnet: Boolean(created.onboarding_joins_tailnet) }
}

export type NodeOrderView = {
  id: string
  path: string
  name: string
  stage: OrderStage
  stages: OrderStage[]
  error: string | null
  host_id: string | null
  node_id: string | null
  /** The stage it failed in; null unless `failed` (absent from an older server). */
  failed_stage?: OrderStage | null
  /** Its machine's cloud instance exists, so a `launching` order is booting (absent from an older server). */
  instance_started?: boolean
  /** When its machine last fetched the install script; null = the command has not run (absent from an older server). */
  install_fetched_at?: string | null
  /** When it entered its stage: the countdown runs from it (absent from an older server). */
  updated_at?: string
  /** The machine's OS and cloud instance type, for the boot estimate; null when the order does not launch one
   *  (absent from an older server). */
  os?: NodeOs | null
  instance_type?: string | null
}

export type HostingBody = {
  kind: 'dedicated' | 'shared'
  billing: 'monthly'
  instance_type?: string
  storage_gb?: number
  tier_key?: string
  /** Dedicated only; absent = Ubuntu. Windows takes only a type whose `windows_monthly_cents` is set. */
  os?: 'windows'
  payment_method_id?: string
}

export type NodeOrderBody = {
  org_id: string
  path: OrderPath
  name: string
  project_ids: number[]
  /* The org's NodeLinks the new node is born attached to (cloud, self-hosted, co-located only). Absent = the
     server's default: the accounts NodeLink iff the org's NodeLink allows it; `[]` = none; a non-empty list
     while the org's NodeLink is off is a 403, another org's id a 404. */
  nodelinks?: string[]
  rent?: HostingBody
  /* `os` absent = Ubuntu. Windows takes only a type from the options' `windows_instance_types`. */
  cloud?: { connection_id: string; instance_type: string; os?: NodeOs }
  self_hosted?: { os: NodeOs }
  co_locate?: { host_id: string }
}

const q = encodeURIComponent

export const getHostingCatalog = (orgId: string) => get<HostingCatalog>(`/api/rentals/catalog?org_id=${q(orgId)}`)
export const listHosts = (orgId: string) =>
  get<Page<HostRow>>(`/api/hosts?org_id=${q(orgId)}&per=200`)

/** A name to pre-fill (`host_id`: `{host}-{n}`; none: `{org}-shared-{n}`), and, with `name`, whether it can be used. */
export function suggestNodeName(orgId: string, opts: { hostId?: string; name?: string } = {}) {
  const params = [`org_id=${q(orgId)}`]
  if (opts.hostId) params.push(`host_id=${q(opts.hostId)}`)
  if (opts.name !== undefined) params.push(`name=${q(opts.name)}`)
  return get<NameSuggestion>(`/api/node-names/suggest?${params.join('&')}`)
}

export const createNodeOrder = (body: NodeOrderBody) => post<OrderCreated>('/api/node-orders', body)
export const confirmNodeOrder = (id: string) => post<OrderCreated>(`/api/node-orders/${q(id)}/confirm`)
export const retryNodeOrder = (id: string) => post<OrderCreated>(`/api/node-orders/${q(id)}/retry`)
/** A new command for a self-hosted machine that hasn't joined yet; the earlier one stops working. */
export const reissueOrderCommand = (id: string) =>
  post<{ onboarding_command: string } & Omit<OrderCreated, 'order' | 'payment' | 'onboarding_command'>>(`/api/node-orders/${q(id)}/command`)
/** Clears a failed order off its host; 409 unless it failed. */
export const dismissNodeOrder = (id: string) => post<{ dismissed: true }>(`/api/node-orders/${q(id)}/dismiss`)
export const getNodeOrder = (id: string) => get<NodeOrderView>(`/api/node-orders/${q(id)}`)
/** The failed Cloud order whose launch removed node `nodeId` (404 when none): why a link to it finds no node. */
export const getFailedOrderForNode = (nodeId: string) => get<NodeOrderView>(`/api/node-orders/for-node/${q(nodeId)}`)

export const nodeOrderKey = (id: string) => ['node-order', id] as const

/** A stage's words, while it runs and once it is past (Architecture G5's labels, in the past tense when done). */
export const STAGE_LABELS: Record<Exclude<OrderStage, 'failed'>, { active: string; done: string }> = {
  paying: { active: 'Paying', done: 'Paid' },
  launching: { active: 'Launching', done: 'Launched' },
  joining: { active: 'Joining', done: 'Joined' },
  placing: { active: 'Placing', done: 'Placed' },
  waiting_for_machine: { active: 'Waiting for your machine', done: 'Machine connected' },
  ready: { active: 'Ready', done: 'Ready' },
}
/** A `launching` order whose machine exists: it is booting, no longer being launched. */
export const BOOTING_LABEL = 'Booting'
/** A `waiting_for_machine` order whose machine fetched its install script: it is installing, no longer awaited. */
export const INSTALLING_LABEL = 'Installing on your machine'
