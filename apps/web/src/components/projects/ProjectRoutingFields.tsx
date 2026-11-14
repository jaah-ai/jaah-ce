import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { routingRank, ROUTING_MODE_OPTIONS } from '../../lib/routingModes'
import { grid } from '../../theme'
import type { Project, RoutingMode } from '../../types'
import { DetailCell, DetailSection } from '../../ui/detail/DetailPage'
import FieldRow from '../../ui/FieldRow'
import FormTextField from '../../ui/FormTextField'
import { PERCENT_SLOTS } from '../../ui/moneyInput'
import SettingCheckbox from '../../ui/SettingCheckbox'
import AdvancedGroup from './AdvancedGroup'
import type { ProjectFormState } from './projectForm'

const ROUTING_DOCS = 'console-reference/directory/projects'
/** The group's explanation, for a caller that names it itself (`bare`). */
export const ROUTING_HELP = { tip: "Lets a router pick each task's runner.", docs: ROUTING_DOCS }

/* What picking each mode does, as a line of the Mode field's pop-up. */
const MODE_HELP: Record<RoutingMode, string> = {
  inherit: 'No routing; tasks run as picked.',
  recommend: "Each task shows the router's pick; you choose.",
  auto: "Tasks run on the router's pick.",
}

/* A PROJECT'S INTELLIGENT ROUTING (`routes/projects.py::_validated_routing`): its mode, capped at what the org allows
 * now (`Project.routing_cap`; a mode above it is disabled, the reason under the field), its latency cap, its experiment
 * budget (a percent here, `experiment_budget_share` 0..1 on the wire) and blind review. Part of the project form: saved with everything else by its one Save bar.
 * With nothing settable (the cap Off, the mode Off, blind review off) the group is not drawn at all. */

export default function ProjectRoutingFields({ form, project, patch, errorFor, bare = false }: {
  form: ProjectFormState
  /** A compact group (`AdvancedGroup`), no titled section: the project page's Advanced. */
  bare?: boolean
  project: Project | null | undefined
  patch: (next: Partial<ProjectFormState>) => void
  errorFor: (key: string) => string | undefined
}): JSX.Element | null {
  const cap = project?.routing_cap ?? 'off'
  const above = ROUTING_MODE_OPTIONS.some(o => routingRank(o.value) > routingRank(cap))
  const options = ROUTING_MODE_OPTIONS.map(o => ({ ...o, help: MODE_HELP[o.value], disabled: routingRank(o.value) > routingRank(cap) }))
  // Why Blind review cannot be turned on; a save turning it on would be refused with the same words.
  const blindBlocked = errorFor('blind_review') ?? project?.blind_review_reason ?? undefined
  /* Nothing here can run (no mode above Off, routing and blind review off): nothing is drawn. A saved mode or
     blind review still draws the fields, so they can be turned off. */
  if (cap === 'off' && project?.routing_cap_reason && form.routing_mode === 'inherit' && !form.blind_review) return null
  const fields = (
      <Box sx={{ minWidth: 0 }}>
        <FieldRow align="start" rowGap={2}>
          <DetailCell basis={grid(32)} max={grid(48)}>
            <FormTextField
              id="project-routing-mode"
              label="Mode"
              help={{
                tip: "How far the router decides each task's runner.",
                more: "A mode above what the organization allows can't be picked.",
                docs: ROUTING_DOCS,
              }}
              value={form.routing_mode}
              onChange={v => patch({ routing_mode: v as RoutingMode })}
              options={options}
              error={errorFor('routing_mode')}
              hint={above && project?.routing_cap_reason ? project.routing_cap_reason : undefined}
              testId="project-routing-mode"
            >
              {form.routing_mode === '' && <option value="">Not known</option>}
            </FormTextField>
          </DetailCell>
          <DetailCell basis={grid(24)} max={grid(32)}>
            <FormTextField
              id="project-routing-latency-cap"
              label="Latency cap"
              help="Most hours a routed task may take; empty, no cap."
              type="number"
              placeholder="hours"
              slotProps={{ inputLabel: { shrink: true } }}
              value={form.routing_latency_cap_hours}
              onChange={v => patch({ routing_latency_cap_hours: v })}
              error={errorFor('routing_latency_cap_hours')}
            />
          </DetailCell>
          <DetailCell basis={grid(24)} max={grid(32)}>
            <FormTextField
              id="project-experiment-budget"
              label="Experiment budget"
              help={{
                tip: 'Share of 30-day spend for trying other runners and benchmarks.',
                more: "It is also the cap on the Benchmark tab's monthly spend.",
                docs: ROUTING_DOCS,
              }}
              type="number"
              slotProps={PERCENT_SLOTS}
              value={form.experiment_budget_pct}
              onChange={v => patch({ experiment_budget_pct: v })}
              error={errorFor('experiment_budget_share') ?? errorFor('experiment_budget_pct')}
              testId="project-experiment-budget"
            />
          </DetailCell>
        </FieldRow>
        <FieldRow align="center" rowGap={0.5}>
          <DetailCell>
            <SettingCheckbox
              name="project-blind-review"
              label="Blind review"
              help={{
                tip: "Other agents review the PR without its author's reasoning.",
                more: "Only on Major and security changes. A line under it says why it can't run yet, such as no node of the project being able to.",
                docs: ROUTING_DOCS,
              }}
              checked={form.blind_review}
              disabled={!form.blind_review && !!blindBlocked}
              describedBy={blindBlocked ? 'project-blind-review-reason' : undefined}
              onChange={next => patch({ blind_review: next })}
            />
            {blindBlocked && (
              <Typography id="project-blind-review-reason" variant="caption"
                data-testid="project-blind-review-reason" sx={{ display: 'block', color: 'text.secondary' }}>
                {blindBlocked}
              </Typography>
            )}
          </DetailCell>
          <DetailCell>
            <SettingCheckbox
              name="project-blind-review-other-vendor"
              label="Prefer a different vendor"
              help={{
                tip: 'Blind reviewer comes from another vendor than the author.',
                more: 'Only where another vendor is allowed. Needs Blind review on.',
                docs: ROUTING_DOCS,
              }}
              checked={form.blind_review_other_vendor}
              disabled={!form.blind_review}
              onChange={next => patch({ blind_review_other_vendor: next })}
            />
          </DetailCell>
        </FieldRow>
      </Box>
  )
  if (bare) return <AdvancedGroup label="Routing" help={ROUTING_HELP} testId="project-routing">{fields}</AdvancedGroup>
  return (
    <DetailSection title="Routing" help={ROUTING_HELP} divided>
      {fields}
    </DetailSection>
  )
}
