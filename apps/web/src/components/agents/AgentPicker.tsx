import { useEffect, useState } from 'react'
import Typography from '@mui/material/Typography'
import { useAuth, useCan, useCanOnAnyProject } from '../../auth'
import { FormTextField } from '../../ui/FormTextField'
import { FIELD_SX } from '../../ui/formDensity'
import { get } from '../../api'
import { type Agent, type AgentChoice, DISABLED, listAgentChoices, listAgents, REMOVED } from '../../pages/agents/api'
import type { ProviderChoice } from '../../types'
import { agentsOn as gridAgentsOn, providerOn } from '../../lib/providerGrid'
import { cachedRead, CATALOGUE_STALE_MS, ROSTER_STALE_MS } from '../../lib/cachedRead'
import { readAsOnce } from '../../lib/actAs'
import type { OrgRef } from '../../types'

/* ── THE ONE AGENT PICKER (AC-10) ─────────────────────────────────────────────────────────────────────────
 * One native <select> over `GET /api/agents/choices`: the built-in runners the org can run, then the org's
 * own agents and the catalog connectors it enabled. It writes ONE of two wire fields — `bound_provider` (a
 * built-in) or `agent_id` (an SDK agent), never both (`ck_tasks_agent_or_provider`) — so a pick is the pair
 * `{ agentId, provider }` with at most one half set. Used by the task form (`TaskSpecFields`) and a project's
 * Default agent (`ProjectEditPage`).
 * An agent that cannot run the persona stays listed, disabled, with the server's reason: never silently
 * dropped. A stored pick the list lacks keeps its own option so a save never clears it. A failed fetch is
 * logged and leaves the default and the stored pick (nothing the operator did failed).
 * The org's provider grid narrows the offer (`lib/providerGrid`): no `sdk` or `acp:` row on offers no agent and
 * reads none (the server's choices hold each agent to its own row); only the built-ins with a cell on are offered.
 * A stored pick either hides still shows.
 * `org`: another org's row (an operator's) — every read goes as that org (`lib/actAs.ts::readAsOnce`) and
 * is cached under it, so the offer is that org's agents, never the viewer's. */

export type AgentPick = { agentId: string | null; provider: string }

export const NO_PICK: AgentPick = { agentId: null, provider: '' }

const BUILTIN = 'builtin:'
const AGENT = 'agent:'

/** The select's value for a pick: `''` default, `builtin:<provider>`, `agent:<uuid>`. */
export function encodePick(pick: AgentPick): string {
  if (pick.agentId) return `${AGENT}${pick.agentId}`
  if (pick.provider) return `${BUILTIN}${pick.provider}`
  return ''
}

export function decodePick(value: string): AgentPick {
  if (value.startsWith(AGENT)) return { agentId: value.slice(AGENT.length), provider: '' }
  if (value.startsWith(BUILTIN)) return { agentId: null, provider: value.slice(BUILTIN.length) }
  return NO_PICK
}

/** The server's reason without the leading name the option already shows ("X does not serve …" → "does not
 *  serve …"). */
function reasonTail(choice: AgentChoice): string {
  const reason = choice.reason ?? 'unavailable'
  return reason.startsWith(choice.display_name) ? reason.slice(choice.display_name.length).trim() : reason
}

/** An agent's option text: its name alone (a narrow pane cuts a longer label), a disabled one's reason after a
 *  dash. The picked agent's type and version ride the caption under the select (`agentKindCaption`). */
export function agentOptionLabel(choice: AgentChoice): string {
  return choice.disabled ? `${choice.display_name} — ${reasonTail(choice)}` : choice.display_name
}

/** The picked agent's caption: type, the catalog marked, version ("non_coding · catalog · v1.2.0"). */
export function agentKindCaption(choice: AgentChoice): string {
  const version = choice.version ? `v${choice.version.replace(/^v/, '')}` : null
  return [choice.agent_type, choice.is_catalog ? 'catalog' : null, version].filter(Boolean).join(' · ')
}

/** May this principal write a task's agent? The two halves ride `tasks:create` / `tasks:edit` (DD13), held
 *  org-wide or on any project — not an admin tier, unlike the old Provider pin. Presentation only. */
export function useCanPickAgent(): boolean {
  const can = useCan()
  const canOnAny = useCanOnAnyProject()
  return (['tasks:edit', 'tasks:create'] as const).some(key => can(key) || canOnAny(key))
}

/** A project's unpicked Default agent, everywhere it is shown: `agent_resolution.resolve_agent`'s last step, the
 *  first free engine. One word for one state (the wizard and the edit page once said "Any" and "None"). */
export const ANY_AGENT_LABEL = 'Any'

/** The orphan label while the agent list is still loading: not a name, so never handed to `onPickLabel`. */
const LOADING_LABEL = 'Loading…'

export function AgentPicker({
  id,
  label,
  help,
  value,
  onChange,
  persona = null,
  defaultLabel,
  disabled = false,
  builtinsOnly = false,
  error,
  onPickLabel,
  org = null,
}: {
  id: string
  label: string
  /** A few words shown on hovering the label (`ui/HelpLabel`). */
  help?: string
  value: AgentPick
  onChange: (next: AgentPick) => void
  /** The task's persona: an agent that does not serve it is listed disabled. Null for a project default. */
  persona?: string | null
  /** The empty option's words: what an unpicked row does. */
  defaultLabel: string
  disabled?: boolean
  /** Offer the built-in half only (a surface that cannot store `agent_id`, an automation's Task action). */
  builtinsOnly?: boolean
  error?: string
  /** Told the picked option's visible text (the default's words when unpicked), for a surface that repeats it;
   *  null while a picked agent's name is still loading, so the surface keeps its unpicked wording. */
  onPickLabel?: (label: string | null) => void
  /** Another org's row: its agents are offered, read as that org. Null for the viewer's own. */
  org?: OrgRef | null
}) {
  /* Reading agents takes `agents:view` (the roles that write tasks hold it, DD13). Without it — a project
   * Viewer reading a task's pick — neither agent read is attempted: the built-in half falls back to the provider
   * catalogue (`GET /api/accounts/providers`, the old Provider select's list), and a stored agent is named by id
   * alone rather than claimed unavailable. */
  const can = useCan()
  const canOnAny = useCanOnAnyProject()
  /* Agents off the grid reads as no `agents:view`: the same fallback, and no agent offered. The viewer's grid, for its
   * own rows only: another org's choices come back as that org's plan allows them. */
  const { admin } = useAuth()
  const agentsOn = gridAgentsOn(admin) || org !== null
  const canViewAgents = agentsOn && (can('agents:view') || canOnAny('agents:view'))

  const [choices, setChoices] = useState<readonly AgentChoice[]>([])
  const [loaded, setLoaded] = useState(false)
  const orgId = org?.id ?? null
  // Another org's cache entry; the viewer's own keeps the key other surfaces share (`TaskSpecFields`).
  const keyed = (...key: unknown[]) => (orgId === null ? key : [...key, orgId])
  useEffect(() => {
    let live = true
    setChoices([])
    setLoaded(false)
    /* Both reads, and the picked agent's below, go through `lib/cachedRead`: the task form remounts on every
     * own-task event, and a remount answers from cache instead of re-reading the catalogues. */
    const builtinFallback = () => cachedRead(keyed('agent-providers'), () => get<ProviderChoice[]>('/api/accounts/providers', ...readAsOnce(org)), CATALOGUE_STALE_MS)
      .then(rows => (Array.isArray(rows) ? rows : []).map((p): AgentChoice => ({ kind: 'builtin', provider: p.value, display_name: p.label })))
    const read = canViewAgents
      ? cachedRead(keyed('agent-choices', persona ?? null), () => listAgentChoices(persona, org), ROSTER_STALE_MS).catch(err => {
        console.error('the agent choices could not be loaded:', err)
        return builtinFallback()
      })
      : builtinFallback()
    read
      .then(rows => { if (live && Array.isArray(rows)) setChoices(rows) })
      .catch(err => console.error('the provider catalogue could not be loaded:', err))
      .finally(() => { if (live) setLoaded(true) })
    return () => { live = false }
  }, [persona, canViewAgents, orgId])

  /* The picked agent's row, for its name when the choices lack it (a disabled or withdrawn agent is not
   * offered), its status, and its `resolve_error` (a refresh that failed: the last good snapshot still runs,
   * but someone should look). Fetched only when an agent is picked. */
  const [picked, setPicked] = useState<Agent | null>(null)
  useEffect(() => {
    setPicked(null)
    if (!value.agentId || !canViewAgents) return
    let live = true
    cachedRead(keyed('agents-list'), () => listAgents(org), ROSTER_STALE_MS)
      .then(rows => { if (live && Array.isArray(rows)) setPicked(rows.find(a => a.id === value.agentId) ?? null) })
      .catch(err => console.error('the agent list could not be loaded:', err))
    return () => { live = false }
  }, [value.agentId, canViewAgents, orgId])

  /* The viewer's provider grid narrows the built-ins (`lib/providerGrid`); another org's choices come back as that
   * org's plan allows them. */
  const builtins = choices.filter(c => c.kind === 'builtin' && (org !== null || providerOn(admin, c.provider ?? '')))
  // A connector removed from the catalog is offered only to the row that already names it (its value stays put).
  const agents = builtinsOnly || !canViewAgents
    ? []
    : choices.filter(c => c.kind === 'agent' && (!c.removed || c.id === value.agentId))
  const selected = encodePick(value)
  const known = selected === '' || [...builtins, ...agents].some(c => encodePick({
    agentId: c.kind === 'agent' ? (c.id ?? null) : null,
    provider: c.kind === 'builtin' ? (c.provider ?? '') : '',
  }) === selected)
  const pickedChoice = value.agentId ? agents.find(c => c.id === value.agentId) : undefined
  /* Why a picked agent will not run, when the picker can tell: offered but disabled for this persona, or absent
   * from the offer because it was disabled or withdrawn. Tasks naming it fail at dispatch, saying so (DD19). */
  const pickedStatus = picked?.unsupported ? 'unsupported'
    : picked?.status === REMOVED ? 'removed from catalog' : picked?.status === DISABLED ? 'disabled' : null
  // An unsupported (SDK v1) agent is never in the choices (the server omits it); a stored pick of one says why.
  const pickReason = picked?.unsupported ? picked.unsupported
    : pickedChoice?.disabled ? pickedChoice.reason ?? null
    : pickedStatus && picked ? `${picked.display_name} is ${pickedStatus} — tasks using it fail to start` : null
  const orphanLabel = value.agentId
    ? picked
      ? `${picked.display_name}${pickedStatus ? ` (${pickedStatus})` : ''}`
      : !loaded ? LOADING_LABEL : canViewAgents ? 'Unavailable agent' : `Agent ${value.agentId.slice(0, 8)}`
    : choices.find(c => c.kind === 'builtin' && c.provider === value.provider)?.display_name ?? value.provider
  const pickLabel = selected === '' ? defaultLabel
    : !known ? orphanLabel
      : value.agentId ? (pickedChoice?.display_name ?? orphanLabel)
        : builtins.find(c => c.provider === value.provider)?.display_name ?? value.provider
  useEffect(() => { onPickLabel?.(pickLabel === LOADING_LABEL ? null : pickLabel) }, [onPickLabel, pickLabel])
  const reasonId = `${id}-reason`
  const warningId = `${id}-warning`
  const describedBy = [pickReason ? reasonId : null, picked?.resolve_error ? warningId : null].filter(Boolean).join(' ')

  return (
    <>
      <FormTextField
        id={id}
        label={label}
        help={help}
        sx={FIELD_SX}
        select
        // `shrink`: the default option is `''`, so MUI would float the label over it.
        slotProps={{
          select: { inputProps: { 'aria-label': label, ...(describedBy ? { 'aria-describedby': describedBy } : {}) } },
          inputLabel: { shrink: true },
        }}
        value={selected}
        disabled={disabled}
        error={error}
        hint={pickedChoice ? agentKindCaption(pickedChoice) || undefined : undefined}
        onChange={v => onChange(decodePick(v))}
        fullWidth
      >
        <option value="">{defaultLabel}</option>
        {!known && <option value={selected}>{orphanLabel}</option>}
        {builtins.length > 0 && (
          <optgroup label="Built-in">
            {builtins.map(c => (
              <option key={`b-${c.provider}`} value={`${BUILTIN}${c.provider}`}>{c.display_name}</option>
            ))}
          </optgroup>
        )}
        {agents.length > 0 && (
          <optgroup label="Your agents & catalog">
            {agents.map(c => (
              <option key={`a-${c.id}`} value={`${AGENT}${c.id}`} disabled={c.disabled} title={c.reason ?? undefined}>
                {agentOptionLabel(c)}
              </option>
            ))}
          </optgroup>
        )}
      </FormTextField>
      {/* Visible, not a help caption: a picked agent that will not run is a fact the operator must see. */}
      {pickReason && (
        <Typography id={reasonId} component="p" variant="caption" data-testid="agent-pick-reason" sx={{ color: 'warning.main', mt: '0.35rem' }}>
          {pickReason}
        </Typography>
      )}
      {picked?.resolve_error && (
        <Typography id={warningId} component="p" variant="caption" data-testid="agent-resolve-warning" sx={{ color: 'warning.main', mt: '0.35rem' }}>
          ⚠ Last refresh failed: {picked.resolve_error}
        </Typography>
      )}
    </>
  )
}

export default AgentPicker
