import { type JSX, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import DialogActions from '@mui/material/DialogActions'
import DialogContent from '@mui/material/DialogContent'
import Radio from '@mui/material/Radio'
import RadioGroup from '@mui/material/RadioGroup'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../api'
import { useSubmit } from '../../hooks/useSubmit'
import {
  changeText, chainsKey, type DraftSource, getPipelineChains, getPresets, getTemplatePreview, linkTemplate, type Pipeline,
  type PipelineChange, type PipelineDraft, postPipelineDraft, presetsKey, roleLabel,
} from '../../lib/pipeline'
import { listTemplates, TEMPLATES_KEY } from '../../pages/pipelineTemplates/api'
import { FONT_SIZE, FONT_WEIGHT, grid, RADIUS, SIZE } from '../../theme'
import Banner from '../../ui/Banner'
import { DIALOG_ACTIONS_SX, DIALOG_CONTENT_SX, DialogFrame } from '../../ui/FormDialog'
import LoadingState from '../../ui/LoadingState'
import { DisabledReason } from '../../ui/DisabledReason'
import { OverflowMenu } from '../../ui/OverflowMenu'
import SearchBox from '../../ui/SearchBox'
import { tapTargetSx } from '../../ui/listStyles'

/* PROJECT SETTINGS › PIPELINE › START FROM… (`apps/api/app/releases/pipeline_doc.py`): a preset or another project of
 * the org fills the editor as unsaved changes. The picker lists each source with its full stage chain; picking one
 * asks the server what would change (`POST …/pipeline/draft`, which writes nothing); "Use this" hands the draft to
 * the editor, where Save applies it and Discard returns to the saved pipeline. The Pipeline tab shows only for a
 * project with a repository, so every preset is offered.
 * LINK TO TEMPLATE… picks a published org template, shows what linking changes (`GET …/pipeline/template-preview`,
 * which writes nothing; a problem holds Link), and Link saves at once (`PUT …/pipeline/template`). */

export type StartMode = 'preset' | 'copy'
export type PipelineChoice = { value: string; name: string; chain: string; note?: string; disabled?: boolean }


const OPTION_SX = {
  m: 0, display: 'flex', alignItems: 'flex-start', gap: 1, p: 1, pr: 1.5, border: '1px solid', borderColor: 'divider',
  borderRadius: RADIUS.md, cursor: 'pointer', minWidth: 0, minHeight: SIZE.tapTarget,
  '&:has(input:checked)': { borderColor: 'primary.main', outline: '1px solid', outlineColor: 'primary.main', outlineOffset: -2 },
  '&:has(input:focus-visible)': { outline: '2px solid', outlineColor: 'primary.main', outlineOffset: 2 },
} as const

/* Two cards a row where they fit a card's measure, one on a phone; a chain wraps, never truncates. */
const CHOICES_SX = { display: 'grid', gap: 1, gridTemplateColumns: `repeat(auto-fill, minmax(min(100%, ${grid(30)}), 1fr))` } as const

/** A radio list of pipelines, each with its name and full stage chain (never truncated: it wraps); `label` names the
 *  group. */
export function PipelineChoices({ label, value, options, onChange, testId, disabled = false }: {
  label: string
  value: string | null
  options: readonly PipelineChoice[]
  onChange: (value: string) => void
  testId?: string
  disabled?: boolean
}): JSX.Element {
  return (
    <RadioGroup aria-label={label} value={value ?? ''} onChange={e => onChange(e.target.value)} data-testid={testId}
      sx={CHOICES_SX}>
      {options.map(option => (
        <Box component="label" key={option.value} sx={[OPTION_SX, (disabled || option.disabled === true) && { cursor: 'default', opacity: 0.6 }]}
          data-testid="pipeline-choice">
          <Radio size="small" value={option.value} disabled={disabled || option.disabled} sx={{ p: 0.5 }}
            slotProps={{ input: { 'data-testid': `pipeline-choice-${option.value}` } as object }} />
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.semibold, overflowWrap: 'anywhere' }}>{option.name}</Typography>
            <Typography variant="body2" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }} data-testid="pipeline-choice-chain">
              {option.chain}
            </Typography>
            {option.note && (
              <Typography variant="body2" sx={{ color: 'text.secondary', fontSize: FONT_SIZE.sm }}>{option.note}</Typography>
            )}
          </Box>
        </Box>
      ))}
    </RadioGroup>
  )
}

/** The Pipeline tab's Start from… button and its two choices; `disabledReason` holds it (a linked template). */
export function StartFromMenu({ busy, onPick, disabledReason }: {
  busy: boolean
  onPick: (mode: StartMode) => void
  disabledReason?: string
}): JSX.Element | null {
  return (
    <DisabledReason reason={disabledReason}>
      <OverflowMenu busy={busy || disabledReason !== undefined} testId="pipeline-start-from" sx={[tapTargetSx, { whiteSpace: 'nowrap' }]} button={{ label: 'Start from…', variant: 'outlined' }}
        items={[
          { key: 'preset', label: 'A preset…', ariaLabel: 'Start from a preset', testId: 'pipeline-start-preset', onClick: () => onPick('preset') },
          { key: 'copy', label: 'Copy from project…', ariaLabel: 'Copy from another project', testId: 'pipeline-start-copy', onClick: () => onPick('copy') },
        ]} />
    </DisabledReason>
  )
}

/** What a source changes, one change a line in plain words (`lib/pipeline.ts::changeText`). */
function ChangesList({ changes }: { changes: readonly PipelineChange[] }): JSX.Element {
  if (changes.length === 0) {
    return <Typography variant="body2" sx={{ color: 'text.secondary' }}>Nothing: it matches the saved pipeline.</Typography>
  }
  return (
    <Box component="ul" sx={{ m: 0, pl: 2.5 }} data-testid="pipeline-start-changes">
      {changes.map((c, i) => (
        <Typography component="li" variant="body2" key={i} sx={{ overflowWrap: 'anywhere' }}>{changeText(c, roleLabel)}</Typography>
      ))}
    </Box>
  )
}

/** The picker: choose a source, see what changes, then "Use this" hands the draft to the editor. */
export function StartFromDialog({ projectId, mode, onClose, onUse }: {
  projectId: number
  mode: StartMode
  onClose: () => void
  onUse: (draft: PipelineDraft, sourceName: string) => void
}): JSX.Element {
  const [picked, setPicked] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const presets = useQuery({ queryKey: presetsKey, queryFn: getPresets, enabled: mode === 'preset', staleTime: Infinity })
  const chains = useQuery({ queryKey: chainsKey(projectId), queryFn: () => getPipelineChains(projectId), enabled: mode === 'copy' })
  const source: DraftSource | null = picked === null ? null : mode === 'preset' ? { preset: picked } : { project_id: Number(picked) }
  const preview = useQuery({
    queryKey: ['pipeline-draft', projectId, source],
    queryFn: () => postPipelineDraft(projectId, source as DraftSource),
    enabled: source !== null,
    retry: false,
    gcTime: 0,
  })

  const list = mode === 'preset' ? presets : chains
  const all: PipelineChoice[] = mode === 'preset'
    ? (presets.data ?? []).map(p => ({ value: p.slug, name: p.name, chain: p.chain, note: p.note }))
    : (chains.data ?? []).map(c => ({ value: String(c.project_id), name: c.name, chain: c.chain }))
  const needle = search.trim().toLowerCase()
  const options = needle ? all.filter(o => o.name.toLowerCase().includes(needle)) : all
  const sourceName = all.find(o => o.value === picked)?.name ?? ''
  const title = mode === 'preset' ? 'Start from a preset' : 'Copy from project'

  return (
    <DialogFrame onClose={onClose} title={title} testId="pipeline-start-dialog">
      <DialogContent sx={[DIALOG_CONTENT_SX, { display: 'flex', flexDirection: 'column', gap: 2, pt: 0.5 }]}>
        {mode === 'copy' && (
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>
            Copies stages, branches and step rules. Addresses and freezes stay as they are.
          </Typography>
        )}
        {mode === 'copy' && (chains.data?.length ?? 0) > 5 && (
          <SearchBox value={search} onChange={setSearch} delayMs={0} ariaLabel="Search projects" placeholder="Search projects"
            testId="pipeline-start-search" />
        )}
        {list.isPending ? <LoadingState label={mode === 'preset' ? 'Loading presets' : 'Loading projects'} />
          : list.isError ? <Banner severity="error">{apiDetail(list.error, 'Could not load the list')}</Banner>
            : options.length === 0 ? (
              <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="pipeline-start-empty">
                {needle ? 'No project matches.' : 'No other project to copy from.'}
              </Typography>
            ) : (
              <PipelineChoices label={title} value={picked} options={options} onChange={setPicked} testId="pipeline-start-choices" />
            )}
        {source !== null && (
          <Box data-testid="pipeline-start-preview" aria-live="polite">
            <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.semibold, mb: 0.5 }}>What changes</Typography>
            {preview.isPending ? <LoadingState label="Checking what changes" />
              : preview.isError ? <Banner severity="error">{apiDetail(preview.error, 'Could not prepare it')}</Banner>
                : (
                  <>
                    <ChangesList changes={preview.data.changes} />
                    {preview.data.problems.map((p, i) => (
                      <Banner key={i} severity="warning" testId="pipeline-start-problem">{p.stage_index === undefined ? p.message : `${p.message} Fix it on that stage before you save.`}</Banner>
                    ))}
                    <Typography variant="body2" sx={{ color: 'text.secondary', mt: 1 }}>Nothing is saved until you press Save.</Typography>
                  </>
                )}
          </Box>
        )}
      </DialogContent>
      <DialogActions sx={DIALOG_ACTIONS_SX}>
        <Button onClick={onClose} sx={tapTargetSx} data-testid="pipeline-start-cancel">Cancel</Button>
        <Button variant="contained" sx={tapTargetSx} disabled={!preview.data || preview.isFetching} data-testid="pipeline-start-use"
          onClick={() => preview.data && onUse(preview.data, sourceName)}>
          Use this
        </Button>
      </DialogActions>
    </DialogFrame>
  )
}

/** Link to template…: pick a published org template, see what linking changes, then Link saves it. */
export function LinkTemplateDialog({ projectId, dirty, onClose, onLinked }: {
  projectId: number
  /** The editor holds unsaved edits: linking discards them. */
  dirty: boolean
  onClose: () => void
  onLinked: (fresh: Pipeline) => void
}): JSX.Element {
  const [picked, setPicked] = useState<string | null>(null)
  const templates = useQuery({ queryKey: TEMPLATES_KEY, queryFn: listTemplates })
  const preview = useQuery({
    queryKey: ['pipeline-template-preview', projectId, picked],
    queryFn: () => getTemplatePreview(projectId, picked as string),
    enabled: picked !== null,
    retry: false,
    gcTime: 0,
  })
  const { busy, formError, run } = useSubmit()
  const options: PipelineChoice[] = (templates.data ?? []).map(t => ({
    value: t.id, name: t.name, chain: t.chain, disabled: t.published === null, note: t.published === null ? 'Not published' : undefined,
  }))
  const problems = preview.data?.problems ?? []
  const link = () => void run('Could not link. Retry.', async () => {
    onLinked(await linkTemplate(projectId, picked as string))
  })

  return (
    <DialogFrame onClose={onClose} title="Link to template" testId="pipeline-link-dialog">
      <DialogContent sx={[DIALOG_CONTENT_SX, { display: 'flex', flexDirection: 'column', gap: 2, pt: 0.5 }]}>
        {templates.isPending ? <LoadingState label="Loading templates" />
          : templates.isError ? <Banner severity="error">{apiDetail(templates.error, 'Could not load the list')}</Banner>
            : options.length === 0 ? (
              <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="pipeline-link-empty">No templates yet.</Typography>
            ) : (
              <PipelineChoices label="Link to template" value={picked} options={options} onChange={setPicked} disabled={busy}
                testId="pipeline-link-choices" />
            )}
        {picked !== null && (
          <Box data-testid="pipeline-link-preview" aria-live="polite">
            <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.semibold, mb: 0.5 }}>What changes</Typography>
            {preview.isPending ? <LoadingState label="Checking what changes" />
              : preview.isError ? <Banner severity="error">{apiDetail(preview.error, 'Could not prepare it')}</Banner>
                : (
                  <>
                    <ChangesList changes={preview.data.changes} />
                    {problems.map((p, i) => (
                      <Banner key={i} severity="error" testId="pipeline-link-problem">{p.message}</Banner>
                    ))}
                    {dirty && (
                      <Typography variant="body2" sx={{ color: 'text.secondary', mt: 1 }} data-testid="pipeline-link-discards">
                        Your unsaved edits are discarded.
                      </Typography>
                    )}
                  </>
                )}
          </Box>
        )}
        {formError && <Banner severity="error" testId="pipeline-link-error">{formError}</Banner>}
      </DialogContent>
      <DialogActions sx={DIALOG_ACTIONS_SX}>
        <Button onClick={onClose} sx={tapTargetSx} data-testid="pipeline-link-cancel">Cancel</Button>
        <DisabledReason reason={problems.length > 0 ? 'Blocked: see above.' : undefined}>
          <Button variant="contained" sx={tapTargetSx} onClick={link} data-testid="pipeline-link-confirm"
            disabled={!preview.data || preview.isFetching || problems.length > 0 || busy}>
            Link
          </Button>
        </DisabledReason>
      </DialogActions>
    </DialogFrame>
  )
}
