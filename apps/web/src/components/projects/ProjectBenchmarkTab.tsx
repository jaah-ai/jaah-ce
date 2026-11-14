import type { JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { useProjectTabDraft } from '../../hooks/useProjectTabDraft'
import { moneyText } from '../../lib/billing'
import { grid } from '../../theme'
import type { Project } from '../../types'
import Banner from '../../ui/Banner'
import { DetailCell, DetailSection } from '../../ui/detail/DetailPage'
import FieldRow from '../../ui/FieldRow'
import FormTextField from '../../ui/FormTextField'
import type { Explain } from '../../ui/explain'
import CancelSaveBar from '../../ui/CancelSaveBar'
import { PERCENT_SLOTS } from '../../ui/moneyInput'
import SettingCheckbox from '../../ui/SettingCheckbox'
import { StatusChip } from '../../ui/StatusChip'

/* THE PROJECT PAGE'S BENCHMARK TAB (the outcome ledger's benchmark engine, API side). Drawn only while the org's
 * `benchmark_mode` switch is on (`benchmark_offered`; the edit page omits the tab otherwise). Outside the project
 * form: its own draft and Save of the opt-in and the sample rate (`hooks/useProjectTabDraft`). The budget is the project's Experiment budget (General tab) — the one knob routing shares — so
 * it is shown here, never edited twice: this month's shadow spend against its cap, and "Budget reached" when the
 * sampler has stopped. */

/** The cap is hit: nothing new is sampled this month (`benchmark.Budget.reached`). */
export const BUDGET_REACHED = 'Budget reached'

type Draft = { benchmark_opt_in: boolean; sample_pct: string }

/** A 0..1 share as a percent with at most two decimals (the API keeps four places of the share). */
const pctOf = (share: number) => String(Math.round(share * 10000) / 100)

const draftOf = (p: Project): Draft => ({
  benchmark_opt_in: p.benchmark_opt_in ?? false,
  sample_pct: pctOf(p.benchmark_sample_rate ?? 0.05),
})

const BENCHMARK_HELP: Explain = {
  tip: 'Re-runs sampled merged coder tasks on another configuration.',
  more: 'Each re-run is a shadow: its pull request never merges. Sampling stops for the month once shadow spend '
    + 'reaches the cap below.',
  docs: 'console-reference/directory/projects',
}

/** The sample rate's range, said where it is typed (the API's 422 says the same). */
const RATE_RANGE = 'Set the sample rate between 0% and 100%.'

/** What Save sends: only what changed (Save stays disabled while the typed rate is out of range). */
function bodyOf(draft: Draft, seed: Draft): Record<string, unknown> {
  const body: Record<string, unknown> = {}
  if (draft.benchmark_opt_in !== seed.benchmark_opt_in) body.benchmark_opt_in = draft.benchmark_opt_in
  if (draft.sample_pct !== seed.sample_pct) body.benchmark_sample_rate = Number(draft.sample_pct) / 100
  return body
}

export default function ProjectBenchmarkTab({ project, canEdit }: { project: Project; canEdit: boolean }): JSX.Element {
  const { row, draft, seed, dirty, busy, formError, patch, discard, save } = useProjectTabDraft(project, draftOf)
  const pct = Number(draft.sample_pct)
  const rateError = draft.sample_pct.trim() === '' || Number.isNaN(pct) || pct < 0 || pct > 100
    ? RATE_RANGE : undefined
  const locked = !canEdit || busy

  const spent = row.benchmark_spent_usd
  const cap = row.benchmark_cap_usd
  const share = row.experiment_budget_share ?? 0.1
  return (
    <Box data-testid="project-benchmark-tab" sx={{ maxWidth: grid(128), minWidth: 0 }}>
      {formError && <Banner severity="error" testId="benchmark-error">{formError}</Banner>}
      <DetailSection>
        <SettingCheckbox
          name="benchmark_opt_in"
          label="Benchmark mode"
          help={BENCHMARK_HELP}
          checked={draft.benchmark_opt_in}
          disabled={locked}
          onChange={benchmark_opt_in => patch({ benchmark_opt_in })}
          testId="benchmark-opt-in"
          dense
        />
        <FieldRow>
          <DetailCell basis={grid(26.5)} max={grid(37)}>
            <FormTextField
              id="benchmark-sample-rate"
              label="Sample rate"
              help="Share of merged coder tasks re-run as a shadow."
              type="number"
              slotProps={PERCENT_SLOTS}
              value={draft.sample_pct}
              onChange={sample_pct => patch({ sample_pct })}
              disabled={locked}
              error={rateError}
              testId="benchmark-sample-rate"
            />
          </DetailCell>
        </FieldRow>
      </DetailSection>
      <DetailSection title="Budget" divided testId="benchmark-budget">
        <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1.5, rowGap: 0.5 }}>
          <Typography variant="body2" data-testid="benchmark-spend" sx={{ fontVariantNumeric: 'tabular-nums' }}>
            {/* No agent spend yet this month: a zero cap, which samples nothing — said as that, not "Budget reached". */}
            {cap ? `${moneyText(Math.round((spent ?? 0) * 100), 'usd')} of ${moneyText(Math.round(cap * 100), 'usd')} this month` : 'No agent spend yet this month: nothing sampled'}
          </Typography>
          {!!cap && row.benchmark_budget_reached && (
            <StatusChip label={BUDGET_REACHED} tone="warning" testId="benchmark-budget-reached"
              title="Nothing new is sampled until next month; duels in flight finish." />
          )}
        </Box>
        <Typography variant="body2" sx={{ color: 'text.secondary', mt: 0.5 }}>
          Cap: {pctOf(share)}% of this month’s agent spend, the{' '}
          <Link component={RouterLink} to={`/projects/${project.id}?tab=general`}>Experiment budget</Link>.
        </Typography>
      </DetailSection>
      <Box sx={{ mt: 2 }}>
        <CancelSaveBar plain testId="benchmark-actions" saveTestId="benchmark-save" discardTestId="benchmark-discard" onDiscard={discard}
          dirty={dirty} busy={busy} disabled={locked || rateError !== undefined}
          saveProps={{ onClick: () => void save('Could not save benchmark mode', bodyOf(draft, seed)) }} />
      </Box>
    </Box>
  )
}
