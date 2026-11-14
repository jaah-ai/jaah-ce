import { type JSX, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { useConfirm } from '../../components/ConfirmDialog'
import { useSubmit } from '../../hooks/useSubmit'
import { useTaskModels } from '../../hooks/useTaskModels'
import { routingRank, ROUTING_MAX_OPTIONS } from '../../lib/routingModes'
import type { Org, RoutingAllowed, RoutingMaxMode } from '../../types'
import { grid } from '../../theme'
import Banner from '../../ui/Banner'
import { DetailSection } from '../../ui/detail/DetailPage'
import FieldRow from '../../ui/FieldRow'
import FormTextField from '../../ui/FormTextField'
import SettingCheckbox from '../../ui/SettingCheckbox'
import SubmitButton from '../../ui/SubmitButton'
import { type AgentChoice, listAgentChoices } from '../agents/api'
import { updateOrg } from './api'

/* AN ORG'S INTELLIGENT ROUTING (`routes/orgs.py::_apply_routing`): the highest mode its projects may run, and the policy
 * the router picks inside — which runners, and the model tier range. The Org Owner/Admin's own card in Settings ›
 * Organizations. Off the plan the card still shows, every control disabled under the reason (the API answers 403). */

/** Why every control here is disabled off the plan — `app/agent_router/modes.py::NOT_ON_PLAN`. */
export const NOT_ON_PLAN = "Intelligent routing isn't included in your plan."

/** A choice's key in the allowed set: a built-in by provider, an SDK agent by id. */
const keyOf = (c: AgentChoice): string => (c.kind === 'builtin' ? `p:${c.provider}` : `a:${c.id}`)

function allowedKeys(allowed: RoutingAllowed | null): Set<string> | null {
  if (!allowed) return null
  return new Set([
    ...(allowed.providers ?? []).map(p => `p:${p}`),
    ...(allowed.agent_ids ?? []).map(id => `a:${id}`),
  ])
}

/** What each maximum does (`app/agent_router/modes.py`): the pop-up's one line per choice. */
const MAX_MODE_HELP: Record<RoutingMaxMode, string> = {
  off: 'Projects cannot route; the router only measures.',
  recommend: "Tasks show the router's pick; one click accepts it.",
  auto: "Dispatch runs on the router's pick, inside this policy.",
}

const MAX_MODE_EXPLAIN = {
  tip: 'Caps the routing mode any project may choose.',
  more: 'The highest routing mode a project of this organization may pick. It saves as soon as you pick it. '
    + 'Lowering it drops every project above it to the new maximum, and raising it again later does not restore them.',
  docs: 'administration/security-and-audit/settings',
}

/** A tier select's options, led by `No limit`, each with the tiers routing may then pick (the catalogue's order is
 *  most capable first): `keep` is `'above'` for the lowest tier, `'below'` for the highest. */
function tierOptions(tiers: readonly { value: string; label: string }[], keep: 'above' | 'below') {
  const names = (list: readonly { label: string }[]) => list.map(t => t.label).join(', ')
  return [
    { value: '', label: 'No limit', help: 'Routing may pick any tier.' },
    ...tiers.map((t, i) => ({
      value: t.value,
      label: t.label,
      help: `Routing picks only ${names(keep === 'above' ? tiers.slice(0, i + 1) : tiers.slice(i))}.`,
    })),
  ]
}

export default function RoutingTab({ org, onOrgSaved }: { org: Org; onOrgSaved: (org: Org) => void }): JSX.Element {
  const routing = org.routing
  const offered = routing?.offered === true
  const maxMode: RoutingMaxMode = routing?.max_mode ?? 'off'
  const [confirm, confirmDialog] = useConfirm()
  const mode = useSubmit()
  const policy = useSubmit()

  const [picked, setPicked] = useState<Set<string> | null>(() => allowedKeys(routing?.allowed ?? null))
  const [floor, setFloor] = useState(routing?.tier_floor ?? '')
  const [ceiling, setCeiling] = useState(routing?.tier_ceiling ?? '')

  const choices = useQuery({ queryKey: ['agent-choices'], queryFn: () => listAgentChoices(), enabled: offered })
  const tiers = useTaskModels()
  // Off the plan the runners are not read (every control is disabled); the tier catalogue still is, for the pop-ups.
  const runners = (Array.isArray(choices.data) ? choices.data : []).filter(c => !c.removed)
  const tierList = Array.isArray(tiers.data) ? tiers.data : []

  async function saveMax(next: RoutingMaxMode) {
    if (routingRank(next) < routingRank(maxMode)) {
      const ok = await confirm({
        title: 'Lower routing maximum',
        message: `Every project set above ${ROUTING_MAX_OPTIONS.find(o => o.value === next)!.label} drops to it. `
          + 'Raising it again later does not restore them.',
        confirmLabel: 'Lower',
      })
      if (!ok) return
    }
    await mode.run('Failed to save the maximum mode.', async () => {
      onOrgSaved(await updateOrg(org.id, { routing_max_mode: next }))
    })
  }

  function toggle(key: string, on: boolean) {
    const next = new Set(picked ?? runners.map(keyOf))
    if (on) next.add(key)
    else next.delete(key)
    setPicked(next)
  }

  function allowedBody(): RoutingAllowed | null {
    if (picked === null) return null
    const keys = [...picked]
    return {
      providers: keys.filter(k => k.startsWith('p:')).map(k => k.slice(2)),
      agent_ids: keys.filter(k => k.startsWith('a:')).map(k => k.slice(2)),
    }
  }

  const sameKeys = (a: Set<string> | null, b: Set<string> | null) =>
    a === b || (a !== null && b !== null && a.size === b.size && [...a].every(k => b.has(k)))
  const policyChanged = !sameKeys(picked, allowedKeys(routing?.allowed ?? null))
    || floor !== (routing?.tier_floor ?? '') || ceiling !== (routing?.tier_ceiling ?? '')

  const runnersChanged = !sameKeys(picked, allowedKeys(routing?.allowed ?? null))
  const savePolicy = () => policy.run('Failed to save the routing policy.', async () => {
    onOrgSaved(await updateOrg(org.id, {
      // Only a changed selection: a stored agent the org has since lost is the server's to drop, not a resend's.
      ...(runnersChanged ? { routing_allowed: allowedBody() } : {}),
      routing_tier_floor: floor || null,
      routing_tier_ceiling: ceiling || null,
    }))
  })

  return (
    <Box data-testid="org-routing-tab" sx={{ maxWidth: grid(100), minWidth: 0 }}>
      {confirmDialog}
      {!offered && <Box sx={{ mb: 1.5 }}><Banner severity="info" testId="org-routing-off-plan">{NOT_ON_PLAN}</Banner></Box>}
      <DetailSection title="Mode" headingLevel="h3">
        <FormTextField
          id="org-routing-max-mode"
          label="Maximum mode"
          help={MAX_MODE_EXPLAIN}
          value={maxMode}
          /* Off the plan it may still be lowered (the API allows that alone), so projects left above wind down. */
          readOnly={(!offered && maxMode === 'off') || mode.busy}
          onChange={v => void saveMax(v as RoutingMaxMode)}
          options={ROUTING_MAX_OPTIONS.map(o => ({
            ...o,
            help: MAX_MODE_HELP[o.value],
            ...(offered ? {} : { disabled: routingRank(o.value) > routingRank(maxMode) }),
          }))}
          error={mode.formError || undefined}
          hint={mode.saved ? 'Saved.' : undefined}
        />
      </DetailSection>
      <DetailSection title="Policy" divided headingLevel="h3">
        <Stack spacing={1.5}>
          <SettingCheckbox
            name="org-routing-all-runners"
            label="Allow every runner"
            help="Off, routing picks only from the runners checked below."
            checked={picked === null}
            disabled={!offered || choices.isLoading}
            onChange={all => setPicked(all ? null : new Set(runners.map(keyOf)))}
          />
          {picked !== null && (
            <Box data-testid="org-routing-runners" sx={{ display: 'flex', flexDirection: 'column', pl: 1 }}>
              {runners.length === 0 && (
                <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                  {choices.isLoading ? 'Loading runners…' : 'No runners.'}
                </Typography>
              )}
              {runners.map(c => (
                <SettingCheckbox
                  key={keyOf(c)}
                  name={`org-routing-runner-${keyOf(c)}`}
                  label={c.display_name}
                  labelPlacement="end"
                  checked={picked.has(keyOf(c))}
                  disabled={!offered}
                  onChange={on => toggle(keyOf(c), on)}
                />
              ))}
            </Box>
          )}
          <FieldRow align="start">
            <Box sx={{ flex: `1 1 ${grid(26.5)}`, minWidth: 0 }}>
              <FormTextField
                id="org-routing-tier-floor"
                label="Lowest tier"
                value={floor}
                help="Routing never picks a model tier below this."
                readOnly={!offered}
                onChange={setFloor}
                options={tierOptions(tierList, 'above')}
              />
            </Box>
            <Box sx={{ flex: `1 1 ${grid(26.5)}`, minWidth: 0 }}>
              <FormTextField
                id="org-routing-tier-ceiling"
                label="Highest tier"
                value={ceiling}
                help="Routing never picks a model tier above this."
                readOnly={!offered}
                onChange={setCeiling}
                options={tierOptions(tierList, 'below')}
              />
            </Box>
          </FieldRow>
          {policy.formError && <Banner severity="error" testId="org-routing-policy-error">{policy.formError}</Banner>}
          <Box>
            <SubmitButton
              busy={policy.busy}
              disabled={!offered || !policyChanged}
              onClick={() => void savePolicy()}
              testId="org-routing-policy-save"
            >
              {policy.saved ? 'Saved' : 'Save'}
            </SubmitButton>
          </Box>
        </Stack>
      </DetailSection>
    </Box>
  )
}
