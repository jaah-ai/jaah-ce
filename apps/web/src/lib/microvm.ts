/* MicroVM — the console's shapes for the API's MicroVM fields and the
 * words they render as. The resolver, the bounds and every refusal sentence are the server's
 * (`app/microvm_sizing.py`, `app/microvm_mode.py`, `app/microvm_egress.py`); nothing here re-derives one. */

import type { Shape } from './nodeShape'

/** The three sizes, in the server's display order (`microvm_sizing.FIELDS`). */
export const SIZING_FIELDS = ['vcpus', 'ram_mib', 'disk_gib'] as const
export type SizingField = (typeof SIZING_FIELDS)[number]

/** Which level a size came from: node > project > org > system (`microvm_sizing.effective`). */
export type SizingSource = 'node' | 'project' | 'org' | 'system'
/** A level that has its own override. */
export type SizingLevel = Exclude<SizingSource, 'system'>

/** `microvm_sizing.sizing_out`: each size with its source, and the edited level's own values (null = inherits). */
export type MicrovmSizing = Record<SizingField, { value: number; source: SizingSource }> & {
  override: Record<SizingField, number | null>
}

/** The tier column a size is written to (`microvm_sizing.column`). */
export const sizingColumn = (field: SizingField) => `microvm_${field}` as const
export type SizingBody = Partial<Record<ReturnType<typeof sizingColumn>, number | null>>

/** Each size's label and its input's unit. */
export const SIZING_LABELS: Record<SizingField, { label: string; unit: string }> = {
  vcpus: { label: 'vCPUs', unit: 'vCPUs' },
  ram_mib: { label: 'RAM', unit: 'MiB' },
  disk_gib: { label: 'Disk', unit: 'GiB' },
}

/** RAM in GiB, to two places: 1536 MiB is 1.5. */
export const gibOf = (mib: number) => Number((mib / 1024).toFixed(2))

/** One size as words: `4 vCPU` (the unit, as the PRD and the catalogue's column spell it), `8 GiB` (whole GiB) or
 *  `1536 MiB` — or, with `ramGib`, always GiB (`1.5 GiB`) — `64 GiB`. */
export function sizeText(field: SizingField, value: number, ramGib = false): string {
  if (field === 'vcpus') return `${value} vCPU`
  if (field === 'ram_mib') return ramGib || value % 1024 === 0 ? `${gibOf(value)} GiB` : `${value} MiB`
  return `${value} GiB`
}

/** Where a size came from, as the PRD words it: `from project`, or `default` for a Whole-node VM's unset size. The
 *  system tier is Operator Settings. */
export function sourceText(source: SizingSource | NodeVmSizeSource): string {
  return source === 'default' ? 'default' : `from ${source}`
}

/** `4 vCPU — from project`: the value AND the level in force, always together (PRD quality bar). */
export function sizedText(field: SizingField, sized: { value: number; source: SizingSource | NodeVmSizeSource },
  ramGib = false): string {
  return `${sizeText(field, sized.value, ramGib)} — ${sourceText(sized.source)}`
}

/** A Whole-node VM's own size (`GET /api/nodes/{id}` `node_vm_size`): the node's value, else the default. */
export type NodeVmSizeSource = 'node' | 'default'
export type NodeVmSize = Record<SizingField, { value: number; source: NodeVmSizeSource }>
/** `PUT /api/nodes/{id}`: only the changed sizes; null resets one to the default. */
export type NodeVmSizeBody = { node_vm_size: Partial<Record<SizingField, number | null>> }

/** A project's MicroVM fields (`routes/projects.py::ProjectOut`, architecture §4.9): its egress edits, the default
 *  they edit (read-only), the list its VMs get, and its size. Optional on `Project` for cached payloads. */
export type ProjectEgress = {
  egress_allow: string[]
  egress_deny: string[]
  egress_default: string[]
  egress_effective: string[]
}
export type ProjectMicrovm = Partial<ProjectEgress> & { microvm_sizing?: MicrovmSizing | null }

/** An org's MicroVM fields (`routes/orgs.py::OrgOut`): whether it may mark nodes, and who decided (its plan, or
 *  the operator's override on the org's Overrides tab), and its size. */
export type OrgMicrovm = {
  microvm_allowed?: { value: boolean; source: 'plan' | 'override' } | null
  microvm_sizing?: MicrovmSizing | null
}

/** One `microvms` row as the node page lists it (`routes/nodes/node_wire.py::NodeMicrovmOut`). */
export type NodeMicrovm = {
  vm_id: string
  kind: string
  task_id: number | null
  console_session_id: string | null
  state: string
  wait_reason: string | null
  /** The newest order's reason (`microvm_intents.reason`): why an ended row ended. */
  reason?: string | null
  created_at: string | null
}

/** The node's own MicroVM report (`config_snapshot['microvm']`, architecture §4.3), served as sent. Every key is
 *  optional here: it is the node's opaque report, so a key it did not send renders as "not reported". */
export type MicrovmFacts = {
  kvm_usable?: boolean
  kvm_error?: string | null
  free_ram_mib?: number
  free_disk_mib?: number
  taps_free?: number
  base?: { pin_id?: string | null; state?: string | null; error?: string | null } | null
  layers?: MicrovmLayer[]
  vms?: { vm_id?: string; state?: string; tap?: string | null; generation?: number | null }[]
  vm_dirs?: string[]
  ch_pids?: number
  virtiofsd_pids?: number
  taps_busy?: number
  egress_refusals?: (EgressRefusal | string)[]
}

/** One project layer (`microvm_images.layers_fact`). A failed one carries its error sentence and the last lines of
 *  its build log (`log_tail`, bounded node-side), so the page shows why without a shell. */
export type MicrovmLayer = {
  project_id?: number | string | null
  key?: string | null
  state?: string | null
  log?: string | null
  error?: string | null
  log_tail?: string | null
}

/** One refused guest connection from a VM's egress log (C8). Its shape is not pinned by §4.3, so each field
 *  reads the spellings a log line plausibly carries. */
export type EgressRefusal = {
  vm_id?: string
  vm?: string
  name?: string
  host?: string
  reason?: string
  at?: string
  time?: string
  ts?: string
}

/** Where the node's report sits in its config snapshot (`app/node_config.py::MICROVM_SNAPSHOT_KEY`): the
 *  MicroVM tab renders it, so the node page's daemon config leaves it out. */
export const MICROVM_SNAPSHOT_KEY = 'microvm'

/** The node half of `GET /api/nodes/{id}` (architecture §4.9). Optional for cached payloads and fixtures. */
export type NodeMicrovmDetail = {
  /** The node's shape, and the shapes it may change to, its own included (`PUT {shape}`). */
  shape?: Shape
  shape_options?: Shape[]
  microvm?: boolean
  microvm_blocker?: string | null
  microvm_editable?: boolean
  microvm_sizing?: MicrovmSizing | null
  /** A Whole-node VM's size; null on any other shape. */
  node_vm_size?: NodeVmSize | null
  microvm_facts?: MicrovmFacts | null
  microvms?: NodeMicrovm[]
  /** Every VM row of the node counted by state, beyond the `microvms` slice (`NODE_MICROVMS_SHOWN`). */
  microvm_counts?: Record<string, number>
  /** The ids of every `retained` VM row, beyond the slice. */
  microvm_retained_ids?: string[]
}
