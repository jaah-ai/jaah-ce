import type { JSX } from 'react'
import { Link as RouterLink, useSearchParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import type { Project } from '../../../types'
import {
  branchOutsidePrefix, branchOutsidePrefixWarning, filledRepos, type ProjectFormState, type RepoDraft, repoName, type RepoRowErrors,
} from '../projectForm'
import { DetailCell, DetailSection, SECTION_TITLE_SX } from '../../../ui/detail/DetailPage'
import FieldRow from '../../../ui/FieldRow'
import FormTextField from '../../../ui/FormTextField'
import SelectField from '../../../ui/SelectField'
import SettingCheckbox from '../../../ui/SettingCheckbox'
import { ExplainMoreButton, HelpLabel } from '../../../ui/HelpLabel'
import type { Explain } from '../../../ui/explain'
import { FONT_SIZE, FONT_WEIGHT, grid } from '../../../theme'
import { PROJECT_FIELDS_DOCS, HOW_PROJECTS_WORK_DOCS, type StringFieldKey } from './projectFields'
import { basicPipeline, pipelineTabShown } from './CiPipelineSection'

/* CUSTOMIZE › BRANCHES: each repository's Branch policy (Read only, Branch prefix, Allow force push) and its
 * integration branch, read only here: General's repository editor is that branch's one home (architecture X1).
 * Saved with the project form; read only for a viewer the governance verdict does not admit
 * (`projectForm.ts::governanceIsReadOnly`). */

const BRANCH_POLICY_HELP: Explain = {
  tip: 'How runners may push to each repository.',
  more: 'Each repository has its own Read only, Branch prefix and Allow force push. Read only turns the other two off. '
    + 'Only an owner, or an admin with node-edit permission, can edit them.',
  docs: HOW_PROJECTS_WORK_DOCS,
}

/* Branch policy's repository, in the URL (`?repo=`) so the General tab's shield and a link land on it: a stored
 * row's id, or an unsaved row's draft key. */
export const POLICY_REPO_PARAM = 'repo'
export function policyRepoParam(row: RepoDraft): string {
  return row.id !== undefined ? String(row.id) : row.key
}

/** Whether Branches carries a setting off its default (AC-C2): any repository read only, prefixed or force-pushable. */
export function branchesActive(form: ProjectFormState): boolean {
  const repos = form.repos === null ? [] : filledRepos(form.repos)
  if (repos.length === 0) return form.push_enabled === false || form.branch_prefix.trim() !== ''
  return repos.some(r => r.push_enabled === false || (r.branch_prefix ?? '').trim() !== '' || r.allow_force_push === true)
}

export default function BranchesSection({
  form, project, readOnly, repoErrors, errorFor, patch, patchRepo, onPickRepo,
}: {
  form: ProjectFormState
  project: Project | undefined
  /** The governance verdict withheld (or pending): every control reads only. */
  readOnly: boolean
  repoErrors: RepoRowErrors
  errorFor: (key: StringFieldKey) => string | undefined
  patch: (next: Partial<ProjectFormState>) => void
  patchRepo: (key: string, next: Partial<RepoDraft>) => void
  onPickRepo: (row: RepoDraft) => void
}): JSX.Element {
  const [searchParams] = useSearchParams()
  /* Branch policy's subject: the repository `?repo=` names, else the primary. None (a cached row with no `repos`):
  the project's own prefix and push, as before repositories had their own. */
  const policyRepos = form.repos === null ? [] : filledRepos(form.repos)
  const policyRepo = policyRepos.find(r => policyRepoParam(r) === searchParams.get(POLICY_REPO_PARAM))
    ?? policyRepos.find(r => r.is_primary) ?? policyRepos[0]
  const policyPush = policyRepo ? policyRepo.push_enabled : form.push_enabled
  const policyReadOnly = policyPush === false
  const policyPrefix = policyRepo ? policyRepo.branch_prefix : form.branch_prefix
  /* On a custom pipeline the project's own prefix — a stored primary's — is written from the Pipeline's work stage, so
  here it reads only, with the way there. A basic pipeline has no Pipeline tab: its prefix is set here. An unknown
  mode (a cached row) reads only, as a custom one. Another repository's always stays here. */
  const pipelineOwnsPrefix = project !== undefined && (project.has_repo ?? project.repo_url !== null) && !basicPipeline(project)
    && (policyRepo === undefined || (policyRepo.is_primary && policyRepo.id !== undefined))
  /* Saves, but runners cannot push to that repository's integration branch (`branchOutsidePrefix`): warned on Branch
  prefix, never refused. Only while push is on and force push off: read only pushes nowhere, and Allow force push lets
  the integration branch through whatever the prefix. */
  const outsideBranch = policyPush !== true || policyRepo?.allow_force_push
    ? null
    : branchOutsidePrefix(policyPrefix, policyRepo ? [policyRepo] : form.repos)

  return (
    <Box data-testid="project-branch-policy" sx={{ minWidth: 0 }}>
      {/* Each repository's own. With a choice, the picker names the repository everything under it governs; `?repo=`
      carries it. Read only means no pushes at all, so the prefix and force push go disabled. `align="start"`: an
      error line under one input must not shift the others. */}
      {policyRepos.length > 1 && policyRepo && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', columnGap: 1, minWidth: 0 }}>
          <Typography variant="subtitle2" component="h3" sx={SECTION_TITLE_SX}>
            <HelpLabel label="Branch policy for" help={BRANCH_POLICY_HELP} />
          </Typography>
          <ExplainMoreButton label="Branch policy for" help={BRANCH_POLICY_HELP} />
          <SelectField
            variant="standard"
            id="project-policy-repo"
            value={policyRepoParam(policyRepo)}
            onChange={v => {
              const row = policyRepos.find(r => policyRepoParam(r) === v)
              if (row) onPickRepo(row)
            }}
            ariaLabel="Repository"
            sx={{ mt: 1, mb: 0.5, minWidth: 0, maxWidth: '100%', '& select': { fontWeight: FONT_WEIGHT.semibold, fontSize: FONT_SIZE.base } }}
          >
            {policyRepos.map(r => <option key={r.key} value={policyRepoParam(r)}>{repoName(r)}</option>)}
          </SelectField>
        </Box>
      )}
      <FieldRow align="start" rowGap={2}>
        <DetailCell>
          <SettingCheckbox
            name="project-read-only"
            id="project-read-only"
            label="Read only"
            help={{
              tip: "No pushes or PRs; the clone's push URL is removed.",
              more: 'Runners still work in the repository and report. Branch prefix and Allow force push go off with it.',
              docs: PROJECT_FIELDS_DOCS,
            }}
            /* `checked` + `indeterminate` for three states; an empty box would claim push is on. */
            checked={policyReadOnly}
            indeterminate={policyPush === null}
            disabled={readOnly}
            onChange={next => (policyRepo
              ? patchRepo(policyRepo.key, { push_enabled: !next })
              : patch({ push_enabled: !next }))}
          />
        </DetailCell>

        <DetailCell basis={grid(24)} max={grid(37)}>
          <FormTextField
            id="project-branch-prefix"
            label="Branch prefix"
            placeholder="e.g. agent/"
            help={{
              tip: 'Pushes and PRs only from branches under it. Empty: unrestricted.',
              more: 'Put in front of every branch Jaah creates. Off while Read only is on. An integration branch outside '
                + 'the prefix is warned, not refused: runners then cannot push to it.',
              docs: PROJECT_FIELDS_DOCS,
            }}
            value={policyPrefix}
            onChange={v => (policyRepo ? patchRepo(policyRepo.key, { branch_prefix: v }) : patch({ branch_prefix: v }))}
            readOnly={readOnly || pipelineOwnsPrefix}
            hint={pipelineOwnsPrefix && pipelineTabShown(project)
              ? <RouterLink to="?tab=pipeline" data-testid="project-edit-in-pipeline">Edit in Pipeline</RouterLink>
              : undefined}
            disabled={policyReadOnly}
            error={(policyRepo && repoErrors[policyRepo.key]?.branch_prefix) || errorFor('branch_prefix')}
            warning={outsideBranch ? branchOutsidePrefixWarning(policyPrefix, outsideBranch) : undefined}
          />
        </DetailCell>
      </FieldRow>

      {/* The repository's integration branch, read only (its home is General's repository editor), and whether agents
      may push to it (after asking: the record is the task's Q&A, not a node check). */}
      {policyRepo && (
        <DetailSection
          title={policyRepos.length > 1 ? `Integration branch · ${repoName(policyRepo)}` : 'Integration branch'}
          headingLevel="h3"
          help="Where pull requests land, and whether runners may force-push."
          testId="project-policy-integration"
        >
          <FieldRow align="start" rowGap={2}>
            <DetailCell basis={grid(26.5)} max={grid(42.5)}>
              <FormTextField
                id="project-policy-branch"
                label="Branch"
                help={{ tip: 'Pull requests open against it; set with the repository on General.', docs: PROJECT_FIELDS_DOCS }}
                value={policyRepo.integration_branch}
                onChange={() => undefined}
                readOnly
                hint={<RouterLink to="?tab=general" data-testid="project-policy-branch-edit">Edit</RouterLink>}
                error={repoErrors[policyRepo.key]?.integration_branch}
              />
            </DetailCell>
            <DetailCell>
              <SettingCheckbox
                name="project-allow-force-push"
                id="project-allow-force-push"
                label="Allow force push"
                help={{
                  tip: 'Runners may force-push the integration branch, after asking you.',
                  more: 'A runner asks in the task first; your yes lasts the rest of that task. It also lets the '
                    + 'integration branch through whatever the Branch prefix. Off while Read only is on.',
                  docs: PROJECT_FIELDS_DOCS,
                }}
                checked={policyRepo.allow_force_push}
                disabled={readOnly || policyReadOnly}
                onChange={next => patchRepo(policyRepo.key, { allow_force_push: next })}
              />
            </DetailCell>
          </FieldRow>
        </DetailSection>
      )}
    </Box>
  )
}
