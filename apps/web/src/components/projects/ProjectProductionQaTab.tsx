import type { JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from '../../api'
import { countOf } from '../../lib/humanize'
import { nameSegments } from '../../lib/nameTree'
import { STATUS, WHEN_CHOICE_LABELS } from '../../pages/automations/vocabulary'
import { grid } from '../../theme'
import { useProjectTabDraft } from '../../hooks/useProjectTabDraft'
import { useSubmit } from '../../hooks/useSubmit'
import type { HealthProbe, Project, RevertPolicy, SmokeCommand } from '../../types'
import Banner from '../../ui/Banner'
import { DetailCell, DetailSection } from '../../ui/detail/DetailPage'
import type { Explain } from '../../ui/explain'
import FieldRow from '../../ui/FieldRow'
import FormTextField from '../../ui/FormTextField'
import CancelSaveBar from '../../ui/CancelSaveBar'
import { ExplainMoreButton, HelpLabel } from '../../ui/HelpLabel'
import { monoSx, PHONE, tapTargetSx } from '../../ui/listStyles'
import SettingCheckbox from '../../ui/SettingCheckbox'
import { StatusChip } from '../../ui/StatusChip'
import ProductionQaConnectDeploys from './ProductionQaConnectDeploys'

/* THE PROJECT PAGE'S PRODUCTION QA TAB (`apps/api/app/production_qa/`). Outside the project form: its own draft and
 * Save (`hooks/useProjectTabDraft`), as the Benchmark tab. The opt-in
 * is disabled with its reason while the org's switch is off (`production_qa_offered`; the API refuses it 409 on its
 * own); every other setting saves either way, so a project is ready before the operator turns the org on.
 * Connect releases (`ProductionQaConnectDeploys.tsx`) sits under Releases; the pipeline page's Release step links here
 * while deploys are not connected. Automations lists the incident-kind
 * automations that target this project and creates the defaults (`routes/production_qa.py`). Probes — the health URLs
 * and smoke commands a node runs after each production deploy — ride the same draft and Save: a changed list is sent
 * whole, each existing row with its server `id` (it keys the incident fingerprint), a new row without one. */

/** The org switch is off: the opt-in's reason, shown beside it (the API's 409 says the same). */
export const OFF_FOR_ORG = 'Off for this org'

const REVERT_POLICIES: readonly { value: RevertPolicy; label: string; help: string }[] = [
  { value: 'off', label: 'Off', help: 'Files nothing.' },
  { value: 'draft', label: 'Draft', help: 'Files a draft revert task for someone to start.' },
  { value: 'auto_file', label: 'Auto-file', help: 'Files a revert task that starts on its own.' },
]

const QA_DOCS = 'using-jaah/production-qa/how-production-qa-works'
const SETUP_DOCS = 'using-jaah/production-qa/turn-on-production-qa-for-a-project'

const ENABLED_HELP: Explain = {
  tip: 'Files tasks from incidents and runs probes.',
  more: 'Releases and incidents are recorded either way. Turning it on needs Production QA on for the org; '
    + 'turning it off always works.',
  docs: QA_DOCS,
}
const REVERT_HELP: Explain = {
  tip: 'What a release that causes a regression files.',
  more: 'A revert is an ordinary task: its pull request goes through the merge lane like any other.',
  docs: QA_DOCS,
}
const WINDOW_HELP: Explain = {
  tip: 'Minutes measured after a release, against as long before.',
  more: 'The baseline is the same length just before the release started. A later release inside the window cuts it '
    + 'short. 5 to 1440.',
  docs: QA_DOCS,
}
const MIN_EVENTS_HELP: Explain = {
  tip: 'An error-rate breach needs at least this many errors.',
  more: 'Counted in the window after the release. It does not gate the p95 latency check. 1 to 1,000,000.',
}
const ERROR_RATIO_HELP: Explain = {
  tip: 'Error rate this many times the baseline is a regression.',
  more: 'Error rate is errors over requests, from the project\'s monitors. 1.01 to 100.',
  docs: QA_DOCS,
}
const P95_RATIO_HELP: Explain = {
  tip: 'p95 latency this many times the baseline is a regression.',
  more: 'Measured only where a monitor reports latency. 1.01 to 100.',
  docs: QA_DOCS,
}
const PROBES_HELP: Explain = {
  tip: 'Checks a node runs after each successful production release.',
  more: 'A failing probe records a health incident; the next pass resolves it. Probes run only while Production QA '
    + 'is on.',
  docs: SETUP_DOCS,
}
const HEALTH_HELP: Explain = {
  tip: 'One GET each; another status records a health incident.',
  more: 'https only, no credentials in the URL, up to 10. A redirect to another host or to http fails.',
}
const SMOKE_HELP: Explain = {
  tip: 'Commands run sandboxed; a failure records a health incident.',
  more: 'Each runs in the project\'s checkout on the node, under the merge lane\'s sandbox: the daemon socket '
    + 'hidden, git read-only, secret-shaped environment dropped. Up to 5.',
}

type Draft = {
  production_qa_enabled: boolean
  /** One workflow path per line. */
  deploy_workflows: string
  revert_policy: RevertPolicy
  regression_window_min: string
  regression_error_ratio: string
  regression_min_events: string
  regression_p95_ratio: string
  health_probes: HealthRow[]
  smoke_commands: SmokeRow[]
}

/** A probe row as edited: numbers as typed, `key` the React key (the server id, or a local one for a new row). */
type HealthRow = { key: string; id?: string; name: string; url: string; expect_status: string }
type SmokeRow = { key: string; id?: string; name: string; command: string; timeout_s: string }

const HEALTH_CAP = 10
const SMOKE_CAP = 5
let nextKey = 0
const newKey = () => `new-${++nextKey}`

/* A stored row's key is its id (by position if a server ever sends none): it must not change across renders, or the
   seed would never settle. */
const healthRowOf = (p: HealthProbe, i: number): HealthRow => ({
  key: p.id ?? `row-${i}`, id: p.id, name: p.name, url: p.url, expect_status: String(p.expect_status),
})
const smokeRowOf = (c: SmokeCommand, i: number): SmokeRow => ({
  key: c.id ?? `row-${i}`, id: c.id, name: c.name, command: c.command, timeout_s: String(c.timeout_s),
})
/** A typed number, or the server's default when left blank; anything else is sent as typed, so the server's 422 names
 *  it rather than a silent default. */
const numberOr = (typed: string, fallback: number): number | string => {
  if (typed.trim() === '') return fallback
  const n = Number(typed)
  return Number.isNaN(n) ? typed : n
}
const withId = (id: string | undefined) => (id ? { id } : {})

function draftOf(p: Project): Draft {
  return {
    production_qa_enabled: p.production_qa_enabled ?? false,
    deploy_workflows: (p.deploy_workflows ?? []).join('\n'),
    revert_policy: p.revert_policy ?? 'off',
    regression_window_min: String(p.regression_window_min ?? 60),
    regression_error_ratio: String(p.regression_error_ratio ?? 1.5),
    regression_min_events: String(p.regression_min_events ?? 20),
    regression_p95_ratio: String(p.regression_p95_ratio ?? 1.3),
    health_probes: (p.health_probes ?? []).map(healthRowOf),
    smoke_commands: (p.smoke_commands ?? []).map(smokeRowOf),
  }
}

/** What Save sends: only the fields that changed (an unchanged opt-in is never resent). */
function bodyOf(draft: Draft, seed: Draft): Record<string, unknown> {
  const body: Record<string, unknown> = {}
  const changed = (key: keyof Draft) => JSON.stringify(draft[key]) !== JSON.stringify(seed[key])
  if (changed('production_qa_enabled')) body.production_qa_enabled = draft.production_qa_enabled
  if (changed('deploy_workflows')) {
    body.deploy_workflows = draft.deploy_workflows.split('\n').map(line => line.trim()).filter(Boolean)
  }
  if (changed('revert_policy')) body.revert_policy = draft.revert_policy
  for (const key of ['regression_window_min', 'regression_error_ratio', 'regression_min_events', 'regression_p95_ratio'] as const) {
    if (changed(key)) body[key] = Number(draft[key])
  }
  if (changed('health_probes')) {
    body.health_probes = draft.health_probes.map(r => ({
      ...withId(r.id), name: r.name.trim(), url: r.url.trim(), expect_status: numberOr(r.expect_status, 200),
    }))
  }
  if (changed('smoke_commands')) {
    body.smoke_commands = draft.smoke_commands.map(r => ({
      ...withId(r.id), name: r.name.trim(), command: r.command.trim(), timeout_s: numberOr(r.timeout_s, 60),
    }))
  }
  return body
}

/** A probe row's fields stack at phone width (each cell a full-width line: a row that wraps at 100% basis, since a
 *  column would read each cell's basis as a height); a wide screen wraps by basis. Rows after the first are set off
 *  by a rule, so a stacked row reads as one. */
const PROBE_ROW_SX = {
  '& + &': { borderTop: '1px solid', borderColor: 'divider', pt: 1 },
  [PHONE]: { flexWrap: 'wrap', alignItems: 'stretch', '&& > *': { flex: '1 1 100%', maxWidth: 'none' } },
} as const
const MONO_INPUT = { htmlInput: { spellCheck: false, sx: monoSx } }

/** One probe list: its rows, Remove on each, and Add until the cap. A viewer without edit rights sees the rows only. */
function ProbeList<R extends { key: string; name: string }>({
  title, help, noun, rows, cap, addLabel, canEdit, locked, blank, onChange, testId, fields,
}: {
  title: string
  help: Explain
  noun: string
  rows: R[]
  cap: number
  addLabel: string
  canEdit: boolean
  locked: boolean
  blank: () => R
  onChange: (rows: R[]) => void
  testId: string
  fields: (row: R, set: (next: Partial<R>) => void) => JSX.Element
}): JSX.Element {
  const set = (key: string) => (next: Partial<R>) => onChange(rows.map(r => (r.key === key ? { ...r, ...next } : r)))
  return (
    <Stack spacing={1} data-testid={testId}>
      {/* The title and its Add share one line: a list that is usually empty costs one row. */}
      <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1.5 }}>
        <Typography variant="subtitle2" component="h3"><HelpLabel label={title} help={help} /></Typography>
        <ExplainMoreButton label={title} help={help} />
        {canEdit && (
          <Button size="small" disabled={locked || rows.length >= cap} onClick={() => onChange([...rows, blank()])}
            data-testid={`${testId}-add`} sx={tapTargetSx}>
            {addLabel}
          </Button>
        )}
      </Box>
      {rows.length === 0 && !canEdit && <Typography variant="body2" sx={{ color: 'text.secondary' }}>None</Typography>}
      {rows.map(row => (
        <FieldRow key={row.key} align="center" testId={`${testId}-row`} sx={PROBE_ROW_SX}>
          {fields(row, set(row.key))}
          {canEdit && (
            <Button color="error" disabled={locked} onClick={() => onChange(rows.filter(r => r.key !== row.key))}
              aria-label={`Remove ${row.name.trim() || noun}`} data-testid={`${testId}-remove`}
              sx={[tapTargetSx, { alignSelf: 'center', [PHONE]: { alignSelf: 'flex-start' } }]}>
              Remove
            </Button>
          )}
        </FieldRow>
      ))}
    </Stack>
  )
}

type QaAutomations = {
  /** `foreign_owner`: owned from another org, so every run is refused until a member takes ownership. */
  items: { id: string; name: string; source_kind: string; status: string; foreign_owner?: boolean }[]
  total: number
}

const automationsKeyOf = (projectId: number) => ['production-qa-automations', projectId] as const

/** The automations on the incident kinds that file in this project; with none, one button creates the defaults. */
function ProductionQaAutomations({ projectId, canEdit }: { projectId: number; canEdit: boolean }): JSX.Element {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: automationsKeyOf(projectId),
    queryFn: () => get<QaAutomations>(`/api/projects/${projectId}/production-qa/automations`),
    retry: (count, error) => !(error instanceof ApiError && error.status === 403) && count < 2,
  })
  const { busy, formError, run } = useSubmit()
  const create = () => run('Could not create the default automations', async () => {
    const next = await post<QaAutomations>(`/api/projects/${projectId}/production-qa/default-automations`)
    queryClient.setQueryData(automationsKeyOf(projectId), next)
    void queryClient.invalidateQueries({ queryKey: automationsKeyOf(projectId) })
  })
  const takeOwnership = (id: string) => run('Could not take ownership', async () => {
    await post(`/api/automations/${id}/take-ownership`)
    await queryClient.invalidateQueries({ queryKey: automationsKeyOf(projectId) })
  })
  const denied = query.error instanceof ApiError && query.error.status === 403

  return (
    <DetailSection title="Automations" testId="production-qa-automations" actions={query.data?.total === 0 && (
      <Button size="small" variant="outlined" disabled={!canEdit || busy} onClick={() => void create()}
        data-testid="production-qa-create-automations" sx={tapTargetSx}>
        Create defaults
      </Button>
    )}>
      {denied ? (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>Org Admins manage these automations.</Typography>
      ) : query.isError ? (
        <Banner severity="error" testId="production-qa-automations-error">Could not load the automations.</Banner>
      ) : query.data ? (
        <Stack spacing={1.5}>
          <Typography variant="body2" data-testid="production-qa-automations-count">
            {countOf(query.data.total, 'automation')}
          </Typography>
          {query.data.items.length > 0 && (
            <Stack component="ul" spacing={0.5} sx={{ m: 0, p: 0, listStyle: 'none' }}>
              {query.data.items.map(item => (
                <Box component="li" key={item.id} data-testid={`production-qa-automation-${item.id}`}
                  sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1.5, rowGap: 0.5, minWidth: 0 }}>
                  <Link component={RouterLink} to={`/automations/${item.id}`} variant="body2" sx={{ overflowWrap: 'anywhere' }}>
                    {/* The leaf only: the folder path (`Production QA/…`) files it in the Automations tree. */}
                    {nameSegments(item.name).at(-1) ?? item.name}
                  </Link>
                  <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                    {WHEN_CHOICE_LABELS[item.source_kind as keyof typeof WHEN_CHOICE_LABELS] ?? item.source_kind}
                  </Typography>
                  <StatusChip label={STATUS[item.status]?.label ?? item.status} tone={STATUS[item.status]?.tone ?? 'neutral'}
                    glyph={STATUS[item.status]?.glyph} data-status={item.status} />
                  {item.foreign_owner && (
                    <>
                      <Typography variant="body2" sx={{ color: 'text.secondary' }}>Owner no longer in this org</Typography>
                      <Button size="small" disabled={!canEdit || busy} onClick={() => void takeOwnership(item.id)}
                        data-testid="production-qa-take-ownership" sx={tapTargetSx}>
                        Take ownership
                      </Button>
                    </>
                  )}
                </Box>
              ))}
            </Stack>
          )}
          {formError && <Banner severity="error" testId="production-qa-automations-create-error">{formError}</Banner>}
        </Stack>
      ) : null}
    </DetailSection>
  )
}

export default function ProjectProductionQaTab({ project, canEdit }: {
  project: Project
  canEdit: boolean
}): JSX.Element {
  const { row, draft, seed, dirty, busy, formError, patch, discard, save } = useProjectTabDraft(project, draftOf)
  const offered = row.production_qa_offered === true
  const locked = !canEdit || busy

  return (
    <Box data-testid="project-production-qa-tab" sx={{ maxWidth: grid(170.5), minWidth: 0 }}>
      {formError && <Banner severity="error" testId="production-qa-error">{formError}</Banner>}
      <DetailSection>
        <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1.5 }}>
          <SettingCheckbox
            name="production_qa_enabled"
            label="Production QA"
            help={ENABLED_HELP}
            checked={draft.production_qa_enabled}
            // Turning it off always works; turning it on needs the org's switch.
            disabled={locked || (!offered && !draft.production_qa_enabled)}
            onChange={production_qa_enabled => patch({ production_qa_enabled })}
            testId="production-qa-enabled"
            dense
          />
          {!offered && (
            <Typography variant="body2" data-testid="production-qa-off-for-org" sx={{ color: 'text.secondary' }}>
              {OFF_FOR_ORG}
            </Typography>
          )}
        </Box>
      </DetailSection>
      <DetailSection title="Releases" divided>
        <FieldRow>
          {/* GitHub Actions workflow files: a Bitbucket project names its production branch in Connect releases. */}
          {project.forge !== 'bitbucket' && <DetailCell basis={grid(48)}>
            <FormTextField
              id="production-qa-deploy-workflows"
              label={project.forge === 'azure_devops' ? 'Release pipelines' : 'Release workflows'}
              help={project.forge === 'azure_devops'
                ? 'Runs of these pipelines or Azure releases count as releases.'
                : 'Runs of these workflow files count as releases.'}
              placeholder={project.forge === 'azure_devops' ? 'Deploy' : 'deploy.yml'}
              multiline
              minRows={2}
              value={draft.deploy_workflows}
              onChange={v => patch({ deploy_workflows: v })}
              disabled={locked}
              testId="production-qa-deploy-workflows"
            />
          </DetailCell>}
          <DetailCell basis={grid(32)} max={grid(42.5)}>
            <FormTextField
              id="production-qa-revert-policy"
              label="Revert policy"
              help={REVERT_HELP}
              value={draft.revert_policy}
              onChange={v => patch({ revert_policy: v as RevertPolicy })}
              options={REVERT_POLICIES}
              slotProps={{ inputLabel: { shrink: true } }}
              disabled={locked}
              testId="production-qa-revert-policy"
            />
          </DetailCell>
        </FieldRow>
      </DetailSection>
      <ProductionQaConnectDeploys projectId={project.id} canEdit={canEdit} />
      <DetailSection title="Regression" divided>
        <FieldRow>
          <DetailCell basis={grid(24)} max={grid(32)}>
            <FormTextField id="production-qa-window" label="Window (min)" help={WINDOW_HELP}
              type="number" value={draft.regression_window_min} disabled={locked}
              onChange={v => patch({ regression_window_min: v })} testId="production-qa-window" />
          </DetailCell>
          <DetailCell basis={grid(24)} max={grid(32)}>
            <FormTextField id="production-qa-min-events" label="Min events" help={MIN_EVENTS_HELP}
              type="number" value={draft.regression_min_events} disabled={locked}
              onChange={v => patch({ regression_min_events: v })} testId="production-qa-min-events" />
          </DetailCell>
          <DetailCell basis={grid(24)} max={grid(32)}>
            <FormTextField id="production-qa-error-ratio" label="Error rate ×" help={ERROR_RATIO_HELP}
              type="number" value={draft.regression_error_ratio} disabled={locked}
              onChange={v => patch({ regression_error_ratio: v })} testId="production-qa-error-ratio" />
          </DetailCell>
          <DetailCell basis={grid(24)} max={grid(32)}>
            <FormTextField id="production-qa-p95-ratio" label="p95 latency ×" help={P95_RATIO_HELP}
              type="number" value={draft.regression_p95_ratio} disabled={locked}
              onChange={v => patch({ regression_p95_ratio: v })} testId="production-qa-p95-ratio" />
          </DetailCell>
        </FieldRow>
      </DetailSection>
      <DetailSection title="Probes" help={PROBES_HELP}
        divided testId="production-qa-slot-probes">
        {/* The two lists side by side on a wide screen (each wraps its own rows), stacked on a narrow one. */}
        <FieldRow columnGap={grid(5)} rowGap={2}>
          <DetailCell basis={grid(58.5)}>
            <ProbeList<HealthRow>
              title="Health URLs" help={HEALTH_HELP} noun="URL" addLabel="Add URL" cap={HEALTH_CAP} testId="production-qa-health"
              rows={draft.health_probes} canEdit={canEdit} locked={locked}
              blank={() => ({ key: newKey(), name: '', url: '', expect_status: '200' })}
              onChange={health_probes => patch({ health_probes })}
              fields={(r, set) => (
                <>
                  <DetailCell basis={grid(24)} max={grid(37)}>
                    <FormTextField id={`production-qa-health-${r.key}-name`} label="Name"
                      help="Names its results; blank uses the URL's host." placeholder="Home" value={r.name}
                      onChange={name => set({ name })} disabled={locked} testId="production-qa-health-name" />
                  </DetailCell>
                  <DetailCell basis={grid(42.5)}>
                    <FormTextField id={`production-qa-health-${r.key}-url`} label="URL" type="url"
                      help="Fetched with one GET; https only, no credentials."
                      placeholder="https://example.com/health" value={r.url} onChange={url => set({ url })} disabled={locked}
                      slotProps={MONO_INPUT} testId="production-qa-health-url" />
                  </DetailCell>
                  <DetailCell basis={grid(18.5)} max={grid(24)}>
                    <FormTextField id={`production-qa-health-${r.key}-status`} label="Expected status" type="number"
                      help="Any other response status fails the probe."
                      value={r.expect_status} onChange={expect_status => set({ expect_status })} disabled={locked}
                      slotProps={{ htmlInput: { min: 100, max: 599 } }} testId="production-qa-health-status" />
                  </DetailCell>
                </>
              )}
            />
          </DetailCell>
          <DetailCell basis={grid(58.5)}>
            <ProbeList<SmokeRow>
              title="Smoke commands" help={SMOKE_HELP} noun="command" addLabel="Add command" cap={SMOKE_CAP} testId="production-qa-smoke"
              rows={draft.smoke_commands} canEdit={canEdit} locked={locked}
              blank={() => ({ key: newKey(), name: '', command: '', timeout_s: '60' })}
              onChange={smoke_commands => patch({ smoke_commands })}
              fields={(r, set) => (
                <>
                  <DetailCell basis={grid(24)} max={grid(37)}>
                    <FormTextField id={`production-qa-smoke-${r.key}-name`} label="Name"
                      help="Names its results; blank uses the command's first word." placeholder="Checkout" value={r.name}
                      onChange={name => set({ name })} disabled={locked} testId="production-qa-smoke-name" />
                  </DetailCell>
                  <DetailCell basis={grid(42.5)}>
                    <FormTextField id={`production-qa-smoke-${r.key}-command`} label="Command" placeholder="./scripts/smoke.sh"
                      help="Runs with bash in the checkout; nonzero exit fails it."
                      value={r.command} onChange={command => set({ command })} disabled={locked}
                      slotProps={MONO_INPUT} testId="production-qa-smoke-command" />
                  </DetailCell>
                  <DetailCell basis={grid(18.5)} max={grid(24)}>
                    <FormTextField id={`production-qa-smoke-${r.key}-timeout`} label="Timeout (s)" type="number"
                      help="Still running after this many seconds fails the probe."
                      value={r.timeout_s} onChange={timeout_s => set({ timeout_s })} disabled={locked}
                      slotProps={{ htmlInput: { min: 1, max: 300 } }} testId="production-qa-smoke-timeout" />
                  </DetailCell>
                </>
              )}
            />
          </DetailCell>
        </FieldRow>
      </DetailSection>
      <Box sx={{ mt: 2 }}>
        <CancelSaveBar plain testId="production-qa-actions" saveTestId="production-qa-save" discardTestId="production-qa-discard"
          onDiscard={discard} dirty={dirty} busy={busy} disabled={locked}
          saveProps={{ onClick: () => void save('Could not save Production QA', bodyOf(draft, seed)) }} />
      </Box>
      {/* Automations: its state and one action, under one rule. */}
      <FieldRow columnGap={grid(5)} rowGap={2}
        sx={{ mt: 2, pt: 1.5, borderTop: '1px solid', borderColor: 'divider' }}>
        <DetailCell basis={grid(48)}>
          <ProductionQaAutomations projectId={project.id} canEdit={canEdit} />
        </DetailCell>
      </FieldRow>
    </Box>
  )
}
