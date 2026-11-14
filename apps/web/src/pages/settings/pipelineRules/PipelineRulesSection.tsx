import { useEffect, useState, type JSX } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import { apiDetail } from '../../../api'
import { sameJson as same } from '../../../hooks/useServerDraft'
import { useSubmit } from '../../../hooks/useSubmit'
import { APPROVALS_MAX } from '../../../lib/pipeline'
import { FONT_WEIGHT } from '../../../theme'
import Banner from '../../../ui/Banner'
import CancelSaveBar from '../../../ui/CancelSaveBar'
import CellLabel from '../../../ui/CellLabel'
import DataTable from '../../../ui/DataTable'
import type { Explain } from '../../../ui/explain'
import HelpLabel from '../../../ui/HelpLabel'
import LoadingState from '../../../ui/LoadingState'
import { NoneMark } from '../../../ui/NoneMark'
import SelectField from '../../../ui/SelectField'
import Tip from '../../../ui/Tip'
import { PHONE, phoneCardSx, phoneColumnOrder, PIPELINE_RULES_CONTAINER, PIPELINE_RULES_NARROW } from '../../../ui/listStyles'
import { getGuardrails, GUARDRAILS_KEY, putGuardrail, type Guardrail, type GuardrailMode, type GuardrailRule } from './api'

/* Settings › Projects › Pipeline rules (`./api.ts`): the org's three built-in checks on the step into production, one
 * row each — the rule (what it asks on hover), its mode (Off / Warn / Block; the approval rule also its count) and the
 * projects whose saved pipeline breaks it today (their names on hover or tap). One Save bar; saving touches no saved
 * pipeline. Org Owner/Admin only: Settings shows the panel to no one else, and the API refuses the write. */

const DOCS = 'using-jaah/projects/set-org-pipeline-rules'

const MODES: readonly { value: GuardrailMode; label: string }[] = [
  { value: 'off', label: 'Off' }, { value: 'warn', label: 'Warn' }, { value: 'block', label: 'Block' },
]
const MODE_CHOICES = [
  { label: 'Off', help: 'Not checked.' },
  { label: 'Warn', help: 'Saves, and shows the warning on the step.' },
  { label: 'Block', help: 'Refuses the save until the step complies.' },
]

const HELP: Record<GuardrailRule, Explain> = {
  production_approval: {
    tip: 'The step into production needs approvals.',
    more: 'Whoever asked for a release can never approve it.',
    docs: DOCS,
  },
  no_auto_production: { tip: 'The step into production never runs by itself.', docs: DOCS },
  production_maintainers: {
    tip: 'Only maintainers and above may press the step into production.',
    more: 'Org Owners, Org Admins, Maintainers and named people pass; Developer and Viewer do not.',
    docs: DOCS,
  },
}

type Draft = Record<string, { mode: GuardrailMode; approvals_min: number | null }>
const draftOf = (rules: Guardrail[]): Draft =>
  Object.fromEntries(rules.map(r => [r.rule, { mode: r.mode, approvals_min: r.approvals_min }]))

/* A select never narrower than its longest option: squeezed (a side panel narrowing the column), the approvals
   count wraps under the mode instead of clipping to "1 ap". */
const FULL_LABEL_SX = { minWidth: 'max-content' } as const

/* On a phone, or where the table's own width is narrow (a side panel open beside Settings), each rule is one card:
   the rule, then its mode, then what breaks it — each value beside its heading. */
const CARD_SX = phoneCardSx(phoneColumnOrder([
  ['rule', { flex: '1 1 100%', display: 'block', pt: 1 }],
  'mode',
  ['breaking', { flex: '1 1 100%', pb: 1 }],
]), { rowGap: 1 })
const TABLE_SX = [
  { containerType: 'inline-size', containerName: PIPELINE_RULES_CONTAINER, minWidth: 0 },
  CARD_SX,
  { [PIPELINE_RULES_NARROW]: { ...CARD_SX[PHONE], '& [data-cell-label]': { display: 'inline' } } },
] as const

export default function PipelineRulesSection(): JSX.Element {
  const query = useQuery({ queryKey: GUARDRAILS_KEY, queryFn: getGuardrails })
  if (query.isPending) return <LoadingState label="Loading pipeline rules" />
  if (query.isError) return <Banner severity="error">{apiDetail(query.error, 'Could not load pipeline rules')}</Banner>
  return <Editor rules={query.data?.rules ?? []} />
}

function Editor({ rules }: { rules: Guardrail[] }): JSX.Element {
  const queryClient = useQueryClient()
  const [base, setBase] = useState(() => draftOf(rules))
  const [draft, setDraft] = useState(base)
  const { busy, formError, run, saved } = useSubmit()
  const dirty = !same(draft, base)
  // A fresh read reseeds a clean draft only.
  useEffect(() => {
    const next = draftOf(rules)
    setBase(prev => (same(prev, next) ? prev : next))
    setDraft(prev => (same(prev, base) ? next : prev))
  }, [rules])

  const patch = (rule: string, change: Partial<Draft[string]>) => setDraft(d => ({ ...d, [rule]: { ...d[rule], ...change } }))
  /* One PUT per changed rule; each answer lands at once, so a later refusal leaves the earlier rules saved and shown
     saved, and only the unsaved ones still dirty. */
  const save = () => void run('Could not save the pipeline rules', async () => {
    for (const r of rules) {
      const want = draft[r.rule]
      if (same(want, base[r.rule])) continue
      const fresh = await putGuardrail(r.rule, {
        mode: want.mode, ...(r.rule === 'production_approval' && want.approvals_min ? { approvals_min: want.approvals_min } : {}),
      })
      const landed = draftOf(fresh.rules)[r.rule]
      setBase(b => ({ ...b, [r.rule]: landed }))
      setDraft(d => ({ ...d, [r.rule]: landed }))
      queryClient.setQueryData(GUARDRAILS_KEY, fresh)
    }
  })

  return (
    <Stack spacing={1.5} data-testid="pipeline-rules">
      <Box sx={TABLE_SX}>
        <DataTable ariaLabel="Pipeline rules" nested testId="pipeline-rules-table">
          <TableHead>
            <TableRow>
              <TableCell>Rule</TableCell>
              <TableCell>Mode</TableCell>
              <TableCell>Projects breaking it</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rules.map(r => {
              const want = draft[r.rule] ?? { mode: r.mode, approvals_min: r.approvals_min }
              const count = r.breaking.length
              return (
                <TableRow key={r.rule} data-testid={`pipeline-rule-${r.rule}`}>
                  <TableCell data-col="rule">
                    <Box component="span" sx={{ fontWeight: FONT_WEIGHT.semibold }} data-testid="pipeline-rule-title">
                      <HelpLabel label={r.title} help={HELP[r.rule]} choices={MODE_CHOICES} />
                    </Box>
                  </TableCell>
                  <TableCell data-col="mode">
                    <CellLabel>Mode</CellLabel>
                    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center' }}>
                      <SelectField id={`pipeline-rule-${r.rule}-mode`} ariaLabel={`${r.title}: mode`} value={want.mode} dense
                        sx={FULL_LABEL_SX} testId="pipeline-rule-mode" disabled={busy} options={MODES}
                        onChange={mode => patch(r.rule, { mode: mode as GuardrailMode })} />
                      {r.rule === 'production_approval' && (
                        <SelectField id={`pipeline-rule-${r.rule}-approvals`} ariaLabel="Approvals needed" dense
                          value={String(want.approvals_min ?? 1)} testId="pipeline-rule-approvals" disabled={busy}
                          sx={FULL_LABEL_SX}
                          options={Array.from({ length: APPROVALS_MAX }, (_, i) => ({
                            value: String(i + 1), label: `${i + 1} approval${i ? 's' : ''}`,
                          }))}
                          onChange={n => patch(r.rule, { approvals_min: Number(n) })} />
                      )}
                    </Box>
                  </TableCell>
                  <TableCell data-col="breaking">
                    <CellLabel>Projects breaking it</CellLabel>
                    {count === 0 ? <NoneMark /> : (
                      <Tip title={r.breaking.map(p => p.name).join(', ')}>
                        <Box component="span" tabIndex={0} data-testid="pipeline-rule-breaking"
                          sx={{ textDecoration: 'underline dotted', cursor: 'help' }}>
                          {count} project{count === 1 ? '' : 's'}
                        </Box>
                      </Tip>
                    )}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </DataTable>
      </Box>
      {formError && <Banner severity="error" testId="pipeline-rules-error">{formError}</Banner>}
      <Box>
        <CancelSaveBar plain testId="pipeline-rules-actions" saveTestId="pipeline-rules-save" dirty={dirty} busy={busy}
          saved={saved && !dirty} onDiscard={() => setDraft(base)} saveProps={{ onClick: save }} />
      </Box>
    </Stack>
  )
}
