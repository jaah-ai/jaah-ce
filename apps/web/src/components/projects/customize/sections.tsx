import type { ComponentType, ReactNode } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { AUDIT_TAB_PATHS } from '../../../routeGroups'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { isEnabled } from '../../../lib/editionFeatures'
import { editionModule } from '../../../lib/editionModule'
import { ProjectLiveCastTab } from '../../../lib/liveCastSurface'
import { UpgradeToActivate } from '../../../lib/planSwitches'
import type { OrgRef, Project } from '../../../types'
import SettingCheckbox from '../../../ui/SettingCheckbox'
import { grid } from '../../../theme'
import ProjectMcpLinksTab from '../ProjectMcpLinksTab'
import CiPipelineSection from './CiPipelineSection'
import { formHasRepo, type ProjectFormState } from '../projectForm'
import type { TextCell } from './projectFields'

/* THE CUSTOMIZE TAB'S SECTIONS, in order (`customize/CustomizeTab.tsx` draws them; AC-C1): Branches, Testing,
 * Merge lanes, CI Pipeline, MCP, MicroVM, Live Cast. Each def says whether it shows here (its feature in this edition
 * and on hand), whether it is activated (its marker, AC-C2: a checkbox section while ticked, any other while a setting
 * differs from its default — D14), and its body. The Merge lanes and CI Pipeline defs are seams their own children
 * own (`mergeLanesSection`, `ciPipelineSection`). */

export type CustomizeSectionKey = 'branches' | 'testing' | 'merge-lanes' | 'ci-pipeline' | 'mcp' | 'microvm' | 'live-cast'

export type CustomizeSectionDef = {
  key: CustomizeSectionKey
  title: string
  shown: boolean
  active: boolean
  render: () => ReactNode
  /** Its fields save with the project form (the page's Save bar), not on their own. */
  inForm?: boolean
}

const CUSTOMIZE_KEYS: readonly CustomizeSectionKey[] = ['branches', 'testing', 'merge-lanes', 'ci-pipeline', 'mcp', 'microvm', 'live-cast']

/** The URL key naming the section to open on arrival (`?tab=customize&section=testing`). */
export const SECTION_PARAM = 'section'

/** The tabs Customize replaced, each landing on its section (the page's `LEGACY_TABS` sends the tab here). */
export const LEGACY_SECTIONS: Readonly<Record<string, CustomizeSectionKey>> = {
  governance: 'branches', mcp: 'mcp', microvm: 'microvm', 'live-cast': 'live-cast',
}

/** The section holding each form field a refusal may name, so a refused Save opens it. */
export const FIELD_SECTIONS: Readonly<Record<string, CustomizeSectionKey>> = {
  branch_prefix: 'branches',
  push_enabled: 'branches',
  gate_quick_cmd: 'testing',
  stack_up_cmd: 'testing',
  stack_down_cmd: 'testing',
  outcome_scan_interval_minutes: 'testing',
  security_check_patterns: 'testing',
  merge_lane_batch_max: 'merge-lanes',
}

/** The section a URL names: `?section=`, else the retired tab `?tab=` still says. */
export function sectionFromUrl(params: URLSearchParams): CustomizeSectionKey | null {
  const named = params.get(SECTION_PARAM)
  if (named !== null && CUSTOMIZE_KEYS.includes(named as CustomizeSectionKey)) return named as CustomizeSectionKey
  const tab = params.get('tab')
  return tab !== null && Object.hasOwn(LEGACY_SECTIONS, tab) ? LEGACY_SECTIONS[tab] : null
}

/* Loaded where present (`lib/editionModule.ts`): MicroVM's (`microvm`). */
const ProjectMicrovmTab = editionModule(isEnabled('microvm'),
  () => import.meta.glob<ComponentType<{ project: Project; canEdit: boolean }>>(
    '../../microvm/ProjectMicrovmTab.tsx', { eager: true, import: 'default' }))

/** Whether the project's VM settings differ from the org's (AC-C2): its own size, or its own egress edits. */
export function microvmCustomized(project: Project): boolean {
  const own = project.microvm_sizing?.override
  return (own !== undefined && Object.values(own).some(v => v !== null))
    || (project.egress_allow ?? []).length > 0 || (project.egress_deny ?? []).length > 0
}

export type CustomizeContext = {
  project: Project
  form: ProjectFormState
  canEdit: boolean
  /** Another org's project (an operator's): its reads and writes go as that org. */
  otherOrg: OrgRef | null
  textCell: TextCell
  /** Merges a change into the draft (the page's `patch`). */
  patch: (next: Partial<ProjectFormState>) => void
  branches: { active: boolean; render: () => ReactNode }
  testing: { active: boolean; render: () => ReactNode }
  /** The project links an MCP server (its links' read). */
  mcpLinked: boolean
  /** MicroVM is on this org's plan (`planSurfaceShown(admin, 'microvm_allowed')`). */
  microvmOffered: boolean
  /** The viewer may see Live Cast at all (an org admin); `ready` once its read answered, `on` while enabled. */
  liveCast: { shown: boolean; ready: boolean; on: boolean }
}

/* The project's own merge lane switch (seam S-ML), off by default and saved with the form. Off the plan: the box
   disabled and the way to the upgrade. Ticked: Batch size; once saved on, the reason it cannot run here yet (push off,
   forge) or, while it runs, the link to this project's lane in Audit. */
export function mergeLanesSection(ctx: CustomizeContext): CustomizeSectionDef {
  const { project, form } = ctx
  const offered = project.merge_lane_offered === true
  const on = form.merge_lane_enabled === true
  const savedOn = project.merge_lane_enabled === true
  return {
    key: 'merge-lanes',
    title: 'Merge lanes',
    shown: isEnabled('merge_lane') && formHasRepo(form),
    active: on,
    inForm: true,
    render: () => (
      <>
        <SettingCheckbox
          name="merge_lane_enabled" label="Merge lanes" testId="merge-lanes-enabled"
          help="Queued pull requests are tested together and merged by the lane."
          // Saved with the form, so gated as its other fields: the server answers who may write it.
          checked={on} disabled={form.merge_lane_enabled === null || (!offered && !on)}
          onChange={merge_lane_enabled => ctx.patch({ merge_lane_enabled })}
        />
        {project.merge_lane_offered === false && <UpgradeToActivate testId="merge-lanes-upgrade" />}
        {on && savedOn && project.merge_lane_reason && (
          <Typography variant="body2" sx={{ color: 'warning.main' }} data-testid="merge-lanes-reason">{project.merge_lane_reason}</Typography>
        )}
        {on && ctx.textCell({
          name: 'merge_lane_batch_max',
          label: 'Batch size',
          placeholder: '6',
          basis: grid(18.5),
          max: grid(26.5),
          help: 'Pull requests the merge lane tests and merges together.',
          type: 'number',
        })}
        {on && savedOn && project.merge_lane === true && (
          <Typography variant="body2">
            <Link component={RouterLink} to={`${AUDIT_TAB_PATHS.mergeLanes}?project=${project.id}`} data-testid="merge-lanes-audit">Audit</Link>
          </Typography>
        )}
      </>
    ),
  }
}

/* The basic pipeline or the Custom box (`customize/CiPipelineSection.tsx`, seam S-CI): on while the pipeline is
   anything but the default basic, Integration + Release. */
export function ciPipelineSection(ctx: CustomizeContext): CustomizeSectionDef {
  const mode = ctx.project.pipeline_mode
  return {
    key: 'ci-pipeline',
    title: 'CI Pipeline',
    shown: ctx.project.has_repo ?? ctx.project.repo_url !== null,
    active: mode !== undefined && mode !== 'integration_release' && mode !== 'none',
    render: () => <CiPipelineSection projectId={ctx.project.id} canEdit={ctx.canEdit} />,
  }
}

export function customizeSections(ctx: CustomizeContext): CustomizeSectionDef[] {
  const { project, canEdit, otherOrg, liveCast } = ctx
  const hasRepo = formHasRepo(ctx.form)
  return [
    { key: 'branches', title: 'Branches', shown: hasRepo, inForm: true, ...ctx.branches },
    { key: 'testing', title: 'Testing', shown: hasRepo, inForm: true, ...ctx.testing },
    mergeLanesSection(ctx),
    ciPipelineSection(ctx),
    {
      key: 'mcp',
      title: 'MCP',
      shown: true,
      active: ctx.mcpLinked,
      render: () => <ProjectMcpLinksTab projectId={project.id} canEdit={canEdit} />,
    },
    {
      key: 'microvm',
      title: 'MicroVM',
      shown: ProjectMicrovmTab !== undefined && ctx.microvmOffered,
      active: microvmCustomized(project),
      render: () => (ProjectMicrovmTab ? <ProjectMicrovmTab project={project} canEdit={canEdit} /> : null),
    },
    {
      key: 'live-cast',
      title: 'Live Cast',
      // On the holding, shown once its read answered (a refusal leaves it out, as the API's 403 says).
      shown: ProjectLiveCastTab !== null && liveCast.shown && (project.live_cast_offered !== true || liveCast.ready),
      active: liveCast.on,
      /* On the org's holding, today's body (its own box, settings, Save and Publish), once its read answered. Off it,
         the box disabled and the way to the upgrade (S4/S5). */
      render: () => {
        if (project.live_cast_offered !== true) {
          return (
            <>
              <SettingCheckbox name="live-cast-off-plan" label="Live Cast" checked={false} disabled onChange={() => undefined}
                testId="live-cast-off-plan" />
              <UpgradeToActivate testId="live-cast-upgrade" />
            </>
          )
        }
        return liveCast.ready && ProjectLiveCastTab ? <ProjectLiveCastTab projectId={project.id} canEdit org={otherOrg} /> : null
      },
    },
  ]
}
