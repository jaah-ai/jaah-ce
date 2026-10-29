/* `GET /api/hosts` as the `/nodes` roster reads it: every machine as a host with its nodes beneath (Architecture
 * §5). The Add node dialog reads a subset of the same row (`components/addNode/api.ts::HostRow`); this is the
 * whole of it. A tenant's nodes on machines shared with other organizations come back as one `shared` entry
 * with no id or name, and nothing here may reveal more. */
import { get } from '../../../api'
import { setPageParams, type ListPageState, type Page } from '../../../hooks/useListPage'
import type { HostRow, OrderStage } from '../../../components/addNode/api'
import type { CloudProvider } from '../../../types'

export type HostHosting = {
  id: string
  kind: string
  billing: string
  status: string
  period_end: string | null
  cancel_at: string | null
  stop_cause?: string | null
  /** True when deleting it now refunds the unused time to the card; false when it is credited to the next bill. */
  refund_on_delete?: boolean
}

/** The machine itself: each fact null when the server could not tell. */
export type HostHardware = {
  instance_type: string | null
  vcpu: number | null
  memory_gib: number | null
  arch: 'x64' | 'arm64' | null
  machine: 'cloud' | 'vm' | 'physical' | null
  /** The cloud a `cloud` machine runs on; null otherwise (absent from an older server = AWS). */
  cloud?: CloudProvider | null
  /** The hypervisor or cloud it runs under, as the machine reported it. */
  virtualization: string | null
  /** Whether it can run MicroVMs (`/dev/kvm` on Linux, KVM under WSL2, Virtualization.framework); on a VM that is
   *  nested virtualization. */
  kvm: boolean | null
  /** The hypervisor MicroVMs run on; null (or absent from an older server) when the node has not reported one. */
  hypervisor?: 'kvm' | 'kvm-wsl2' | 'vz' | null
  /** Why it can't run MicroVMs, as the server's one sentence; null when it can. */
  hypervisor_blocker?: string | null
  /** The cloud machine's disk, from its node record's `cloud.disk_gb`: the host page adds it, `GET /api/hosts` carries
   *  none. */
  disk_gb?: number | null
}

export type HostNode = HostRow['nodes'][number] & {
  state: HostRow['state']
  project_count: number
  /** The node's last reported fault in its host's words (`reason: detail`); null when none. */
  failure_reason?: string | null
  /** When its placement last changed; null for a node with no placement. */
  updated_at?: string | null
}

/** Where the host's setup of a joining node stands (`hosts.py::_converge_out`). */
export type HostConverge = {
  /** `pickup`: the host has not taken the node yet; `building`: setting it up; `retrying`: its last try failed. */
  step: 'pickup' | 'building' | 'retrying'
  /** The failed try, in the host's words; null unless `retrying`. */
  error?: string | null
  /** When the host's converge loop last asked for its nodes; null until it has. */
  polled_at?: string | null
}

export type HostView = Omit<HostRow, 'nodes'> & {
  /** What the state means, in a sentence; null when ready. */
  state_reason: string | null
  os: string | null
  rental: HostHosting | null
  nodes: HostNode[]
  /** An Add node order still under way (or failed) on this host. */
  order: {
    id: string
    stage: OrderStage
    node_id?: string | null
    /** The name of the node the order adds. */
    name?: string | null
    /** Why it failed, as a sentence; null unless `failed`. */
    error?: string | null
    /** The stage it failed in; null unless `failed`. */
    failed_stage?: string | null
    created_at?: string | null
    /** When its row last changed — for an open order, usually when it entered its stage. */
    updated_at?: string | null
    /** The order's machine has an instance: a `launching` order is then booting, not launching. */
    instance_started?: boolean
    /** When its machine last fetched the install script; null = the install command has not run. */
    install_fetched_at?: string | null
    /** A `joining` order's node as its host's converge has it; null (or absent) at any other stage. */
    converge?: HostConverge | null
  } | null
  /** Operator only: the last lines of the machine's boot log while it has not connected; null when none was read. */
  boot_log?: { lines: string[]; at: string } | null
  /** Operator only: the machine's node name — what Change org asks to be typed; can differ from `name`. */
  machine_name?: string | null
  /** Operator only: how many Shared hosting nodes the host takes. */
  shared_capacity?: number | null
  hardware?: HostHardware | null
  /** Operator only: the cloud instance behind the machine; null when it has none. */
  instance?: {
    instance_id: string
    region: string | null
    account_id: string | null
    image_id?: string | null
    /** Its cloud, zone and scope (`clouds.InstanceRef`); absent from an older server = AWS. */
    provider?: CloudProvider | null
    zone?: string | null
    scope?: string | null
  } | null
}

export const HOSTS_KEY = ['hosts'] as const

/* ── THE HOSTS TAB'S PAGED READ ─────────────────────────────────────────────────────────────────
   The same `GET /api/hosts`, searched (`q`), narrowed (`state`), sorted and numbered-paged by the server
   (`hooks/useListPage.ts`), with the filtered set's `total`. `listAllHosts` below stays the Nodes tab's
   auxiliary read (faults, open orders, delete doors); the Hosts tab draws from these pages. */

/** `HostOut.state`'s vocabulary, as the strip offers it; `satisfies` pins every entry to `HostView['state']`,
 *  the type the labels (`hostFacts.tsx::HOST_STATE`) are keyed by, so the three cannot name a state apart. */
export const HOST_STATES = ['provisioning', 'ready', 'failed', 'stopped', 'deleting'] as const satisfies readonly HostView['state'][]

export type HostFilters = {
  /** Host name (and, for the operator, the machine's own name). '' = none. */
  q: string
  /** One of `HOST_STATES`, or ''. */
  state: string
}

export const hasHostFilters = (filters: HostFilters): boolean => Boolean(filters.q || filters.state)

/** The server's sorts (`routes/hosts.py::_SORTS`); the `shared` entry sits last under every one. */
export const HOST_SORTS = ['name', 'state', 'nodes'] as const

export const HOST_PAGE_KEY = ['hosts-page'] as const

export function hostPagePath(filters: HostFilters, page: ListPageState): string {
  const params = new URLSearchParams()
  if (filters.q.trim()) params.set('q', filters.q.trim())
  if (filters.state) params.set('state', filters.state)
  return `/api/hosts?${setPageParams(params, page).toString()}`
}

/* The server's page ceiling (`list_page.py`'s `per` bound): the fewest reads for the whole set. */
const ALL_PER = 200
/* Bounded: a server whose `total` kept outrunning its pages must not spin the page. */
const ALL_MAX_PAGES = 50

/** Every host the viewer may see, every page of it, in the server's order. */
export async function listAllHosts(): Promise<HostView[]> {
  const out: HostView[] = []
  for (let page = 1; page <= ALL_MAX_PAGES; page++) {
    const answer = await get<Partial<Page<HostView>>>(`/api/hosts?page=${page}&per=${ALL_PER}`)
    if (!answer || !Array.isArray(answer.items) || typeof answer.total !== 'number') throw new Error('Unexpected hosts answer')
    out.push(...answer.items)
    // An empty page ends the walk too: the set shrank under it, and the next page is emptier still.
    if (out.length >= answer.total || answer.items.length === 0) break
  }
  return out
}
