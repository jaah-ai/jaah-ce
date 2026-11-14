import { type JSX, type ReactNode, useEffect, useState } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { useQuery } from '@tanstack/react-query'
import { apiDetail } from '../../../api'
import {
  ASK_ON_AUTO_MESSAGE, ciWorkflowsKey, type GateDraft, getCiWorkflows, getTicketStates, SEVERITIES, type TicketState,
  type TicketSystem, ticketStatesKey,
} from '../../../lib/pipelineGates'
import { FONT_SIZE, FONT_WEIGHT, grid, RADIUS } from '../../../theme'
import FieldRow from '../../../ui/FieldRow'
import FormTextField from '../../../ui/FormTextField'
import HelpLabel from '../../../ui/HelpLabel'
import SegmentedControl from '../../../ui/SegmentedControl'
import SettingCheckbox from '../../../ui/SettingCheckbox'
import { DetailCell } from '../../../ui/detail/DetailPage'
import { PHONE } from '../../../ui/listStyles'

/* ONE GATE's fields, open under its row in `GateList.tsx`: name, what it checks (only its own kind's fields, §3.1 of
 * the pipeline gates architecture), time limit, retries and Required/Advisory. Every field shows what Save would refuse
 * beside it (`lib/pipelineGates.ts::gateProblems`). The CI picker reads the project's workflows lazily, with free text
 * as the fallback; the ticket state picker reads the system's states and has no free text (AC7.4). */

const FORM_SX = {
  border: '1px solid', borderColor: 'divider', borderRadius: RADIUS.md, p: 1.5, ml: 4, minWidth: 0,
  display: 'flex', flexDirection: 'column', gap: 1.5, bgcolor: 'background.default',
  [PHONE]: { ml: 0 },
} as const
const CELL_SX = { [PHONE]: { flexBasis: '100%', maxWidth: 'none' } } as const
const LABEL_SX = { fontSize: FONT_SIZE.sm, fontWeight: FONT_WEIGHT.medium, color: 'text.secondary', mb: 0.5 } as const
const OTHER = '__other__'

function Labeled({ label, help, error, children }: { label: string; help?: string; error?: string; children: ReactNode }): JSX.Element {
  return (
    <Box sx={{ minWidth: 0 }}>
      <Typography component="div" sx={LABEL_SX}><HelpLabel label={label} help={help} /></Typography>
      {children}
      {error && <Typography variant="body2" color="error" aria-live="polite" sx={{ mt: 0.5 }} data-testid="gate-field-error">{error}</Typography>}
    </Box>
  )
}

/** Minutes or hours, stored in seconds. Text kept as typed so "0" shows its error instead of snapping back. */
function TimeLimit({ id, seconds, onChange, error, disabled }: {
  id: string; seconds: number; onChange: (s: number) => void; error?: string; disabled: boolean
}): JSX.Element {
  const [unit, setUnit] = useState<'min' | 'h'>(() => (seconds >= 3600 && seconds % 3600 === 0 ? 'h' : 'min'))
  const per = unit === 'h' ? 3600 : 60
  const [text, setText] = useState(() => String(seconds / per))
  useEffect(() => {
    if (Math.round(Number(text) * per) !== seconds) setText(String(seconds / per))
  }, [seconds])
  const set = (raw: string, u: 'min' | 'h') => {
    const n = Number(raw)
    onChange(raw.trim() === '' || !Number.isFinite(n) ? 0 : Math.round(n * (u === 'h' ? 3600 : 60)))
  }
  return (
    <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
      <Box sx={{ flex: 1, minWidth: grid(10) }}>
        <FormTextField id={id} label="Time limit" type="number" value={text} error={error} disabled={disabled} testId="gate-timeout"
          help="1 minute to 6 hours. A run that takes longer fails."
          onChange={raw => { setText(raw); set(raw, unit) }} slotProps={{ htmlInput: { min: 1, step: 'any' } }} />
      </Box>
      <FormTextField id={`${id}-unit`} label="Unit" value={unit} disabled={disabled} testId="gate-timeout-unit"
        options={[{ value: 'min', label: 'minutes' }, { value: 'h', label: 'hours' }]}
        onChange={next => {
          const u = next as 'min' | 'h'
          setUnit(u)
          // The limit stays; only how it reads changes ("90" minutes becomes "1.5" hours).
          setText(seconds > 0 ? String(seconds / (u === 'h' ? 3600 : 60)) : text)
        }} sx={{ width: grid(18), flexShrink: 0 }} />
    </Box>
  )
}

export default function GateForm({ gate, problems, serverError, autoStep, busy, projectId, onPatch }: {
  gate: GateDraft
  problems: Record<string, string>
  /** A refused save's message on this gate. */
  serverError?: string
  autoStep: boolean
  busy: boolean
  projectId: number
  onPatch: (next: Partial<GateDraft>) => void
}): JSX.Element {
  const id = (field: string) => `gate-${gate.key}-${field}`
  const c = gate.config
  const setConfig = (next: Record<string, unknown>) => onPatch({ config: { ...c, ...next } })
  const str = (key: string) => (typeof c[key] === 'string' ? (c[key] as string) : '')
  const num = (key: string) => (typeof c[key] === 'number' ? String(c[key]) : str(key))
  const intOr = (raw: string) => (raw.trim() === '' ? NaN : Number(raw))
  return (
    <Box sx={FORM_SX} data-testid="gate-form" role="group" aria-label={`Edit ${gate.name}`}>
      {serverError && <Typography variant="body2" color="error" data-testid="gate-server-error">{serverError}</Typography>}
      <FieldRow rowGap={1.5}>
        <DetailCell basis={grid(30)} max={grid(48)} sx={CELL_SX}>
          <FormTextField id={id('name')} label="Name" value={gate.name} error={problems.name} disabled={busy} testId="gate-name-input"
            onChange={name => onPatch({ name })} slotProps={{ htmlInput: { maxLength: 120 } }} />
        </DetailCell>
      </FieldRow>

      {gate.kind === 'command' && (
        <FormTextField id={id('run')} label="Command" value={str('run')} error={problems.run} disabled={busy} testId="gate-run"
          placeholder="./dc check full" help="Runs in the project's checkout on a node. Exit 0 passes."
          onChange={run => setConfig({ run })} sx={{ '& input': { fontFamily: 'monospace' } }} />
      )}
      {gate.kind === 'ci' && <CiFields gate={gate} problems={problems} busy={busy} projectId={projectId} setConfig={setConfig} id={id} />}
      {gate.kind === 'webhook' && (
        <FieldRow rowGap={1.5}>
          <DetailCell basis={grid(36)} max={grid(60)} sx={CELL_SX}>
            <FormTextField id={id('url')} label="Address" type="url" value={str('url')} error={problems.url} disabled={busy}
              testId="gate-url" placeholder="https://qa.example.com/check"
              help={'It must answer {"result": "pass" or "fail", "report": "…"}.'} onChange={url => setConfig({ url })} />
          </DetailCell>
          <DetailCell basis={grid(28)} max={grid(40)} sx={CELL_SX}>
            <CredentialField id={id('credential')} label="Signing secret" value={gate.credential} error={problems.credential}
              disabled={busy} onChange={credential => onPatch({ credential })} />
          </DetailCell>
        </FieldRow>
      )}
      {gate.kind === 'probe' && (
        <FieldRow rowGap={1.5}>
          <DetailCell basis={grid(20)} max={grid(32)} sx={CELL_SX}>
            <FormTextField id={id('path')} label="Path" value={str('path')} error={problems.path} disabled={busy} testId="gate-path"
              placeholder="/health" help="Added to this stage's address." onChange={path => setConfig({ path })} />
          </DetailCell>
          <DetailCell basis={grid(12)} max={grid(16)} sx={CELL_SX}>
            <FormTextField id={id('min')} label="Status from" type="number" value={num('expect_min')} error={problems.expect}
              disabled={busy} testId="gate-expect-min" onChange={v => setConfig({ expect_min: intOr(v) })} />
          </DetailCell>
          <DetailCell basis={grid(12)} max={grid(16)} sx={CELL_SX}>
            <FormTextField id={id('max')} label="Status to" type="number" value={num('expect_max')} disabled={busy}
              testId="gate-expect-max" onChange={v => setConfig({ expect_max: intOr(v) })} />
          </DetailCell>
          <DetailCell basis={grid(12)} max={grid(16)} sx={CELL_SX}>
            <FormTextField id={id('tries')} label="Tries in a row" type="number" value={num('tries')} error={problems.tries}
              disabled={busy} testId="gate-tries" help="Each 10 s apart; all must pass." onChange={v => setConfig({ tries: intOr(v) })} />
          </DetailCell>
          <DetailCell basis={grid(24)} max={grid(40)} sx={CELL_SX}>
            <FormTextField id={id('contains')} label="Page contains" value={str('contains')} disabled={busy} testId="gate-contains"
              placeholder="Optional" onChange={contains => setConfig({ contains })} />
          </DetailCell>
        </FieldRow>
      )}
      {gate.kind === 'alerts' && (
        <FieldRow rowGap={1.5}>
          <DetailCell basis={grid(24)} max={grid(32)} sx={CELL_SX}>
            <FormTextField id={id('severity')} label="Fail at" value={str('min_severity')} disabled={busy} testId="gate-severity"
              onChange={v => setConfig({ min_severity: v || null })}
              options={[{ value: '', label: 'Any severity' }, ...SEVERITIES.map(s => ({ value: s.value, label: `${s.label} or above` }))]} />
          </DetailCell>
          <DetailCell basis={grid(24)} max={grid(36)} sx={CELL_SX}>
            <FormTextField id={id('environment')} label="Environment" value={str('environment')} disabled={busy} testId="gate-environment"
              placeholder="This stage's" help="Empty: this stage's deploy environment." onChange={environment => setConfig({ environment })} />
          </DetailCell>
        </FieldRow>
      )}
      {gate.kind === 'ticket' && <TicketFields gate={gate} problems={problems} busy={busy} autoStep={autoStep}
        projectId={projectId} setConfig={setConfig} onPatch={onPatch} id={id} />}

      <FieldRow rowGap={1.5}>
        <DetailCell basis={grid(28)} max={grid(36)} sx={CELL_SX}>
          <TimeLimit id={id('timeout')} seconds={gate.timeout_s} error={problems.timeout_s} disabled={busy}
            onChange={timeout_s => onPatch({ timeout_s })} />
        </DetailCell>
        <DetailCell basis={grid(12)} max={grid(16)} sx={CELL_SX}>
          <FormTextField id={id('retries')} label="Retries" type="number" value={Number.isNaN(gate.retries) ? '' : gate.retries}
            error={problems.retries} disabled={busy} testId="gate-retries" help="Runs it again after a failure, up to 3 times."
            onChange={v => onPatch({ retries: intOr(v) })} slotProps={{ htmlInput: { min: 0, max: 3, step: 1 } }} />
        </DetailCell>
        <DetailCell basis={grid(32)} max={grid(44)} sx={CELL_SX}>
          <Labeled label="If it fails">
            <SegmentedControl ariaLabel="If it fails" value={gate.required ? 'required' : 'advisory'} disabled={busy}
              testId="gate-required" onChange={v => onPatch({ required: v === 'required' })}
              options={[
                { value: 'required', label: 'Required', help: 'Stops the step.', testId: 'gate-required-on' },
                { value: 'advisory', label: 'Advisory', help: 'Only warns.', testId: 'gate-advisory' },
              ]} />
          </Labeled>
        </DetailCell>
      </FieldRow>
    </Box>
  )
}

/** A credential named by its saved path (`org/<name>`, `projects/<project>/<name>`): never a value. */
function CredentialField({ id, label, value, error, disabled, onChange, optional = false }: {
  id: string; label: string; value: string | null; error?: string; disabled: boolean; onChange: (v: string | null) => void
  optional?: boolean
}): JSX.Element {
  return (
    <FormTextField id={id} label={label} value={value ?? ''} error={error} disabled={disabled} testId="gate-credential"
      placeholder={optional ? 'Optional' : 'org/name'} help="A saved credential, by its path like org/qa-hook or projects/acme/argocd."
      onChange={v => onChange(v || null)} sx={{ '& input': { fontFamily: 'monospace' } }} />
  )
}

function CiFields({ gate, problems, busy, projectId, setConfig, id }: {
  gate: GateDraft; problems: Record<string, string>; busy: boolean; projectId: number
  setConfig: (next: Record<string, unknown>) => void; id: (field: string) => string
}): JSX.Element {
  const c = gate.config
  const mode = c.all_checks ? 'all' : c.mode === 'start' ? 'start' : 'wait'
  const workflow = typeof c.workflow === 'string' ? c.workflow : ''
  const workflows = useQuery({
    queryKey: ciWorkflowsKey(projectId), queryFn: () => getCiWorkflows(projectId), enabled: mode !== 'all', staleTime: 5 * 60_000, retry: false,
  })
  const listed = workflows.data ?? []
  const known = listed.some(w => w.path === workflow || w.name === workflow)
  const [typing, setTyping] = useState(false)
  const free = listed.length === 0 || typing || (workflow !== '' && !known)
  return (
    <FieldRow rowGap={1.5}>
      <DetailCell basis={grid(40)} max={grid(56)} sx={CELL_SX}>
        <Labeled label="Run">
          <SegmentedControl ariaLabel="CI run" value={mode} disabled={busy} testId="gate-ci-mode"
            onChange={v => setConfig(v === 'all' ? { all_checks: true, mode: 'wait', workflow: null }
              : { all_checks: false, mode: v, workflow })}
            options={[
              { value: 'start', label: 'Start a run', help: 'Jaah starts the workflow on the change.', testId: 'gate-ci-start' },
              { value: 'wait', label: 'Wait for the run', help: 'Waits for the run the forge started.', testId: 'gate-ci-wait' },
              { value: 'all', label: 'All checks', help: 'Waits for every check on the change.', testId: 'gate-ci-all' },
            ]} />
        </Labeled>
      </DetailCell>
      {mode !== 'all' && (
        <DetailCell basis={grid(32)} max={grid(48)} sx={CELL_SX}>
          {free ? (
            <FormTextField id={id('workflow')} label="Workflow or pipeline" value={workflow} error={problems.workflow} disabled={busy}
              testId="gate-workflow" placeholder={workflows.isFetching ? 'Loading…' : 'deploy.yml'}
              hint={listed.length === 0 && !workflows.isFetching ? "Couldn't list them; type its name." : undefined}
              onChange={v => setConfig({ workflow: v })} />
          ) : (
            <FormTextField id={id('workflow')} label="Workflow or pipeline" value={workflow} error={problems.workflow} disabled={busy}
              testId="gate-workflow-pick"
              onChange={v => { if (v === OTHER) { setTyping(true); setConfig({ workflow: '' }) } else setConfig({ workflow: v }) }}
              options={[
                ...(workflow === '' ? [{ value: '', label: 'Pick one' }] : []),
                ...listed.map(w => ({ value: w.path || w.name, label: w.name === w.path || !w.path ? w.name : `${w.name} · ${w.path}` })),
                { value: OTHER, label: 'Other: type its name' },
              ]} />
          )}
        </DetailCell>
      )}
    </FieldRow>
  )
}

function TicketFields({ gate, problems, busy, autoStep, projectId, setConfig, onPatch, id }: {
  gate: GateDraft; problems: Record<string, string>; busy: boolean; autoStep: boolean; projectId: number
  setConfig: (next: Record<string, unknown>) => void; onPatch: (next: Partial<GateDraft>) => void; id: (field: string) => string
}): JSX.Element {
  const c = gate.config
  const system: TicketSystem = c.system === 'servicenow' ? 'servicenow' : 'jira'
  const chosen: TicketState[] = Array.isArray(c.states) ? c.states as TicketState[] : []
  // Keyed on a credential only once it reads as a path, so typing one does not refetch on every key.
  const typed = gate.credential?.trim() || null
  const credential = typed && /^(org|projects)\/.+/.test(typed) ? typed : null
  const states = useQuery({
    queryKey: ticketStatesKey(projectId, system, credential), queryFn: () => getTicketStates(projectId, system, credential),
    staleTime: 5 * 60_000, retry: false,
  })
  const offered = states.data ?? null
  /* The chosen ones first (a stored state the system no longer lists stays visible and can be unticked). */
  const shown = [...chosen, ...(offered ?? []).filter(s => !chosen.some(x => x.id === s.id))]
  const toggle = (s: TicketState, on: boolean) => setConfig({
    states: on ? [...chosen, { id: s.id, name: s.name }] : chosen.filter(x => x.id !== s.id),
  })
  return (
    <>
      <FieldRow rowGap={1.5}>
        <DetailCell basis={grid(16)} max={grid(20)} sx={CELL_SX}>
          <Labeled label="System">
            <Box sx={{ py: 1 }} data-testid="gate-ticket-system">{system === 'jira' ? 'Jira' : 'ServiceNow'}</Box>
          </Labeled>
        </DetailCell>
        <DetailCell basis={grid(40)} max={grid(56)} sx={CELL_SX}>
          <Labeled label="Which ticket" error={problems.source}>
            <SegmentedControl ariaLabel="Which ticket" value={c.source === 'ask' ? 'ask' : 'fixed'} disabled={busy} testId="gate-ticket-source"
              onChange={v => setConfig({ source: v })}
              options={[
                { value: 'ask', label: 'Ask when pressed', help: autoStep ? ASK_ON_AUTO_MESSAGE : 'Whoever presses the step types it.',
                  testId: 'gate-ticket-ask' },
                { value: 'fixed', label: 'Fixed ticket or query', help: 'Passes when a match is in a state below.', testId: 'gate-ticket-fixed' },
              ]} />
          </Labeled>
        </DetailCell>
        <DetailCell basis={grid(28)} max={grid(40)} sx={CELL_SX}>
          <CredentialField id={id('credential')} label="Login" value={gate.credential} error={problems.credential} disabled={busy}
            onChange={v => onPatch({ credential: v })} />
        </DetailCell>
      </FieldRow>
      {c.source !== 'ask' && (
        <FormTextField id={id('query')} label={system === 'jira' ? 'Ticket or JQL' : 'Ticket or encoded query'}
          value={typeof c.query === 'string' ? c.query : ''} error={problems.query} disabled={busy} testId="gate-ticket-query"
          placeholder={system === 'jira' ? 'project = OPS AND fixVersion = "2.14"' : 'number=CHG0031042'}
          onChange={query => setConfig({ query })} sx={{ '& input': { fontFamily: 'monospace' } }} />
      )}
      <Labeled label="Passes in" error={problems.states}>
        {shown.length > 0 && (
          <Box sx={{ display: 'flex', flexWrap: 'wrap', columnGap: 2 }} data-testid="gate-ticket-states">
            {shown.map(s => (
              <SettingCheckbox key={s.id} name={id(`state-${s.id}`)} label={s.name} labelPlacement="end" dense disabled={busy}
                checked={chosen.some(x => x.id === s.id)} onChange={on => toggle(s, on)} testId="gate-ticket-state" />
            ))}
          </Box>
        )}
        {offered === null && !states.isFetching && !states.isError && (
          <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="gate-ticket-states-wait">
            Pick a login to list its states
          </Typography>
        )}
        {states.isError && !states.isFetching && (
          <Typography variant="body2" sx={{ color: 'error.main' }} data-testid="gate-ticket-states-error">
            {apiDetail(states.error, 'Could not list the states')}
          </Typography>
        )}
        {states.isFetching && <Typography variant="body2" sx={{ color: 'text.secondary' }}>Loading states…</Typography>}
      </Labeled>
    </>
  )
}
