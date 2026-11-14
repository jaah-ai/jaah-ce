import type { JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import type { Project } from '../../../types'
import { isHidden, type ProjectFormState } from '../projectForm'
import ProjectOutcomeScan from '../ProjectOutcomeScan'
import { DetailCell, DetailField, DetailSection } from '../../../ui/detail/DetailPage'
import FieldRow from '../../../ui/FieldRow'
import { grid } from '../../../theme'
import { basicPipeline, pipelineTabShown } from './CiPipelineSection'
import { PROJECT_FIELDS_DOCS, type StringFieldKey, type TextCell } from './projectFields'

/* CUSTOMIZE › TESTING: the quick gate, the full gate (typed here on a basic pipeline, else the way to the Pipeline: its
 * one control, spec D13), the stack
 * commands, and the outcome scan's interval, security checks and status (D12). Saved with the project form; the gate
 * and stack commands read only with the rest of governance. */

/** Whether Testing carries a setting off its default (AC-C2): a gate or stack command, or an outcome-scan override. */
export function testingActive(form: ProjectFormState): boolean {
  return [form.gate_quick_cmd, form.stack_up_cmd, form.stack_down_cmd, form.outcome_scan_interval_minutes,
    form.security_check_patterns].some(v => v.trim() !== '')
}

export default function TestingSection({
  form, project, readOnly, textCell, ledgerShown, scanStatusShown, scanPlaceholder,
}: {
  form: ProjectFormState
  project: Project | undefined
  readOnly: boolean
  textCell: TextCell
  /** The outcome ledger is on the plan: the interval and security checks show. */
  ledgerShown: boolean
  /** The scan's status line (an Owner/Admin's read). */
  scanStatusShown: boolean
  /** The interval's inherited value, as its placeholder. */
  scanPlaceholder: string
}): JSX.Element {
  /* A gate or stack command: one wide box, empty by default, read only with the rest of governance. */
  const commandCell = (name: StringFieldKey, label: string, tip: string, more: string) => textCell({
    name, label, placeholder: 'None', basis: grid(42.5), max: grid(69), help: { tip, more, docs: PROJECT_FIELDS_DOCS }, readOnly,
  })
  const pipelineShown = project !== undefined && (project.has_repo ?? project.repo_url !== null)
  return (
    <Box data-testid="project-testing" sx={{ minWidth: 0 }}>
      <FieldRow rowGap={2}>
        {commandCell('gate_quick_cmd', 'Quick gate command',
          'Runs on every commit and Stop hook; keep it fast.',
          "Empty: no quick gate. A new project starts with the organization's default (Settings › Projects). "
            + 'It runs as a shell on the node, and anyone who can view the project can read it.')}

        {/* One control for the full gate (spec D13): a custom pipeline's is its Merge step's gate list, set in
        Pipeline; a basic pipeline has no Pipeline tab, so its full gate is typed here. */}
        {!isHidden(form, 'gate_full_cmd') && basicPipeline(project) && commandCell('gate_full_cmd', 'Full gate command',
          'Runs before a change merges; a failing change never reaches the branch.',
          "Empty: no full gate. A new project starts with the organization's default (Settings › Projects). "
            + "It is the Merge step's check; with a custom pipeline it is set on the Pipeline tab.")}
        {!isHidden(form, 'gate_full_cmd') && pipelineShown && pipelineTabShown(project) && (
          <DetailCell basis={grid(42.5)} max={grid(69)}>
            <Box component="dl" sx={{ m: 0 }}>
              <DetailField label="Full gate" testId="project-full-gate">
                Set on the Merge step.{' '}
                <RouterLink to="?tab=pipeline" data-testid="project-full-gate-link">Open Pipeline → Merge gates</RouterLink>
              </DetailField>
            </Box>
          </DetailCell>
        )}
      </FieldRow>

      <FieldRow rowGap={2}>
        {commandCell('stack_up_cmd', 'Stack up command',
          "Starts the slot's stack. No secrets: viewers see it.",
          "Empty: no stack. A new project starts with the organization's default (Settings › Projects). "
            + 'Anyone who can view the project can read it, so never put a password or token here.')}

        {commandCell('stack_down_cmd', 'Stack down command',
          'Stop containers only, keep volumes. No secrets: viewers see it.',
          "Empty: none. It must never delete data. A new project starts with the organization's default.")}
      </FieldRow>

      {/* Any `projects:edit` holder sets the interval; the status is Owner/Admin. */}
      {ledgerShown && (
        <DetailSection
          title="Outcome scan" headingLevel="h3" divided testId="project-outcome-scan-section"
          help={{ tip: 'Finds merged work later reverted or fixed.', docs: PROJECT_FIELDS_DOCS }}
        >
          <FieldRow rowGap={2}>
            {textCell({
              name: 'outcome_scan_interval_minutes',
              id: 'project-outcome-scan-interval',
              basis: grid(26.5),
              max: grid(37),
              label: 'Outcome scan interval',
              placeholder: scanPlaceholder,
              help: {
                tip: 'How often merged work is scanned for reverts and fixes.',
                more: "Minutes, 15–1440. Blank uses the organization's value, or Jaah's default.",
                docs: PROJECT_FIELDS_DOCS,
              },
              type: 'number',
            })}
            {textCell({
              name: 'security_check_patterns',
              label: 'Security checks',
              placeholder: 'e.g. zap, nuclei',
              basis: grid(42.5),
              max: grid(69),
              help: {
                tip: 'CI checks counted as security scans, comma-separated.',
                more: 'CodeQL, Semgrep, Snyk and the usual scanners always count.',
                docs: PROJECT_FIELDS_DOCS,
              },
            })}
          </FieldRow>
          {scanStatusShown && project !== undefined && <ProjectOutcomeScan projectId={project.id} />}
        </DetailSection>
      )}
    </Box>
  )
}
