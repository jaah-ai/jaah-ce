import { type JSX } from 'react'
import Box from '@mui/material/Box'
import { type Target, TARGET_CHOICES, type TargetKind, targetProblems } from '../../../lib/pipelineGates'
import FormTextField from '../../../ui/FormTextField'

/* A STAGE's deploy target on its card in Project settings › Pipeline (D9, AC9.1): exactly None, Forge deployment
 * environment, Argo CD app, Webhook or Script, each showing only its own fields (§3.2 of the pipeline gates
 * architecture). The Production stage's forge environment also shows the deploy workflows whose success marks the
 * arrival (the project's `deploy_workflows`, their one home; `PipelineSettings.tsx` saves them). The stage's Address
 * stays the card's own field, read-only while a target reports it. */

const MONO_SX = { '& input': { fontFamily: 'monospace' } } as const

export default function TargetFields({ id, target, onChange, environment, workflows, onWorkflows, workflowsError, canEdit, busy, error }: {
  /** The card's stable key, for field ids. */
  id: string
  target: Target
  onChange: (next: Target) => void
  /** The stage's forge deployment environment as stored, shown on a Forge target. */
  environment: string | null
  /** The deploy workflows (one per line), on the Production stage only; undefined elsewhere. */
  workflows?: string
  onWorkflows?: (next: string) => void
  /** The project's refusal of the workflows' own write. */
  workflowsError?: string
  canEdit: boolean
  busy: boolean
  /** A refused save's message on this target. */
  error?: string
}): JSX.Element {
  const c = target.config ?? {}
  const str = (key: string) => (typeof c[key] === 'string' ? (c[key] as string) : '')
  const set = (next: Record<string, unknown>) => onChange({ ...target, config: { ...c, ...next } })
  const problems = canEdit ? targetProblems(target) : {}
  const readOnly = !canEdit
  const fid = (field: string) => `pipeline-${id}-target-${field}`
  const credential = (label: string) => (
    <FormTextField id={fid('credential')} label={label} value={target.credential ?? ''} error={problems.credential}
      readOnly={readOnly} disabled={busy} testId="pipeline-target-credential" placeholder="org/name" sx={MONO_SX}
      help="A saved credential, by its path like org/argocd." onChange={v => onChange({ ...target, credential: v || null })} />
  )
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minWidth: 0 }} data-testid="pipeline-target" data-kind={target.kind}>
      <FormTextField id={fid('kind')} label="Deploy target" value={target.kind} readOnly={readOnly} disabled={busy}
        testId="pipeline-target-kind" error={error}
        onChange={kind => onChange({ kind: kind as TargetKind, config: null, credential: null })}
        options={TARGET_CHOICES.map(t => ({ value: t.value, label: t.label, help: t.help }))} />
      {target.kind === 'forge_env' && (
        <FormTextField id={fid('environment')} label="Environment" value={environment ?? ''} readOnly
          placeholder="Not deployed yet" testId="pipeline-target-environment" onChange={() => {}} />
      )}
      {target.kind === 'forge_env' && workflows !== undefined && (
        <FormTextField id={fid('workflows')} label="Deploy workflows" value={workflows} readOnly={readOnly} disabled={busy}
          error={workflowsError || undefined}
          multiline minRows={1} placeholder="deploy.yml" testId="pipeline-target-workflows" sx={MONO_SX}
          hint={workflows.trim() ? `Arrives when ${workflows.split('\n').map(w => w.trim()).filter(Boolean).join(', ')} succeeds` : undefined}
          help="One a line. Their success on the push marks the change as arrived."
          onChange={v => onWorkflows?.(v)} />
      )}
      {target.kind === 'argocd' && (
        <>
          <FormTextField id={fid('server')} label="Argo CD server" type="url" value={str('server')} error={problems.server}
            readOnly={readOnly} disabled={busy} placeholder="https://…" testId="pipeline-target-server"
            onChange={server => set({ server })} />
          <FormTextField id={fid('app')} label="App" value={str('app')} error={problems.app} readOnly={readOnly} disabled={busy}
            placeholder="web-prod" testId="pipeline-target-app" onChange={app => set({ app })} />
          {credential('Credential')}
        </>
      )}
      {target.kind === 'webhook' && (
        <>
          <FormTextField id={fid('url')} label="Deploy webhook" type="url" value={str('url')} error={problems.url}
            readOnly={readOnly} disabled={busy} placeholder="https://…" testId="pipeline-target-url"
            help={'It must answer {"result": "deployed" or "failed", "url": "https://…"}.'} onChange={url => set({ url })} />
          {credential('Signing secret')}
        </>
      )}
      {target.kind === 'script' && (
        <FormTextField id={fid('run')} label="Command" value={str('run')} error={problems.run} readOnly={readOnly} disabled={busy}
          placeholder="./deploy.sh" testId="pipeline-target-run" sx={MONO_SX}
          help="Print JAAH_URL=https://… to report the address." onChange={run => set({ run })} />
      )}
    </Box>
  )
}
