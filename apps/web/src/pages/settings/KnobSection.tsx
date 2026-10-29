import { useEffect, useMemo, useRef, useState, type ComponentType, type ReactNode } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import Button from '@mui/material/Button'
import TextField from '@mui/material/TextField'
import InputAdornment from '@mui/material/InputAdornment'
import Alert from '@mui/material/Alert'
import Stack from '@mui/material/Stack'
import { alpha, useTheme } from '@mui/material/styles'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, apiDetail, get, put } from '../../api'
import { useAuth } from '../../auth'
import { useApiRead } from '../../hooks/useApiRead'
import { useSubmit } from '../../hooks/useSubmit'
import { useConfirm } from '../../components/ConfirmDialog'
import { useLeaveGuard } from '../../hooks/useLeaveGuard'
import Banner from '../../ui/Banner'
import UnsavedChangesDialog from '../../ui/UnsavedChangesDialog'
import CredentialSelect, { type CredentialOption } from '../../ui/CredentialSelect'
import { credentialMark } from '../../ui/credentialMarks'
import HelpLabel from '../../ui/HelpLabel'
import SectionCard from '../../ui/SectionCard'
import SettingCheckbox from '../../ui/SettingCheckbox'
import { PHONE_QUERY } from '../../ui/listStyles'
import { VISUALLY_HIDDEN } from '../../ui/visuallyHidden'
import { getCredential, loadCredentialOptions, type Credential } from '../credentials/api'
import { knobNumber } from './knobNumber'
import {
  CLOUD_CONNECTIONS_PATH,
  CLOUD_CONNECTIONS_URL,
  connectionKnobLabel,
  listCloudConnectionOptions,
} from './cloudConnections/api'
import { RETENTION_QUERY_ROOT } from './retention/api'
import type { CloudConnectionOption, OperatorSetting, SettingsPayload, VnodeHost } from '../../types'
import { humanize } from '../../lib/humanize'
import { UNFILTERED_READ } from '../../lib/orgScope'
import { isEnabled } from '../../lib/editionFeatures'

/* Support › Jarvis tools as a read/write grid (`./jarvisTools/`): the Help assistant's knobs, so loaded only with it
 * (`support_assistant`) — the community edition's tree leaves the directory out, and `import.meta.glob` yields `{}`
 * for an absent file. Without it no section is a grid. */
type JarvisGrid = { readonly rows: unknown[] }
type JarvisGridView = ComponentType<{
  grid: JarvisGrid
  value: (s: OperatorSetting) => boolean
  onChange: (values: Record<string, boolean>) => void
  editable: boolean
  marked?: (s: OperatorSetting) => boolean
  lit?: string | null
  idPrefix: string
}>
const jarvisOn = isEnabled('support_assistant')
const PermissionGrid: JarvisGridView | undefined = Object.values(jarvisOn
  ? import.meta.glob<JarvisGridView>('./jarvisTools/PermissionGrid.tsx', { eager: true, import: 'default' })
  : {})[0]
const permissionGrid: (entries: OperatorSetting[]) => JarvisGrid | null = Object.values(jarvisOn
  ? import.meta.glob<(entries: OperatorSetting[]) => JarvisGrid | null>(
    './jarvisTools/permissionGrid.ts', { eager: true, import: 'permissionGrid' })
  : {})[0] ?? ((): null => null)

/* KNOB SECTIONS — the ONE home for rendering and saving operator knobs (`GET`/`PUT /api/settings`), shared
 * by Settings (`pages/Settings.tsx`: categories, search, panels) and Products (`pages/Products.tsx`: the
 * knobs that define what is sold). A page holds ONE editor (`useKnobEditor`: the payload, the pending edits,
 * the save) and renders any slice of its knobs as cards (`KnobSection`) under one Save bar (`KnobSaveBar`).
 * The server is unchanged by where a knob renders: key, validation, tier and the operator-only write are
 * `app/operator_settings/`'s and `app/routes/settings.py`'s.
 * GEOMETRY: a row is ONE line, LEFT-PACKED — label column, then the control right beside it,
 * then its provenance ("Default · on" / "Reset to on"). The row it replaces put the label at the left edge
 * and the control at the right, ~90px tall: on a wide screen the two halves of one setting sat a screen
 * apart (a Gestalt-proximity break: NN/g "Group Form Elements Effectively Using White Space"). */

// A pending edit: either a new value, or an explicit "clear the override and fall back".
export type Edit = { clear: true } | { clear: false; value: string | boolean }

/* Stable empties for the best-effort option lists (a fresh `[]` per render is a new identity). */
const NO_HOSTS: VnodeHost[] = []
const NO_CONNECTIONS: CloudConnectionOption[] = []

/* A credential knob's row. The picker asks once PER DECLARED TYPE and merges (`loadCredentialOptions`):
 * `GET /api/credentials`'s `type` filter is single-valued, and a client-side filter would read one page.
 * The hint: `credentials.name` is not unique (hence knobs store an ID), so two same-named rows must not
 * render identically. Multi-type knob → the TYPE (the wire field is a list, so the branch is the contract).
 * Single-type knob → the DESCRIPTION (the operator's own sentence), falling back to the type when blank.
 * Never the id: `uuidv7()` is time-ordered, so a prefix carries no entropy (`pages/credentials/Credentials.tsx`). */
function credentialOption(row: Credential, types: string[]): CredentialOption {
  const hint = types.length > 1 ? row.type_label || row.type : row.description?.trim() || row.type_label || row.type
  return { id: row.id, name: row.name, hint, icon: credentialMark(row.type, row.type_label) }
}

// A choice token's display words: the catalogue's paired label when it declares one, else humanised.
/** A saved credential id's "Loading…" stand-in until its name resolves, one stable object per id (`CredentialSelect`
 *  watches `selected` by identity). */
const loadingNames = new Map<string, CredentialOption>()
function loadingName(id: string): CredentialOption {
  let option = loadingNames.get(id)
  if (!option) {
    option = { id, name: 'Loading…' }
    loadingNames.set(id, option)
  }
  return option
}

function choiceLabel(s: OperatorSetting, token: string): string {
  const i = s.choices?.indexOf(token) ?? -1
  return (i >= 0 ? s.choice_labels?.[i] : undefined) ?? humanize(token)
}

// The warning a choice carries while it is the value shown ('' or none: nothing to say).
function choiceNote(s: OperatorSetting, value: string | boolean): string {
  const i = typeof value === 'string' ? (s.choices?.indexOf(value) ?? -1) : -1
  return (i >= 0 ? s.choice_notes?.[i] : undefined) ?? ''
}

/** A word unit drops its plural s at exactly one ("1 port", "1 day"); a symbol (`s`, `%`, `GB`, `USD`) stays. */
function unitFor(unit: string, value: string): string {
  return Number(value) === 1 && /^[a-z][A-Za-z]+s$/.test(unit) ? unit.slice(0, -1) : unit
}

/** An empty fallback, said: "Default · not set", and the override of one clears ("Clear", not "Reset to …"). */
const NOT_SET = 'not set'

function fallbackLabel(s: OperatorSetting): string {
  if (s.kind === 'bool') return s.fallback ? 'on' : 'off'
  const text = String(s.fallback ?? '')
  // A choice knob's fallback is a raw token, so word it like the options — `''` too, when it is one of them.
  if (s.choices?.includes(text)) return choiceLabel(s, text)
  if (!text) return NOT_SET
  if (s.choices?.length) return choiceLabel(s, text)
  const words = knobNumber(s, text)
  return s.unit ? `${words} ${unitFor(s.unit, text)}` : words
}

/** Whether a knob's value was chosen by the OPERATOR rather than left at the catalogue default — the
 *  non-operator's third provenance state beside "Set for your organization" (`overridden`) and "Default".
 *  Asked only of rows the org has NOT overridden. An inference, sound one way only: an operator pin equal
 *  to the default reads "Default" (the wire lacks the operator's row); serving the operator tier's
 *  `overridden` bit would fix it if it ever matters. */
function setByOperator(s: OperatorSetting): boolean {
  return !s.overridden && s.value !== s.default
}

/** The value as words, for a viewer who may only read: a definition list, not a form of greyed boxes
 *  (disabled MUI text falls below contrast, drops out of the tab order, and still looks like a form; a
 *  native `<select>`, `ui/CredentialSelect` and the checkbox have no read-only mode anyway).
 *  Rendered in the control's slot: `choiceLabel` for choices, the resolved credential, host or connection
 *  NAME for a pointer, else the stored string; the unit rides beside it. A connection list or credential
 *  name still loading reads "Loading…", never "Unknown connection" for one that exists, nor the raw id. */
function valueAsWords(
  s: OperatorSetting,
  value: string | boolean,
  resolvedNames: Record<string, CredentialOption>,
  hosts: VnodeHost[],
  connections: CloudConnectionOption[],
  connectionsLoading: boolean,
): string {
  if (s.kind === 'bool') return value ? 'on' : 'off'
  const text = String(value ?? '')
  if (s.credential_type?.length) {
    // `''` is the documented "unconfigured" state; said in words, not an empty cell.
    if (!text) return 'Not configured'
    return resolvedNames[text]?.name ?? 'Loading…'
  }
  if (s.vnode_host) {
    if (!text) return 'Not configured'
    // Never the raw id: a host outside the list (deleted, or the list refused) is said as such.
    return hosts.find(h => h.host_node_id === text)?.name ?? 'Unknown host'
  }
  if (s.cloud_connection) {
    if (!text) return 'Not configured'
    const found = connections.find(c => c.id === text)
    if (found) return connectionKnobLabel(found)
    return connectionsLoading ? 'Loading…' : 'Unknown connection'
  }
  if (s.choices?.length) return choiceLabel(s, text)
  if (!text) return NOT_SET
  const words = knobNumber(s, text)
  return s.unit ? `${words} ${unitFor(s.unit, text)}` : words
}

/** The SAVED value as words, for a refused row ("Still saved: …"); an empty one is "none". */
function savedAsWords(
  s: OperatorSetting,
  resolvedNames: Record<string, CredentialOption>,
  hosts: VnodeHost[],
  connections: CloudConnectionOption[],
  connectionsLoading: boolean,
): string {
  const words = valueAsWords(s, displayed(s, undefined), resolvedNames, hosts, connections, connectionsLoading)
  return words === 'Not configured' || words === NOT_SET ? 'none' : words
}

// A pending edit if any, else the server's value. A pending clear displays the fallback, so Reset previews.
function displayed(s: OperatorSetting, edit: Edit | undefined): string | boolean {
  const raw = edit ? (edit.clear ? s.fallback : edit.value) : s.value
  if (s.kind === 'bool') return Boolean(raw)
  return typeof raw === 'string' ? raw : String(raw ?? '')
}

/** Is `value` what the knob falls back to? Then an override of it changes nothing, and offers no Reset. */
function atFallback(s: OperatorSetting, value: OperatorSetting['value']): boolean {
  if (s.kind === 'bool') return Boolean(value) === Boolean(s.fallback)
  return String(value) === String(s.fallback ?? '')
}

/* A number with a unit needs its digits AND the unit in view ("16384 connections" once clipped to "16384 con"
 * at 390px in the 8rem track): the row widens its control track to what the pair needs, in `ch` (digits and letters
 * alike), plus the field's padding and the adornment's gap. Sized for the widest value the knob takes (its maximum's
 * digits, else the longest of the value shown, saved and fallen back to), so typing never reflows the row; never
 * past the track's 16rem maximum. */
function controlMin(s: OperatorSetting, value: string | boolean): string | undefined {
  if (s.kind === 'str' || s.kind === 'bool' || !s.unit) return undefined
  const digits = Math.max(...[value, s.value, s.fallback, s.maximum].map(v => (v === null || v === undefined ? 0 : String(v).length)))
  const chars = digits + s.unit.length
  return `min(16rem, calc(${chars}ch + 2.75rem))`
}

export const touchTargetSx = {
  minHeight: { xs: '44px', sm: 32 },
  minWidth: { xs: '44px', sm: 'auto' },
  // MUI's default Button focus is too faint for AC11; an explicit ring.
  '&:focus-visible': {
    outline: '2px solid',
    outlineColor: 'primary.main',
    outlineOffset: '2px',
  },
}

/* THE ONE CONTROL WIDTH. Every editable control — text, number, select, credential picker — fills its
 * row's control TRACK (`ROW_GRID` below sizes it), so a page of rows reads as one aligned form rather
 * than fields of five widths; the unit rides INSIDE the field as an end adornment, never beside it. */
const CONTROL_WIDTH = '100%'

/* THE ROW GRID (desktop). Three columns, left-packed: the label (`LABEL_TRACK` — a label wraps rather than
 * pushing the control), the control (`CONTROL_TRACK`, a TRACK WIDTH and never `auto`: each row is its own grid, so a
 * content-sized column would put a checkbox row's provenance 200px left of a field row's — one shared
 * width is what lines the third column up down the card), and the provenance, which takes what is left
 * and wraps (a long `str` default such as an instance-type list).
 * At a card's narrowest three-column width the LABEL wins: its minimum, 12.5rem, holds "Comment on
 * linked issue" (191px) and every shorter label on one line, while the control's, 8rem, still shows a three-digit
 * value beside its unit ("30 minutes") — at 9rem against 8rem the control took width its value never used while
 * "Manifest transcript wait" wrapped. Wider cards grow both tracks to their maxima. The provenance's minimum fits
 * "Default · 3 attempts" and every shorter fallback on one line at the cell's 0.75rem (at 7rem and
 * body2's 14px most rows wrapped it in two). `ROW_MIN_HEIGHT` is the checkbox's own tap floor
 * (`theme.custom.tapTargetPx`), so a bool row and a text-field row come out the same height and the card
 * reads as an even list. A card too narrow for the three (a tablet window beside the expanded sidebar) keeps the label
 * and control side by side, the provenance under the LABEL in the room the taller control leaves there, so an
 * overridden row's "Reset to …" does not add a line; on a phone-width card the three stack.
 * A card that draws NO control (every row read-only) has no control width to line up on: there the card is the
 * grid (`READ_ONLY_CARD_GRID`) and each row a subgrid of it, so the value track is the card's widest value
 * (capped at the control track's maximum, past which a value wraps) and the provenance sits right beside it —
 * the fixed control track left "Default" a third of the card away from a two-word value. */
const LABEL_TRACK = 'minmax(12.5rem, 14rem)'
/* The two-column label: two columns are chosen down to a ~290px card, where 12.5rem would leave the control no
 * room, so the label's minimum is 12.5rem or all but the control's 8rem and the 1rem gap, whichever is less. */
const MID_LABEL_TRACK = 'minmax(min(12.5rem, 100% - var(--knob-control-min, 8rem) - 1rem), 14rem)'
/* `--knob-control-min` is a row's own floor when its value needs more than 8rem (`controlMin`). */
const CONTROL_TRACK = 'minmax(max(8rem, var(--knob-control-min, 8rem)), 16rem)'
const ROW_MIN_HEIGHT = 44
/* The three columns follow the CARD's width, not the window's: beside the expanded sidebar a
 * 600–760px window leaves a ~230–390px card, and the window-gated grid clipped the unit and the provenance
 * off its right edge. `ROWS_WIDE` is the tracks' minimums (29rem) plus two 1rem gaps and the card's 20px of
 * border and padding (516px) plus 20px, so three columns rarely sit at their floors: 536px. Beside the
 * expanded sidebar the card measures ~440px at the 1024px floor and keeps two columns. `ROWS_MID` is the
 * two-column minimums plus one gap and the padding; below it the row stacks as on a phone. The section root is
 * the query container; a control's touch sizing stays window-gated. */
const KNOB_SECTION_CONTAINER = 'knob-section'
const ROWS_MID = `@container ${KNOB_SECTION_CONTAINER} (min-width:292px)`
const ROWS_WIDE = `@container ${KNOB_SECTION_CONTAINER} (min-width:536px)`
/* A select row keeps the phone shape on a phone-width card (control the row's full width) and goes two-column only
 * where its control gets ~10rem beside the label's 12.5rem: the 8rem floor clipped a choice's words. */
const ROWS_MID_CHOICE = `@container ${KNOB_SECTION_CONTAINER} (min-width:400px)`
const MID_ROW_GRID = `${MID_LABEL_TRACK} ${CONTROL_TRACK}`
const ROW_GRID = `${LABEL_TRACK} ${CONTROL_TRACK} minmax(8.5rem, 1fr)`
const READ_ONLY_CARD_GRID = {
  [ROWS_WIDE]: { display: 'grid', gridTemplateColumns: `${LABEL_TRACK} fit-content(16rem) minmax(7rem, 1fr)` },
}
const READ_ONLY_ROW_GRID = 'subgrid'

export const controlSx = {
  width: CONTROL_WIDTH,
  /* The theme gives every TextField `margin: 'dense'` (8px above, 4px below — a stacked-form rhythm); in a
   * one-line row that margin is what made a field row taller than a checkbox row. The ROW spaces itself
   * (and the search box, which shares this sx, is spaced by the page's own gap). */
  my: 0,
  '& .MuiInputBase-root': { minHeight: { xs: '44px', sm: 'auto' } },
  /* The tap target is the inner input (MUI's `1.4375em` content box), so the floor is PADDING, not
   * `minHeight` (a border-box height truncates the value). FilterRow's recipe. */
  '& .MuiInputBase-input': { pt: { xs: '10.5px', sm: '8.5px' }, pb: { xs: '10.5px', sm: '8.5px' } },
}

/** Shared with panels that write a knob outside the editor (`components/PortGridSection`'s block fix). */
export const SETTINGS_QUERY_KEY = ['settings']

/** May this viewer write knob `s`? The served verdicts (see `useKnobEditor`'s WHO MAY SAVE); false before
 *  the payload lands. */
export function knobWritable(payload: SettingsPayload | undefined, s: OperatorSetting | undefined): boolean {
  if (!payload || !s) return false
  return payload.editable === true || (payload.org_editable === true && s.tenant_editable === true)
}

const fetchSettings = () => get<SettingsPayload>('/api/settings')

/** The one knob a refused save is about, of the keys it `sent`, or null when it names none or several: the
 *  server's own `detail.key` (the Telegram bot), else — for a 409/422 only, never a network failure or a 5xx — the
 *  only key sent, or the one sent knob whose key or label the sentence names (`operator_settings/`'s validation
 *  errors open with one or the other). */
function refusedKnob(err: unknown, sent: string[], settings: OperatorSetting[]): string | null {
  if (!(err instanceof ApiError) || (err.status !== 409 && err.status !== 422)) return null
  const named = (err.detail as { key?: unknown } | null)?.key
  if (typeof named === 'string' && sent.includes(named)) return named
  if (sent.length === 1) return sent[0]
  const text = apiDetail(err, '')
  const hits = sent.filter(key => {
    const label = settings.find(s => s.key === key)?.label
    return text.includes(key) || (label !== undefined && text.startsWith(label))
  })
  return hits.length === 1 ? hits[0] : null
}

/** Is a pending edit the value already saved (nothing left to save)? */
function editSaved(s: OperatorSetting, e: Edit): boolean {
  if (e.clear) return !s.overridden
  return s.kind === 'bool' ? Boolean(e.value) === Boolean(s.value) : String(e.value) === String(s.value ?? '')
}

/* ── THE EDITOR ────────────────────────────────────────────────────────────────────────────────────── */
export type KnobEditor = ReturnType<typeof useKnobEditor>

/** One page's knob payload, pending edits and save. `enabled: false` skips the read (a viewer the GET would
 *  refuse: `GET /api/settings` asks `system:view`). */
export function useKnobEditor({ enabled = true }: { enabled?: boolean } = {}) {
  const queryClient = useQueryClient()
  const settingsQuery = useQuery({
    queryKey: SETTINGS_QUERY_KEY,
    refetchOnWindowFocus: false,
    queryFn: fetchSettings,
    enabled,
  })
  const settings = settingsQuery.data?.settings ?? null
  const loadError = settingsQuery.isError ? 'Failed to load settings' : ''
  // `?? false`: an older server's payload renders read-only (`types.ts`). Both `false` until the payload lands.
  /** The server's "may this viewer save" verdict (see `canSave`). */
  const editable = settingsQuery.data?.editable ?? false
  /** The server's "a tenant Org Admin may save its `tenant_editable` knobs" verdict. */
  const orgEditable = settingsQuery.data?.org_editable ?? false
  const [edits, setEdits] = useState<Record<string, Edit>>({})
  const { busy: saving, formError: saveError, setFormError: setSaveError, run, saved, clearSaved } = useSubmit()
  const [confirm, confirmDialog] = useConfirm()
  /** The Telegram bot whose token Telegram refused on the last save: the save error leads to its credential. */
  const [refusedBot, setRefusedBot] = useState('')
  /** The keys the last REFUSED save carried: a refusal names at most one knob, so the rest are marked on their rows. */
  const [failedKeys, setFailedKeys] = useState<string[]>([])
  /** The one knob the last refused save was about (`refusedKnob`), and how many refusals so far: a page re-finds the
   *  row on each one, the same knob refused twice included. */
  const [refused, setRefused] = useState<{ key: string | null; seq: number }>({ key: null, seq: 0 })
  const refuse = (key: string | null) => setRefused(prev => ({ key, seq: key ? prev.seq + 1 : prev.seq }))
  /* Configured credential ids → names, so a configured knob reads its name without first being focused
   * (`ui/CredentialSelect` only learns names from `fetchOptions`). As `ProjectEditPage` does for Jira. */
  const [resolved, setResolved] = useState<Record<string, CredentialOption>>({})
  /** Ids the resolve effect has already asked about (a ref; see there). */
  const asked = useRef<Set<string>>(new Set())
  /** Successful saves per key, so a panel following a knob re-reads after a save that left its value unchanged. */
  const [saveCounts, setSaveCounts] = useState<Record<string, number>>({})
  /** How many knobs the last landed save wrote: Enter saves every pending edit, so the notice says how many. */
  const [savedCount, setSavedCount] = useState(0)

  const { admin } = useAuth()
  const isOperator = admin?.is_operator === true

  const visibleKnobs = useMemo(
    // A `data_regions` knob is its own panel's (`settings/dataRegions`), never a raw-text row.
    () => (settings ?? []).filter(s => (isOperator || s.tier === 'org') && s.data_regions !== true),
    [settings, isOperator],
  )

  /* Resolve saved ids, one `GET /api/credentials/{id}` each (a paginated, type-filtered roster may not
   * contain the row), past the operator's org filter (`UNFILTERED_READ`): an operator knob names an org-zero
   * row, which a filter on tenants would 404. A 404 then says why — an operator reads every org, so the row is
   * gone; for anyone else a grant-scoped 404 is "deleted" or "not shared" alike. Any other failure reads
   * "Unknown credential". Never the id, as an unknown host or connection. Accumulated by id, never reset
   * (a stable `selected` reference). Over VISIBLE knobs only: never fetch a name the page will not show. */
  useEffect(() => {
    if (settings === null) return
    let alive = true
    const wanted = visibleKnobs
      .filter(s => s.credential_type?.length && s.kind === 'str' && typeof s.value === 'string' && s.value !== '')
      .map(s => s.value as string)
    for (const id of wanted) {
      /* Asked once per id, tracked in a ref: guarding on `resolved` would put it in the deps and each answer would
       * re-fire in-flight siblings; and `save()` gives `visibleKnobs` a new identity. Bookkeeping, not render
       * state. */
      if (asked.current.has(id)) continue
      asked.current.add(id)
      void (async () => {
        try {
          const row = await getCredential(id, UNFILTERED_READ)
          if (alive) setResolved(prev => (prev[id] ? prev : { ...prev, [id]: { id, name: row.name } }))
        } catch (err) {
          const name = !(err instanceof ApiError && err.status === 404)
            ? 'Unknown credential'
            : isOperator ? 'Deleted credential' : 'Deleted or not shared'
          if (alive) setResolved(prev => (prev[id] ? prev : { ...prev, [id]: { id, name } }))
        }
      })()
    }
    return () => {
      alive = false
    }
  }, [settings, visibleKnobs, isOperator])

  /* The host list, once, only when a visible knob points at a host AND the viewer may edit (an operator):
   * `GET /api/vnode-hosts` also serves tenant hosts, so `org_only` ones are dropped — the knob names an
   * OPERATOR host. A failure is silent: the select keeps its current value and "Not set". */
  // Each list exists only with its feature (`lib/editionFeatures.ts`): no host, no cloud connection in the community edition.
  const wantsHosts = isEnabled('multi_node_hosts') && editable && isOperator && visibleKnobs.some(s => s.vnode_host)
  const hosts = useApiRead(
    ['knob-vnode-hosts'],
    () => get<VnodeHost[]>('/api/vnode-hosts').then(rows => rows.filter(h => h.org_only !== true)),
    '',
    { enabled: wantsHosts },
  ).data ?? NO_HOSTS

  /* The connection list, once, on the same terms as the hosts: `GET /api/cloud-connections/options` serves
   * the caller's reach — EVERY org's for an operator — and the knob names an OPERATOR-OWN connection
   * (`hosting_provisioning.py` refuses a tenant's), so the list is cut to the viewer's own org. A failure
   * is silent for the same reason. While it loads, a stored connection reads "Loading…": "Unknown connection"
   * is for one the settled list lacks. */
  const wantsConnections = isEnabled('cloud_nodes') && editable && isOperator && visibleKnobs.some(s => s.cloud_connection)
  const ownOrgId = admin?.org?.id
  const connectionsResource = useApiRead(
    ['knob-cloud-connections', ownOrgId],
    () => listCloudConnectionOptions().then(options => options.connections.filter(c => c.org_id === ownOrgId)),
    '',
    { enabled: wantsConnections },
  )
  const connections = connectionsResource.data ?? NO_CONNECTIONS
  // `loading` stays true while the read is held, so it counts only when the list is wanted.
  const connectionsLoading = wantsConnections && connectionsResource.loading

  /* ── WHO MAY SAVE ≠ WHO MAY READ ────────────────────────────────────────────────────────────────
   * `PUT /api/settings` pairs `@feature("system", "edit")` with an in-handler `is_operator` gate
   * (`routes/settings.py`: the flag AND the operator-plane org), refusing every non-operator; `GET` asks
   * `system:view` and answers PER ORG. The page is offered on the read and renders against the write.
   * Read off the SERVED verdict (`SettingsOut.editable`), never the flag and never a `require_superadmin`
   * dependency: `is_superadmin` has no org, so a tenant-org superadmin holds it while the PUT refuses them.
   * The browser must not derive the verdict: the plane's org uuids are single-homed in
   * `apps/api/app/tenancy/ids.py` (precedent: `routes/orgs.py::OrgOut.is_operator_plane`).
   * FAILS CLOSED: `false` before load, on failure, and on an older payload.
   * Gated panels read the role ("what is OFFERED"); this is "what may be WRITTEN".
   * Never a `useCan` key: superadmin is not in the catalogue (`apps/api/app/authz/`), and
   * `can('system:edit')` would offer a Save the server refuses. Presentation only
   * (`components/RequireFeature.tsx`).
   * A tenant Org Admin (`SettingsOut.org_editable`) writes only the knobs the server marks `tenant_editable`,
   * for its own org: those rows get controls, every other row stays words. */
  function rowEditable(s: OperatorSetting): boolean {
    return knobWritable(settingsQuery.data, s)
  }
  const canSave = editable || (orgEditable && visibleKnobs.some(s => s.tenant_editable === true))

  const dirtyKeys = Object.keys(edits)

  // The Save bar and count are PAGE-WIDE, so a dirty row can sit on an unseen category: the count rides
  // beside the category's name in the list and beside the section's title on its card.
  function unsavedIn(entries: OperatorSetting[]): number {
    return entries.filter(s => edits[s.key] !== undefined).length
  }

  /** A re-edited knob holds a value no save has refused yet: its mark goes. */
  function unmark(keys: string[]) {
    setFailedKeys(prev => prev.filter(key => !keys.includes(key)))
    if (refused.key && keys.includes(refused.key)) refuse(null)
  }

  function edit(key: string, next: Edit) {
    clearSaved()
    unmark([key])
    setEdits(prev => ({ ...prev, [key]: next }))
  }

  /* A grid click sets one or two switches (Write implies Read, `settings/jarvisTools`). A switch put back to
   * its stored value is no longer pending, so the coupling never leaves a phantom edit behind. */
  function editSwitches(values: Record<string, boolean>) {
    clearSaved()
    unmark(Object.keys(values))
    setEdits(prev => {
      const next = { ...prev }
      for (const [key, value] of Object.entries(values)) {
        const stored = settings?.find(s => s.key === key)?.value
        if (value === Boolean(stored)) delete next[key]
        else next[key] = { clear: false, value }
      }
      return next
    })
  }

  function discard() {
    setEdits({})
    setSaveError('')
    setRefusedBot('')
    setFailedKeys([])
    refuse(null)
    clearSaved()
  }

  /** True when the write landed (the leave gate leaves only then). */
  async function save(): Promise<boolean> {
    if (!settings || dirtyKeys.length === 0) return false
    let landed = false
    setRefusedBot('')
    setFailedKeys([])
    refuse(null)
    const values: Record<string, unknown> = {}
    for (const key of dirtyKeys) {
      const pending = edits[key]
      const setting = settings.find(s => s.key === key)
      if (!setting || !rowEditable(setting)) continue
      if (pending.clear) {
        values[key] = null
      } else if (setting.kind === 'bool') {
        values[key] = Boolean(pending.value)
      } else if (setting.kind === 'int' || setting.kind === 'float') {
        // Send a number: the server rejects a wrong type, so a blank/half-typed field fails here with a field-level
        // message instead of a 422.
        const parsed = Number(String(pending.value).trim())
        if (String(pending.value).trim() === '' || !Number.isFinite(parsed)) {
          setSaveError(`${setting.label} must be a number.`)
          refuse(key)
          return false
        }
        // An int knob rejects `80.7` rather than truncating and reporting "Saved".
        if (setting.kind === 'int' && !Number.isInteger(parsed)) {
          setSaveError(`${setting.label} must be a whole number.`)
          refuse(key)
          return false
        }
        values[key] = parsed
      } else {
        values[key] = String(pending.value)
      }
    }
    const putValues = (body: Record<string, unknown>) => put<SettingsPayload>('/api/settings', body).catch((err: unknown) => {
      if (isTokenRefused(err)) setRefusedBot(String(values[TELEGRAM_BOT_KEY] ?? ''))
      throw err
    })
    /* Every knob the refused save carried is marked, and the one it names says why on its row. A refusal can follow a
     * partial write (a refused Telegram bot is put back after the rest of the save landed), so the page re-reads what
     * is saved and drops the edits that now match it: what stays marked is what did not land. */
    await run('Failed to save settings', () => putSettings().catch((err: unknown) => {
      setFailedKeys(Object.keys(values))
      refuse(refusedKnob(err, Object.keys(values), settings))
      if (err instanceof ApiError) {
        void queryClient.fetchQuery({ queryKey: SETTINGS_QUERY_KEY, queryFn: fetchSettings, staleTime: 0 })
          .then(fresh => setEdits(prev => Object.fromEntries(Object.entries(prev).filter(([key, e]) => {
            const row = fresh.settings.find(s => s.key === key)
            return !row || !editSaved(row, e)
          }))))
          .catch(() => undefined)
      }
      throw err
    }))
    return landed
    async function putSettings() {
      let data: SettingsPayload
      try {
        data = await putValues({ values })
      } catch (err) {
        /* The Telegram bot picked delivers to another deployment (a bot holds one webhook): the server asks
         * before taking it (`routes/settings.py`), and the save goes again only on a yes. */
        const elsewhere = err instanceof ApiError && err.status === 409
          ? (err.detail as { code?: unknown; url?: unknown } | null) : null
        if (elsewhere?.code !== TELEGRAM_WEBHOOK_ELSEWHERE) throw err
        const take = await confirm({
          title: 'Take this Telegram bot?',
          message: <>
            It delivers elsewhere — another deployment, or this one's old address:{' '}
            <Box component="span" sx={{ overflowWrap: 'anywhere', fontWeight: 600 }}>{String(elsewhere.url)}</Box>.
            Saving points it here, and anything there stops receiving its Telegram messages.
          </>,
          confirmLabel: 'Take it',
          destructive: true,
        })
        // Thrown as the server's own sentence, so the save error reads it as is.
        if (!take) throw new ApiError(409, { key: TELEGRAM_BOT_KEY, message: 'Not saved: the bot still delivers elsewhere.' })
        data = await putValues({ values, take_telegram_webhook: true })
      }
      // The PUT returns the read model, so a changed verdict (org rehomed, flag revoked) lands now — over any
      // re-read a refused save left in flight, which would otherwise land after it with the older values.
      await queryClient.cancelQueries({ queryKey: SETTINGS_QUERY_KEY })
      queryClient.setQueryData(SETTINGS_QUERY_KEY, data)
      // An org's retention range follows these knobs: its form re-reads the bounds rather than show the old ones.
      if (Object.keys(values).some(key => RETENTION_BOUND_KEYS.includes(key))) {
        void queryClient.invalidateQueries({ queryKey: RETENTION_QUERY_ROOT })
      }
      landed = true
      setEdits({})
      setSavedCount(Object.keys(values).length)
      setSaveCounts(prev => {
        const next = { ...prev }
        for (const key of Object.keys(values)) next[key] = (next[key] ?? 0) + 1
        return next
      })
    }
  }

  // Leaving the page (a link, a reload, a closed tab) with edits pending asks first.
  const leave = useLeaveGuard(dirtyKeys.length > 0, save)

  return {
    settings,
    loadError,
    editable,
    orgEditable,
    edits,
    resolved,
    hosts,
    connections,
    connectionsLoading,
    /** A connection list that LOADED and is empty: a failed read (data null) says nothing. */
    connectionsNone: !connectionsLoading && connectionsResource.data?.length === 0,
    visibleKnobs,
    rowEditable,
    canSave,
    dirtyKeys,
    unsavedIn,
    edit,
    editSwitches,
    discard,
    save,
    saving,
    saveError,
    /** The bot credential a token refusal on the last save points at; '' otherwise. */
    refusedBot,
    /** Whether `key` rode the last refused save and is still pending. */
    failedKey: (key: string) => edits[key] !== undefined && failedKeys.includes(key),
    /** The labels of the pending knobs the last refused save carried, for the page-level alert. */
    failedLabels: failedKeys
      .filter(key => edits[key] !== undefined)
      .map(key => settings?.find(s => s.key === key)?.label ?? key),
    /** The refused save's sentence, when it names one knob that is still on this page: said on that knob's row. */
    refusal: saveError && refused.key && settings?.some(s => s.key === refused.key)
      ? { key: refused.key, message: saveError, seq: refused.seq }
      : null,
    saved,
    /** How many knobs the last landed save wrote. */
    savedCount,
    /** How many times this page saved `key` (the same value included). */
    saveCount: (key: string) => saveCounts[key] ?? 0,
    /** The save's Telegram takeover confirm; `KnobAlerts` renders it. */
    confirmDialog,
    /** Leave for `path` (and router `state`), asking first while edits are pending. */
    leaveTo: leave.leaveTo,
    /** The leave gate's dialog; `KnobAlerts` renders it. */
    leaveDialog: (
      <UnsavedChangesDialog
        open={leave.open}
        saving={leave.saving}
        onChoose={choice => void leave.onChoose(choice)}
        message="Some settings are not saved. Save them before leaving?"
      />
    ),
  }
}

/** The knobs an org's retention range derives from (`app/org_retention.py::bounds`). */
const RETENTION_BOUND_KEYS = ['org_retention_min_days', 'org_retention_max_days', 'activity_events_retention_days']

/* The Telegram Bot knob, read by this save and its webhook panel (`./telegram/TelegramWebhookSection.tsx`). Here,
 * not in the panel: the community edition's tree leaves `settings/telegram/` out (packages/editions/features.toml
 * `telegram`), and the save ships. */

/** The Bot knob the webhook panel follows. */
export const TELEGRAM_BOT_KEY = 'notifications_telegram_credential_id'

/** The 422 `detail.code` of a refusal fixed in the bot's credential: Telegram rejected the token, or the credential
 *  holds none (`routes/settings.py::_telegram_refusal`). */
export const TELEGRAM_TOKEN_REFUSED = 'telegram_token_refused'

export const isTokenRefused = (err: unknown): boolean =>
  err instanceof ApiError && (err.detail as { code?: unknown } | null)?.code === TELEGRAM_TOKEN_REFUSED

/** The 409 `detail.code` of a save whose Telegram bot another deployment holds (`routes/settings.py`). */
export const TELEGRAM_WEBHOOK_ELSEWHERE = 'telegram_webhook_elsewhere'

/** "N unsaved", beside a section's title or a category's name; nothing when none. */
export function unsavedNote(n: number): ReactNode {
  if (n === 0) return null
  return (
    <Typography component="span" variant="caption" sx={{ color: 'primary.main', fontWeight: 600, whiteSpace: 'nowrap' }}>
      {n} unsaved
    </Typography>
  )
}

/** How long a searched-for row stays lit after a page jumps to it. */
export const HIGHLIGHT_MS = 2500

/** A search jump's landing: once the lit row (or panel) is in the DOM, scroll it to the middle, then unlight it
 *  after `HIGHLIGHT_MS`. `ready` re-runs it when the target may just have rendered (an area opened, knobs
 *  loaded). Settings lights its own hits; Products lights the hits Settings' search sends it. */
export function useLitJump(lit: string | null, clear: (next: null) => void, ready: unknown): void {
  useEffect(() => {
    if (!lit) return
    document.getElementById(lit)?.scrollIntoView({ block: 'center' })
    const timer = window.setTimeout(() => clear(null), HIGHLIGHT_MS)
    return () => window.clearTimeout(timer)
  }, [lit, clear, ready])
}

/* ── ONE SECTION'S CARD ────────────────────────────────────────────────────────────────────────────── */
/** A card of knob rows — or the read/write grid when the section is one. `id` anchors deep links
 *  and search jumps; `lit` is the row a search jump is lighting. */
export function KnobSection({
  knobs,
  entries,
  title,
  id,
  lit = null,
}: {
  knobs: KnobEditor
  entries: OperatorSetting[]
  title?: string
  id?: string
  lit?: string | null
}) {
  const theme = useTheme()
  const { edits, edit, editSwitches, rowEditable, resolved, hosts, connections, connectionsLoading, connectionsNone, unsavedIn, refusal, saving, save } = knobs
  /** The number field being typed in: it shows the raw digits; every other one groups them, as "Default ·" does. */
  const [focusedKey, setFocusedKey] = useState<string | null>(null)

  // One knob's control, in the ONE control column. A CLOSED value set becomes a select (anything else
  // would 422). A stored `credentials.id` becomes the credential picker (`ui/CredentialSelect`), checked
  // BEFORE `choices`: the catalogue makes them mutually exclusive, and a violation degrades to the picker,
  // which can only produce a legal value.
  function control(s: OperatorSetting, value: string | boolean, inputId: string, helpId: string) {
    if (s.kind === 'bool') {
      return (
        <SettingCheckbox
          id={inputId}
          name={s.key}
          label={s.label}
          help={s.help}
          checked={value as boolean}
          disabled={false}
          onChange={v => edit(s.key, { clear: false, value: v })}
          labelHidden
          testId="settings-bool"
        />
      )
    }
    const credentialTypes = s.kind === 'str' && s.credential_type?.length ? s.credential_type : null
    if (credentialTypes) {
      return (
        /* Width on a wrapper: the shared control takes no `sx`. Its inner TextField carries the theme's dense
         * margin like every other, zeroed here for the same reason as in `controlSx`. */
        <Box sx={{ width: CONTROL_WIDTH, '& .MuiFormControl-root': { my: 0 } }}>
          <CredentialSelect
            /* The `id` reaches the `<input>` (AsyncLookup → MUI `Autocomplete id`), pairing with the row's `htmlFor`. */
            id={inputId}
            /* The wire's `''` (unconfigured) ↔ the control's `null`, converted here. `''` must round-trip exactly; it
             * must NOT become the page's `null`/`{ clear: true }` (Reset: fall back to the default), or a clear would
             * become a reset. */
            value={(value as string) === '' ? null : (value as string)}
            /* The known name (see `resolved`); the saved id reads "Loading…" while its name is in flight, never the
             * id. A newly picked id is named by the roster it was picked from. */
            selected={resolved[value as string] ?? (value === s.value ? loadingName(value as string) : null)}
            /* The picker's ✕ is the row's ONE clear: with nothing to fall back to it drops the override, as
             * the row's "Clear" did, so the row draws no second one (`pickerClears`). */
            onChange={next => edit(s.key, next === null && fallbackLabel(s) === NOT_SET ? { clear: true } : { clear: false, value: next ?? '' })}
            fetchOptions={(query, signal) => loadCredentialOptions(
              query, signal, row => credentialOption(row, credentialTypes), { types: credentialTypes, sort: true },
            )}
            placeholder="Choose a credential"
          />
        </Box>
      )
    }
    /* After the credential branch, before `choices`: a host id, picked from the operator's hosts. */
    if (s.kind === 'str' && s.vnode_host) {
      const known = hosts.some(h => h.host_node_id === value)
      return (
        <TextField
          id={inputId}
          size="small"
          select
          value={value as string}
          onChange={e => edit(s.key, { clear: false, value: e.target.value })}
          slotProps={{ select: { native: true }, htmlInput: { 'aria-describedby': helpId } }}
          sx={controlSx}
        >
          <option value="">Not set</option>
          {/* A stored host outside the list stays selectable, worded, never the raw id. */}
          {value !== '' && !known && <option value={value as string}>Unknown host</option>}
          {hosts.map(h => (
            <option key={h.host_node_id} value={h.host_node_id}>{h.name}</option>
          ))}
        </TextField>
      )
    }
    /* A connection id, picked from the operator's cloud connections (the host picker's twin). An empty settled
     * list says why and where to add one: the knob takes only a connection the operator's org owns. */
    if (s.kind === 'str' && s.cloud_connection) {
      const known = connections.some(c => c.id === value)
      // Only a list that LOADED and is empty: a failed read (data null) says nothing.
      const none = connectionsNone
      const hintId = `${inputId}-hint`
      return (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
          <TextField
            id={inputId}
            size="small"
            select
            value={value as string}
            onChange={e => edit(s.key, { clear: false, value: e.target.value })}
            slotProps={{
              select: { native: true },
              htmlInput: { 'aria-describedby': none ? `${helpId} ${hintId}` : helpId },
            }}
            sx={controlSx}
          >
            <option value="">None</option>
            {/* A stored connection outside the list stays selectable, worded, never the raw id — "Loading…" until
            the list settles. */}
            {value !== '' && !known && (
              <option value={value as string}>{connectionsLoading ? 'Loading…' : 'Unknown connection'}</option>
            )}
            {connections.map(c => (
              <option key={c.id} value={c.id}>{connectionKnobLabel(c)}</option>
            ))}
          </TextField>
          {none && (
            <Typography id={hintId} variant="body2" sx={{ color: 'text.secondary' }} data-testid="settings-connection-hint">
              Needs one of your connections. Add one in{' '}
              <Link component={RouterLink} to={CLOUD_CONNECTIONS_URL}>{CLOUD_CONNECTIONS_PATH}</Link>.
            </Typography>
          )}
        </Box>
      )
    }
    const choices = s.kind === 'str' && s.choices?.length ? s.choices : null
    if (choices) {
      return (
        <TextField
          id={inputId}
          size="small"
          select
          value={value as string}
          onChange={e => edit(s.key, { clear: false, value: e.target.value })}
          // Native, like every select in this app (phones get their own picker); the `id` lands on the `<select>`.
          slotProps={{ select: { native: true }, htmlInput: { 'aria-describedby': helpId } }}
          sx={controlSx}
        >
          {/* An override the catalogue no longer offers stays visible as its own option (an unmatched native select
          renders blank). */}
          {!choices.includes(value as string) && (
            <option value={value as string}>{choiceLabel(s, value as string)}</option>
          )}
          {choices.map(choice => (
            <option key={choice} value={choice}>{choiceLabel(s, choice)}</option>
          ))}
        </TextField>
      )
    }
    const numeric = s.kind !== 'str'
    // A browser's number field cannot show a separator, so a grouped value rides a text field until focused — only
    // where grouping adds commas and nothing else ("1.50", "007" or a past-2^53 integer stay as stored).
    const grouped = numeric && focusedKey !== s.key ? knobNumber(s, String(value)) : null
    const showGrouped = grouped !== null && grouped !== String(value) && grouped.replace(/,/g, '') === String(value)
    return (
      <TextField
        id={inputId}
        size="small"
        type={numeric && !showGrouped ? 'number' : 'text'}
        value={showGrouped ? grouped : value as string}
        onChange={e => edit(s.key, { clear: false, value: e.target.value })}
        onFocus={e => {
          setFocusedKey(s.key)
          // The text -> number flip drops the browser's selection (Tab-in or a programmatic focus selects the whole
          // value), so typing would append to the old digits; select it again once the flip has rendered.
          if (showGrouped) {
            const el = e.target
            queueMicrotask(() => el.select())
          }
        }}
        onBlur={() => setFocusedKey(key => (key === s.key ? null : key))}
        // Enter saves, as a form field would: the Save bar is easy to miss below a long section.
        onKeyDown={e => {
          if (e.key === 'Enter' && !e.nativeEvent.isComposing && !saving) {
            e.preventDefault()
            void save()
          }
        }}
        slotProps={{
          htmlInput: {
            inputMode: s.kind === 'int' ? 'numeric' : s.kind === 'float' ? 'decimal' : undefined,
            step: s.kind === 'float' ? 'any' : undefined,
            min: s.minimum ?? undefined,
            max: s.maximum ?? undefined,
            'aria-describedby': helpId,
          },
          input: s.unit
            ? {
                endAdornment: (
                  <InputAdornment position="end">
                    <Typography variant="body2" sx={{ color: 'text.secondary', whiteSpace: 'nowrap' }}>{unitFor(s.unit, String(value))}</Typography>
                  </InputAdornment>
                ),
              }
            : undefined,
        }}
        /* A unit leaves a phone's 8rem track ~33px of input, which clipped a third digit: the value sits
         * right beside its unit, so the dead left padding and the adornment's gaps shrink, and a phone drops the
         * number spinner (no tap target there, and it took ~15px of the value's room). */
        sx={{
          ...controlSx,
          '& .MuiInputBase-input': {
            ...controlSx['& .MuiInputBase-input'],
            textAlign: numeric ? 'right' : 'left',
            ...(numeric && s.unit && { pl: '4px' }),
          },
          ...(s.unit && {
            '& .MuiInputBase-root': { ...controlSx['& .MuiInputBase-root'], pr: '10px' },
            '& .MuiInputAdornment-root': { ml: '4px' },
          }),
          ...(numeric && {
            [theme.breakpoints.only('xs')]: {
              '& input[type=number]': { MozAppearance: 'textfield' },
              '& input::-webkit-inner-spin-button, & input::-webkit-outer-spin-button': { WebkitAppearance: 'none', m: 0 },
            },
          }),
        }}
      />
    )
  }

  // The knob rows, one home for every card.
  function rows(entries: OperatorSetting[]) {
    const readOnlyCard = !entries.some(rowEditable)
    const cardRows = entries.map((s, idx) => {
      const pending = edits[s.key]
      const value = displayed(s, pending)
      const writable = rowEditable(s)
      /* A STORED override holding the very value it would fall back to reads as the default, with no Reset (it would
       * change nothing): an editor's row only — a reader's "Set for your organization" is who set it, not what it is.
       * Not an empty one: an override of `''` on a knob with nothing to fall back to keeps its Clear. */
      const storedAtDefault = !pending && s.overridden && writable && fallbackLabel(s) !== NOT_SET && atFallback(s, s.value)
      const isOverridden = (pending ? !pending.clear : s.overridden) && !storedAtDefault
      const inputId = `setting-${s.key}`
      const helpId = `setting-help-${s.key}`
      const rowId = `setting-row-${s.key}`
      const highlighted = lit === rowId
      const minControl = writable ? controlMin(s, value) : undefined
      /* A text value is words — a select's choice ("Business trial") or free text (a model id): at the two-column
         floor it got 8rem and clipped them, so a phone-width card gives it the row's full width. */
      const rowsMid = s.kind === 'str' ? ROWS_MID_CHOICE : ROWS_MID
      /* Nothing set this value: a plain "Default" source line. On a narrow card it stacks under the value as a row of
       * its own, so a card of defaults read "Default" under every value — there it is left out. */
      const atDefault = !isOverridden && (writable || !setByOperator(s))
      /* A one-word "Clear" rides the control's line below `ROWS_WIDE` instead of a row of its own (the card grew by a
       * row for one word); a longer "Reset to …" would squeeze the control, so it keeps stacking. Below `ROWS_WIDE` the
       * row keeps the phone shape — label above, control and Clear beside it: a third track at mid width starved the
       * control to a few letters. */
      // Only while it holds a credential: an override of `''` (an older ✕) leaves the picker nothing to clear.
      const pickerClears = Boolean(s.credential_type?.length) && fallbackLabel(s) === NOT_SET && value !== ''
      const clearBeside = writable && isOverridden && fallbackLabel(s) === NOT_SET && !pickerClears
      // An override the picker's ✕ clears: the provenance cell is empty, so it takes no grid row of its own.
      const metaEmpty = writable && isOverridden && pickerClears
      // A pending edit the last refused save carried: marked, so the operator sees what did not land.
      const failed = knobs.failedKey(s.key)
      // The knob the refusal names: its row says why, beside what is still saved.
      const refused = refusal?.key === s.key
      const mark = failed || refused ? theme.palette.error.main : theme.palette.primary.main
      /* Two columns with a provenance line (and no Clear beside the control): the provenance sits under the label and
       * the control spans both lines, so the row keeps its neighbours' height. */
      const metaUnderLabel = !atDefault && !clearBeside
      const controlCellSx = {
        [rowsMid]: metaUnderLabel ? { gridColumn: 2, gridRow: '1 / span 2' } : {},
        [ROWS_WIDE]: { gridColumn: 'auto', gridRow: 'auto' },
      }
      return (
        <Box
          key={s.key}
          id={rowId}
          data-testid={pending ? 'settings-row-dirty' : 'settings-row'}
          data-highlight={highlighted ? 'true' : undefined}
          data-failed={failed ? 'true' : undefined}
          sx={{
            ...(minControl && { '--knob-control-min': minControl }),
            display: 'grid',
            gridTemplateColumns: clearBeside ? 'minmax(0, 1fr) auto' : 'minmax(0, 1fr)',
            alignItems: 'center',
            gap: '0.375rem',
            py: '8px',
            // Before `ROWS_WIDE`, which overrides it on a card wide enough for both.
            [rowsMid]: {
              ...(!clearBeside && { gridTemplateColumns: MID_ROW_GRID }),
              gap: '0.25rem 1rem',
              minHeight: ROW_MIN_HEIGHT,
              py: '4px',
            },
            [ROWS_WIDE]: {
              gridTemplateColumns: readOnlyCard ? READ_ONLY_ROW_GRID : ROW_GRID,
              ...(readOnlyCard && { gridColumn: '1 / -1' }),
              /* A read-only card's provenance sits right after its value: a wider gap keeps the two apart. */
              gap: readOnlyCard ? '0 1.5rem' : '0 1rem',
              minHeight: ROW_MIN_HEIGHT,
              py: 0,
            },
            /* Out to the card's own padding (SectionCard's 1.5), no further: at -12px the card's clip
             * clipped the unsaved row's 3px inset bar away entirely. */
            mx: -1.5,
            px: 1.5,
            borderTop: idx === 0 ? 'none' : '1px solid',
            borderColor: 'divider',
            transition: 'background-color 600ms',
            ...(pending && {
              bgcolor: alpha(mark, 0.08),
              boxShadow: `inset 3px 0 0 ${mark}`,
              ...theme.applyStyles('dark', {
                bgcolor: alpha(mark, 0.16),
              }),
            }),
            ...(highlighted && {
              bgcolor: alpha(theme.palette.warning.main, 0.18),
            }),
          }}
        >
          {/* `relative`: the visually hidden help is positioned against this cell, not the page — out of every
              scroller, each span stretched the document past the viewport. */}
          <Box
            sx={{
              minWidth: 0,
              position: 'relative',
              ...(clearBeside && { gridColumn: '1 / -1' }),
              // The label at the foot of the row's first line, the provenance right under it.
              [rowsMid]: metaUnderLabel ? { gridRow: 1, alignSelf: 'end' } : {},
              [ROWS_WIDE]: { gridRow: 'auto', alignSelf: 'auto', ...(clearBeside && { gridColumn: 'auto' }) },
            }}
          >
            {/* A read-only row's value is TEXT, so `htmlFor` would name an id that never renders: a span there. */}
            <Typography
              component={writable ? 'label' : 'span'}
              htmlFor={writable ? inputId : undefined}
              data-testid="settings-row-label"
              sx={{ fontWeight: 500 }}
            >
              {/* Above the label at every width: below (MUI's default) hid the NEXT row's label, beside it hid this
                  row's own value, and on a phone a side tooltip runs off the screen. Covering the row above is the
                  trade: the reader is on this row. The FIRST row has no row above, only its card's title bar:
                  below, so the tip stays inside the card. */}
              <HelpLabel label={s.label} help={s.help} placement={idx === 0 ? 'bottom-start' : 'top-start'} />
            </Typography>
            <Box component="span" id={helpId} data-testid="settings-row-help" sx={VISUALLY_HIDDEN}>
              {s.help}
            </Box>
            {/* Under the label, inside the row's lines: a band of its own made a refused row twice as tall. */}
            {failed && !refused && (
              <Typography
                data-testid="settings-row-failed"
                variant="body2"
                sx={{ color: 'error.main', fontSize: '0.75rem', lineHeight: 1.4 }}
              >
                Not saved
              </Typography>
            )}
          </Box>
          {/* The control cell: the value as words for a reader, the control for an editor — both at the same
          left edge, right beside the label. */}
          {!writable ? (
            /* `settings-row-value`, not `settings-input-wrap`: an oracle asking for the input wrap here must find
             * nothing. */
            <Typography
              component="span"
              data-testid="settings-row-value"
              sx={{ fontWeight: 500, wordBreak: 'break-word', ...controlCellSx }}
            >
              {valueAsWords(s, value, resolved, hosts, connections, connectionsLoading)}
            </Typography>
          ) : (
            <Box data-testid="settings-input-wrap" sx={{ display: 'flex', width: '100%', ...controlCellSx, [ROWS_WIDE]: { width: 'auto', ...controlCellSx[ROWS_WIDE] } }}>
              {control(s, value, inputId, helpId)}
            </Box>
          )}
          {/* The provenance cell, right after the control: what the value falls back to, and the way back to it. */}
          <Box
            data-testid="settings-row-meta"
            sx={{
              fontSize: '0.75rem',
              lineHeight: 1.2,
              minWidth: 0,
              display: atDefault || metaEmpty ? 'none' : 'flex',
              alignItems: 'center',
              [rowsMid]: clearBeside ? { gridColumn: 'auto' } : { gridColumn: 1, gridRow: 2, alignSelf: 'start' },
              [ROWS_WIDE]: { display: 'flex', gridColumn: 'auto', gridRow: 'auto', alignSelf: 'auto' },
            }}
          >
            {!writable ? (
              /* The provenance line stays (a reader who cannot change a value asks who set it); the Reset button goes,
               * replaced by the fact stated.
               * For a tenant reader `overridden` means the ORGANIZATION holds its own value (`org_settings`), and
               * `fallback` is what it would inherit. With no org row, a value unequal to `OperatorSetting.default` was
               * set by the operator — not "Default". Neither non-org arm repeats the number (ui-copy-standards). */
              <Typography component="span" variant="body2" sx={{ color: 'text.secondary', fontSize: 'inherit' }}>
                {isOverridden
                  ? `Set here · otherwise ${fallbackLabel(s)}`
                  : setByOperator(s)
                    ? 'Set by the operator'
                    : 'Default'}
              </Typography>
            ) : isOverridden && pickerClears ? null : isOverridden ? (
              /* `mx: -4px` cancels the button's own padding so its text sits on the same left edge as "Default ·"
               * on the row above or below. */
              <Button
                type="button"
                variant="text"
                size="small"
                onClick={() => edit(s.key, { clear: true })}
                sx={{
                  ...touchTargetSx,
                  fontSize: '0.75rem',
                  px: '4px',
                  mx: '-4px',
                  textAlign: 'left',
                  // A number's "Reset to 16,384 tokens" is one phrase: never break it. A choice's label may be long.
                  ...((s.kind === 'int' || s.kind === 'float') && { whiteSpace: 'nowrap' }),
                  /* Two columns, a pointer's window (under the label, or a Clear beside the control): the button keeps
                   * WCAG's 24px target but takes a text line's height, so the row stays its neighbours' height. */
                  [theme.breakpoints.up('sm')]: {
                    [rowsMid]: { minHeight: 24, py: 0, my: '-5px' },
                    [ROWS_WIDE]: { minHeight: 32, py: '4px', my: 0 },
                  },
                }}
              >
                {/* Back to nothing: "Clear", not "Reset to unset" (jargon). */}
                {fallbackLabel(s) === NOT_SET ? 'Clear' : `Reset to ${fallbackLabel(s)}`}
              </Button>
            ) : (
              <Typography component="span" variant="body2" sx={{ color: 'text.secondary', fontSize: 'inherit', overflowWrap: 'anywhere' }}>
                Default · {fallbackLabel(s)}
              </Typography>
            )}
          </Box>
          {/* A chosen value's own warning, under the whole row so it reads as part of that choice. The colour rides
              `sx`: Typography's `color` prop lost to the body text colour here. */}
          {choiceNote(s, value) && (
            <Typography
              data-testid="settings-row-note"
              variant="body2"
              sx={{ color: 'warning.main', gridColumn: '1 / -1', [ROWS_WIDE]: { pb: '8px' } }}
            >
              {choiceNote(s, value)}
            </Typography>
          )}
          {/* The refused save, on the row it names: the page-level alert sits a screen away on a phone. Then what is
              still saved, so a row still showing the refused value does not read as half-saved. */}
          {refused && (
            <Typography
              data-testid="settings-row-refusal"
              variant="body2"
              sx={{ color: 'error.main', gridColumn: '1 / -1', overflowWrap: 'anywhere', [ROWS_WIDE]: { pb: '8px' } }}
            >
              {`${refusal.message} Still saved: ${savedAsWords(s, resolved, hosts, connections, connectionsLoading)}.`}
            </Typography>
          )}
        </Box>
      )
    })
    return readOnlyCard ? <Box data-testid="settings-rows-read-only" sx={READ_ONLY_CARD_GRID}>{cardRows}</Box> : cardRows
  }

  // A read/write section is a grid with no provenance column; every other section is rows.
  const grid = permissionGrid(entries)
  return (
    <Box id={id} data-testid="settings-section" sx={{ containerType: 'inline-size', containerName: KNOB_SECTION_CONTAINER }}>
      <SectionCard title={title || undefined} actions={unsavedNote(unsavedIn(entries))}>
        {grid && PermissionGrid ? (
          <PermissionGrid
            grid={grid}
            value={s => displayed(s, edits[s.key]) as boolean}
            onChange={editSwitches}
            editable={entries.every(rowEditable)}
            marked={s => edits[s.key] !== undefined}
            lit={lit}
            idPrefix="setting-row-"
          />
        ) : rows(entries)}
      </SectionCard>
    </Box>
  )
}

/* ── THE SAVE BAR ──────────────────────────────────────────────────────────────────────────────────── */
/** Discard / Save, ABSENT (not disabled) for a viewer who cannot save: `PUT /api/settings` refuses every
 *  non-operator, and a dead Save is chrome with no reason. `bar: false` renders the bare button cluster, for
 *  a host that places it itself (a tab strip's actions). */
export function KnobSaveBar({ knobs, bar = true }: { knobs: KnobEditor; bar?: boolean }) {
  const { canSave, dirtyKeys, saving, discard, save } = knobs
  if (!canSave) return null
  /* Off the phone bar the pair rides a toolbar row at a pointer's 32px: the 44px touch floor kept a filled Save a
   * head taller than the boxless Discard beside it. */
  const buttonSx = bar ? touchTargetSx : { ...touchTargetSx, minHeight: 32, minWidth: 'auto' }
  const buttons = (
    <Stack direction="row" spacing={1}>
      <Button
        type="button"
        variant="text"
        color="inherit"
        size="small"
        disabled={dirtyKeys.length === 0 || saving}
        onClick={discard}
        sx={buttonSx}
      >
        Discard
      </Button>
      <Button
        type="button"
        variant="contained"
        color="primary"
        size="small"
        disabled={dirtyKeys.length === 0 || saving}
        onClick={() => void save()}
        sx={buttonSx}
      >
        {saving ? 'Saving…' : `Save${dirtyKeys.length ? ` (${dirtyKeys.length})` : ''}`}
      </Button>
    </Stack>
  )
  if (!bar) return buttons
  // Sticky only while there is something to save: a clean bar's two dead buttons must not ride over the page.
  // A refusal keeps the bar up too: on a phone it is where the refusal is said.
  const sticky = dirtyKeys.length > 0 || saving || Boolean(knobs.saveError)
  return (
    <Box
      data-testid="settings-header"
      data-sticky={sticky ? '' : undefined}
      sx={{
        /* A phone scrolls the whole page, so the bar sticks to the top of the scrollport instead. Sticky
         * offsets count from `.app-main`'s padding edge, so the bar reaches up through that 0.75rem
         * (App.tsx's phone padding) to cover the content scrolling behind it. */
        [`@media ${PHONE_QUERY}`]: {
          position: sticky ? 'sticky' : 'static',
          top: '-0.75rem',
          mt: '-0.75rem',
          pt: '0.75rem',
          zIndex: 2,
          bgcolor: 'background.default',
          // Nothing to save, nothing to show: two dead buttons were a 69px band over the page.
          ...(!sticky && { display: 'none' }),
        },
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'flex-end',
        flexWrap: 'wrap',
        gap: 1,
        pb: '12px',
        borderBottom: '1px solid',
        borderColor: 'divider',
      }}
    >
      {buttons}
      {/* On a phone the refusal rides the sticky bar, so it is in view from wherever Save was pressed;
          `KnobAlerts` keeps it above a phone. A failed save keeps its edits, so the bar is up whenever this is. */}
      {knobs.saveError && (
        <Box sx={{ flexBasis: '100%', display: 'none', [`@media ${PHONE_QUERY}`]: { display: 'block' } }}>
          <SaveError knobs={knobs} />
        </Box>
      )}
    </Box>
  )
}

/** The last save's refusal; nothing when it landed. */
function SaveError({ knobs }: { knobs: KnobEditor }) {
  const { saveError, refusedBot, failedLabels, refusal, settings } = knobs
  if (!saveError) return null
  // The knob a refusal names, so the alert says which control failed (its row says why, a screen away on a phone).
  const knob = refusal ? settings?.find(s => s.key === refusal.key) : undefined
  const knobPath = knob && !saveError.startsWith(knob.label) ? [knob.section, knob.label].filter(Boolean).join(' › ') : ''
  return (
    <Banner
      severity="error"
      // Telegram refused the bot's token: the fix is in its credential, as the webhook panel says.
      action={refusedBot ? (
        <Button component={RouterLink} to={`/credentials/${refusedBot}`} color="inherit" size="small" sx={{ whiteSpace: 'nowrap' }}>
          Open credential
        </Button>
      ) : undefined}
    >
      {knobPath && <span>{knobPath}: </span>}
      <span>{saveError}</span>
      {/* Several knobs, one refusal naming at most one: the rest may sit on another category. */}
      {failedLabels.length > 1 && (
        <Box component="span" data-testid="settings-failed-labels" sx={{ display: 'block' }}>
          Not saved: {failedLabels.join(', ')}.
        </Box>
      )}
    </Banner>
  )
}

/** The load/save outcome, said once under the bar. */
export function KnobAlerts({ knobs }: { knobs: KnobEditor }) {
  const { loadError, saved, savedCount, editable, confirmDialog } = knobs
  const what = savedCount > 1 ? `Saved ${savedCount} changes.` : 'Saved.'
  return (
    <>
      {confirmDialog}
      {knobs.leaveDialog}
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {/* On a phone the Save bar carries it (`KnobSaveBar`). */}
      {knobs.saveError && (
        <Box sx={{ [`@media ${PHONE_QUERY}`]: { display: 'none' } }}>
          <SaveError knobs={knobs} />
        </Box>
      )}
      {saved && (
        <Alert severity="success" role="status">{editable ? `${what} Live across the fleet within seconds.` : what}</Alert>
      )}
    </>
  )
}
