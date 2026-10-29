import type { JSX } from 'react'
import Box from '@mui/material/Box'
import { BOOTING_LABEL, HOST_ORIGIN_LABEL, STAGE_LABELS } from '../../../components/addNode/api'
import { countOf } from '../../../lib/humanize'
import { relativeAge } from '../../../lib/relativeTime'
import StatusChip, { type Tone } from '../../../ui/StatusChip'
import { PHONE } from '../../../ui/listStyles'
import type { CloudProvider, OrgRef } from '../../../types'
import type { HostConverge, HostHardware, HostView } from './api'

/* WHAT A HOST ROW SAYS ABOUT ITS MACHINE (AC1.4, R15c): its state as a chip — a word AND a glyph, so five
 * states stay apart without colour — the plain reason behind it, where the machine comes from, and the stage
 * of an Add node order still under way. On the host's page (`HostDetailPage.tsx`) the chips are its State fact and
 * the reason a wrapping line beside the facts, so nothing here is cut to fit a phone. */

export const HOST_STATE: Record<HostView['state'], { label: string; glyph: string; tone: Tone; reason: string }> = {
  provisioning: { label: 'Provisioning', glyph: '»', tone: 'info', reason: '' },
  ready: { label: 'Ready', glyph: '●', tone: 'success', reason: '' },
  failed: { label: 'Failed', glyph: '✕', tone: 'error', reason: '' },
  stopped: { label: 'Stopped', glyph: '■', tone: 'warning', reason: 'See the Billing page.' },
  deleting: { label: 'Deleting', glyph: '○', tone: 'neutral', reason: '' },
}

/** A ready host or node whose machine is not connected now: `ready` is what it was set up as, not that it is up. */
export const OFFLINE_STATE = { label: 'Offline', glyph: '○', tone: 'error' as const, reason: '' }

/** A host's state words; a state newer than this console reads as unknown, never raw. */
export function hostState(state: string) {
  return HOST_STATE[state as HostView['state']] ?? { label: 'Unknown', glyph: '?', tone: 'neutral' as const, reason: '' }
}

/** Where the machine comes from, for the person reading it. `size` is a hosted machine's
 *  "t4g.small · 50 GB" when the viewer can read it. Tenant copy names no AWS internals beyond instance type. */
export function originLabel(host: Pick<HostView, 'origin' | 'rental'>, opts: { operator: boolean; size?: string }): string {
  if (host.rental || host.origin === 'rented') return [HOST_ORIGIN_LABEL.rented, opts.size].filter(Boolean).join(' · ')
  if (!host.origin) return ''
  // The operator reads every org's machines: "your" would be wrong on a tenant's.
  return (opts.operator && OPERATOR_READS[host.origin]) || HOST_ORIGIN_LABEL[host.origin]
}

const OPERATOR_READS: Partial<Record<NonNullable<HostView['origin']>, string>> = {
  cloud: 'Organization’s cloud account', self_hosted: 'Organization’s machine',
}

/** The host's org as a chip can draw it: its door's, else any node record filed in the same org. `GET /api/hosts`
 *  carries only `org_id` (operator only), and a bare id is no answer for a person — null leaves the cell blank. */
export function hostOrgRef(
  host: Pick<HostView, 'org_id'>,
  door: { org?: OrgRef | null } | undefined,
  records: readonly { org?: OrgRef | null }[],
): OrgRef | null {
  if (door?.org) return door.org
  if (!host.org_id) return null
  return records.find(r => r.org?.id === host.org_id)?.org ?? null
}

/** An order's stage as a chip, or null once it is ready. */
export function orderChip(order: HostView['order']): { label: string; tone: Tone; glyph: string; failed: boolean } | null {
  if (!order || order.stage === 'ready') return null
  if (order.stage === 'failed') return { label: 'Add node failed', tone: 'error', glyph: '✕', failed: true }
  const words = STAGE_LABELS[order.stage]
  return { label: words ? words.active : 'In progress', tone: 'info', glyph: '»', failed: false }
}

/** The stage a failed order stopped in, as words a sentence can carry (`launching`); null when unknown. */
export function failedStageWords(order: Pick<NonNullable<HostView['order']>, 'failed_stage'>): string | null {
  const words = order.failed_stage ? STAGE_LABELS[order.failed_stage as keyof typeof STAGE_LABELS] : undefined
  return words ? words.active.toLowerCase() : null
}

/* ── ONE STATUS — the host's state and an open Add node order never draw as two chips that contradict
 * each other ("Ready" beside "Waiting for your machine"). While an order is under way, the order IS the status;
 * the machine's liveness (`machine`: its door's or roster record's presence) says which words it gets. ── */

/** How long an order may stand in one stage before the page says it is stuck. Placement follows the machine's
 *  first converge poll within seconds; ten minutes is far past any healthy first boot. */
export const ORDER_STALL_MS = 10 * 60_000

/** A machine that fetched its install script but has not reported in: what to expect (the install upgrades the OS first). */
export const FIRST_REPORT_WAIT = 'Installing. It upgrades the OS packages first, so this can take several minutes.'
/** A machine that never fetched its install script: the command has not run. Cloud-init skips user-data with no `#!`. */
export const INSTALL_NOT_RUN = 'Install command not run yet. As EC2 user-data, start it with #!/bin/bash.'
/** On the machine: what keeps a connected host from asking for its nodes is its daemon. */
export const STALLED_HINT = 'On the machine, check the node daemon: sudo systemctl status jaahd'

export type HostStatus = {
  label: string
  glyph: string
  tone: Tone
  /** What it means, as a sentence; empty when the chip says it all. */
  reason: string
  /** When the open order entered its stage; null when no order is under way. */
  since: string | null
  /** No progress for `ORDER_STALL_MS`. */
  stalled: boolean
  /** The machine heartbeats but has not asked for its nodes (the converge loop is not polling). */
  connectedNotPolling: boolean
}

/** The machine's presence, as `GET /api/vnode-hosts` or `GET /api/nodes` reports it. */
export type MachinePresence = { status?: string | null; last_seen?: string | null } | undefined

const msSince = (iso: string | null | undefined, now: number) => {
  const at = iso ? Date.parse(iso) : NaN
  return Number.isNaN(at) ? null : Math.max(0, now - at)
}

/** `12 min`, `2 h 5 min`, `3 days`: how long, in words a sentence can carry. */
export function waitWords(ms: number): string {
  const minutes = Math.floor(ms / 60_000)
  if (minutes < 1) return 'under a minute'
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`
  const days = Math.floor(hours / 24)
  return countOf(days, 'day')
}

/** A joining node's step on its host, by `HostConverge.step`. */
export const CONVERGE_STEP: Record<HostConverge['step'], string> = {
  pickup: 'Waiting for the host to pick up the node.',
  building: 'The host is setting up the node.',
  retrying: 'The host’s last try failed.',
}

/** How long a host's converge loop may go without polling before the page says it stopped. A healthy pass blocks
 *  in one node's setup for up to the helper's timeout (`vnode_converge.py::HELPER_TIMEOUT`, 15 min) plus one
 *  pass interval; past that the loop itself is stuck. */
export const CONVERGE_SILENT_MS = 16 * 60_000

/** What the host's converge is doing for a joining node: its step and its last failure (`step`), and — once a stalled
 *  order's host has not polled for `CONVERGE_SILENT_MS` and its machine is not offline — that it stopped, with the
 *  jaahd hint (`silence`, '' otherwise). The setup's few minutes are promised only while the order is not stalled. */
export function convergeWords(
  converge: HostConverge, stalled: boolean, machine: MachinePresence, now: number,
): { step: string; silence: string } {
  const error = converge.step === 'retrying' && converge.error ? converge.error.trim().replace(/\.$/, '') : ''
  const step = error ? `The host’s last try failed: ${error}. It retries on its own.`
    : converge.step === 'building' && !stalled ? `${CONVERGE_STEP.building} This can take several minutes.`
    : CONVERGE_STEP[converge.step]
  const silent = msSince(converge.polled_at, now)
  const silence = stalled && machine?.status !== 'offline' && silent !== null && silent >= CONVERGE_SILENT_MS
    ? `The host stopped reporting its progress ${waitWords(silent)} ago. ${STALLED_HINT}` : ''
  return { step, silence }
}

/** The one status a host shows: its open order's real stage while one is under way, else its own state. */
export function hostStatus(host: HostView, machine: MachinePresence, now: number = Date.now()): HostStatus {
  const order = host.order
  if (!order || order.stage === 'ready' || order.stage === 'failed') {
    if (host.state === 'ready' && machine?.status === 'offline') {
      const lastSeen = msSince(machine.last_seen, now)
      return {
        ...OFFLINE_STATE,
        reason: lastSeen !== null ? `Last seen ${waitWords(lastSeen)} ago.` : 'Not connected.',
        since: null, stalled: false, connectedNotPolling: false,
      }
    }
    const state = hostState(host.state)
    return {
      label: state.label, glyph: state.glyph, tone: state.tone, reason: host.state_reason || state.reason,
      since: null, stalled: false, connectedNotPolling: false,
    }
  }
  const since = order.updated_at ?? null
  const waited = msSince(since, now)
  const stalled = waited !== null && waited >= ORDER_STALL_MS
  const forHow = waited !== null ? ` Waiting ${waitWords(waited)}.` : ''
  // Seen at least once: the host's own state says so (`hosts.py::machine_is_up`). Presence alone says whether it
  // is up NOW; with none to read (no door, no record, or `unknown`), nothing is claimed either way.
  const seen = host.state !== 'provisioning'
  const online = machine?.status === 'online'
  const offline = machine?.status === 'offline'
  const beforePlacement = (order.stage === 'waiting_for_machine' || order.stage === 'launching') && !order.node_id
  if (beforePlacement && online) {
    return {
      label: 'Connected', glyph: '»', tone: stalled ? 'warning' : 'info',
      reason: stalled
        ? `Your machine is connected but hasn’t asked for its nodes.${forHow} ${STALLED_HINT}`
        : `Your machine is connected. Waiting for it to ask for its nodes.${forHow}`,
      since, stalled, connectedNotPolling: true,
    }
  }
  if (order.stage === 'waiting_for_machine') {
    const lastSeen = msSince(machine?.last_seen, now)
    // A legacy machine's order holds its node from the start, so it can be online and still waiting.
    const reason = online ? `Your machine is connected. Waiting for it to finish joining.${forHow}`
      : !seen ? `${order.install_fetched_at ? FIRST_REPORT_WAIT : INSTALL_NOT_RUN}${forHow}`
      : offline && lastSeen !== null ? `Your machine is offline: it last reported ${waitWords(lastSeen)} ago.${forHow}`
      : `Waiting for your machine to report in.${forHow}`
    return {
      label: online ? 'Connected' : STAGE_LABELS.waiting_for_machine.active, glyph: '»', tone: stalled ? 'warning' : 'info',
      reason, since, stalled, connectedNotPolling: false,
    }
  }
  // The launch went through and the machine is starting: its boot report is the reason.
  const booting = order.stage === 'launching' && order.instance_started === true
  const words = STAGE_LABELS[order.stage]
  const converge = order.stage === 'joining' && order.converge ? convergeWords(order.converge, stalled, machine, now) : null
  // The host's own reason first (a fault, a stopped hosting), then the step; the jaahd hint last, where the host page
  // sets its command in monospace at the end of the banner.
  const reason = [
    host.state_reason,
    converge?.step,
    stalled && waited !== null ? `No progress in ${waitWords(waited)}.` : forHow.trim(),
    converge?.silence,
  ]
  return {
    label: booting ? BOOTING_LABEL : words ? words.active : 'In progress', glyph: '»', tone: stalled ? 'warning' : 'info',
    reason: reason.filter(Boolean).join(' '), since, stalled, connectedNotPolling: false,
  }
}

const LINE_SX = {
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'center',
  columnGap: 1,
  rowGap: 0.5,
  pb: 0.5,
  fontSize: '0.75rem',
  color: 'text.secondary',
  [PHONE]: { columnGap: 0.75 },
} as const

const TEXT_SX = { minWidth: 0, overflowWrap: 'anywhere' } as const

/* A long state ("Waiting for your machine") wraps inside its fact's tile rather than spilling over the next
   one: `StatusChip` keeps its intrinsic width everywhere else, so the wrap is this cluster's. */
const CHIPS_SX = {
  display: 'inline-flex',
  flexWrap: 'wrap',
  alignItems: 'center',
  gap: 0.75,
  maxWidth: '100%',
  '& > span': { maxWidth: '100%', minWidth: 0 },
  '& .MuiChip-root': { maxWidth: '100%', height: 'auto', minHeight: 20 },
  '& .MuiChip-root .MuiChip-label': { flexShrink: 1, whiteSpace: 'normal', overflowWrap: 'anywhere', py: '2px' },
} as const

type HostFactsProps = {
  host: HostView
  /** Opens the order's live progress again. */
  onOpenOrder?: (orderId: string) => void
  /** The machine's presence (its door's), which decides an open order's words. */
  machine?: MachinePresence
  now?: number
}

/** What an order chip shows while pointed at: the press opens the order's progress (`ui/pressSwap.tsx`). */
const ORDER_PRESS = { glyph: '↗', label: 'Progress' }

/** An open order IS the status: its chip replaces the host's, so the two never contradict. */
const orderOpen = (host: HostView) => host.order !== null && host.order.stage !== 'ready' && host.order.stage !== 'failed'

/** The host's one status chip, and a settled (failed) order's beside it: the value of the page's State fact. */
export function HostStateChips({ host, onOpenOrder, machine, now }: HostFactsProps): JSX.Element {
  const status = hostStatus(host, machine, now)
  const open = orderOpen(host)
  const order = open ? null : orderChip(host.order)
  return (
    <Box component="span" sx={CHIPS_SX}>
      <span data-testid="host-state" data-state={open ? host.order?.stage : host.state} data-stalled={status.stalled || undefined}>
        <StatusChip
          label={status.label}
          tone={status.tone}
          glyph={status.glyph}
          title={open ? (onOpenOrder ? 'Adding a node. Open to follow it.' : 'Adding a node.') : status.reason || undefined}
          data-status={open ? host.order?.stage : host.state}
          onClick={open && onOpenOrder && host.order ? () => onOpenOrder(host.order!.id) : undefined}
          press={ORDER_PRESS}
        />
      </span>
      {order && host.order && (
        <span data-testid="host-order" data-stage={host.order.stage}>
          <StatusChip
            label={order.label}
            tone={order.tone}
            glyph={order.glyph}
            title={order.failed
              ? `Adding a node failed${host.order.updated_at ? ` ${relativeAge(host.order.updated_at, new Date(now ?? Date.now()))}` : ''}. Open it to retry.`
              : 'Adding a node. Open to follow it.'}
            onClick={onOpenOrder ? () => onOpenOrder(host.order!.id) : undefined}
            press={ORDER_PRESS}
          />
        </span>
      )}
    </Box>
  )
}

/** What the State chip cannot say in a word — why the host is in its state — and the machine's hardware. Nothing at
 *  all for a Ready host with no hardware reported: its State fact says it all. */
export function HostFactsLine({
  host,
  showReason,
  machine,
  now,
}: Omit<HostFactsProps, 'onOpenOrder'> & {
  /** Why the machine is in its state; ready says nothing more than its chip. */
  showReason: boolean
}): JSX.Element | null {
  const status = hostStatus(host, machine, now)
  // A stalled order's reason is the host page's warning banner, not a caption.
  const reason = (showReason || orderOpen(host)) && !status.stalled ? status.reason : ''
  if (!reason && !host.hardware) return null
  return (
    <Box data-testid="host-facts" sx={LINE_SX}>
      {reason && <Box component="span" data-testid="host-state-reason" sx={TEXT_SX}>{reason}</Box>}
      {/* Its own line: the machine is a different fact from who runs it, and must not read as one run of words. */}
      {host.hardware && <Box sx={{ flexBasis: '100%', minWidth: 0 }}><HardwareFacts hardware={host.hardware} /></Box>}
    </Box>
  )
}

/* ── THE MACHINE'S HARDWARE — one home for the host's page and the Shared tier dialog's "Runs on". ── */

export const MACHINE_LABEL: Record<NonNullable<HostHardware['machine']>, string> = {
  cloud: 'Cloud (EC2)', vm: 'Virtual machine', physical: 'Physical',
}

/** A `cloud` machine by the cloud it runs on; no cloud named is AWS (`clouds.provider_key_of`). */
export const CLOUD_MACHINE_LABEL: Record<CloudProvider, string> = {
  aws: 'Cloud (EC2)', gcp: 'Cloud (GCP)', azure: 'Cloud (Azure)',
}

/** The machine kind's words: a cloud machine named by its cloud; one the server could not name (null) is "Cloud". */
export function machineLabel(hw: Pick<HostHardware, 'machine' | 'cloud'>): string {
  if (hw.machine === 'cloud') return hw.cloud === null ? 'Cloud' : CLOUD_MACHINE_LABEL[hw.cloud ?? 'aws'] ?? 'Cloud'
  return hw.machine ? MACHINE_LABEL[hw.machine] ?? '' : ''
}

/** `32` → `32`, `15.25` → `15.3`. */
const gb = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(1))

/** "8 vCPU · 32 GB"; either half alone when the other is unknown; null when both are. */
export function hardwareSize(hw: Pick<HostHardware, 'vcpu' | 'memory_gib'>): string | null {
  const parts = [hw.vcpu != null ? `${hw.vcpu} vCPU` : null, hw.memory_gib != null ? `${gb(hw.memory_gib)} GB` : null]
  return parts.filter(Boolean).join(' · ') || null
}

/** The hypervisor each reported value names (`nodes.hypervisor`). */
export const HYPERVISOR_LABEL: Record<NonNullable<HostHardware['hypervisor']>, string> = {
  kvm: 'KVM', 'kvm-wsl2': 'KVM (WSL2)', vz: 'Virtualization.framework',
}

/** The hypervisor badge. A node that reports none usable says why in the server's own sentence
 *  (`hypervisor_blocker`); an unreported one is "Hypervisor unknown". On a VM, a hypervisor is nested
 *  virtualization; the tooltip says so. A usable report without a hypervisor is an older Linux node's `/dev/kvm`. */
export function kvmBadge(hw: Pick<HostHardware, 'kvm' | 'machine' | 'instance_type' | 'hypervisor' | 'hypervisor_blocker'>): { label: string; tone: Tone; title: string } {
  if (hw.kvm == null) return { label: 'Hypervisor unknown', tone: 'neutral', title: 'This node hasn\'t reported whether it can run MicroVMs' }
  // An EC2 instance is a VM unless it is a metal type (`m5.metal`, `c7i.metal-24xl`).
  const vm = hw.machine === 'vm' || (hw.machine === 'cloud' && !/\.metal(-|$)/.test(hw.instance_type ?? ''))
  if (hw.kvm) {
    return { label: HYPERVISOR_LABEL[hw.hypervisor ?? 'kvm'], tone: 'success', title: vm ? 'Nested virtualization: runs VM-isolated workloads' : 'Runs VM-isolated workloads' }
  }
  return { label: 'No hypervisor', tone: 'neutral', title: hw.hypervisor_blocker ?? (vm ? 'No nested virtualization: no VM-isolated workloads' : 'No VM-isolated workloads') }
}

/** The known facts in reading order — machine, instance type, size, disk, architecture — unknown ones left out. */
export function hardwareFacts(hw: HostHardware, opts: { machine?: boolean } = {}): { key: string; text: string; title?: string }[] {
  const size = hardwareSize(hw)
  return [
    opts.machine !== false && hw.machine
      ? { key: 'machine', text: machineLabel(hw), title: hw.virtualization ?? undefined }
      : null,
    hw.instance_type ? { key: 'type', text: hw.instance_type } : null,
    size ? { key: 'size', text: size } : null,
    hw.disk_gb != null ? { key: 'disk', text: `${hw.disk_gb} GB disk` } : null,
    hw.arch ? { key: 'arch', text: hw.arch } : null,
  ].filter((f): f is { key: string; text: string; title?: string } => Boolean(f?.text))
}

/** The facts as one " · " run, then the hypervisor badge; nothing at all when every fact is unknown. */
export function HardwareFacts({ hardware, machine = true, testId = 'host-hardware' }: {
  hardware: HostHardware | null | undefined
  /** Lead with the machine kind (the host's page); a list of pool hosts leaves it out. */
  machine?: boolean
  testId?: string
}): JSX.Element | null {
  if (!hardware) return null
  const facts = hardwareFacts(hardware, { machine })
  const kvm = kvmBadge(hardware)
  if (!facts.length && hardware.kvm == null) return null
  return (
    <Box component="span" data-testid={testId} sx={{ display: 'inline-flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1, rowGap: 0.5, minWidth: 0 }}>
      {facts.length > 0 && (
        <Box component="span" sx={TEXT_SX}>
          {facts.map((f, i) => (
            <span key={f.key} data-fact={f.key} title={f.title}>{i > 0 && ' · '}{f.text}</span>
          ))}
        </Box>
      )}
      <StatusChip label={kvm.label} tone={kvm.tone} title={kvm.title} data-status={hardware.kvm == null ? 'unknown' : hardware.kvm ? 'kvm' : 'no-kvm'} />
    </Box>
  )
}
