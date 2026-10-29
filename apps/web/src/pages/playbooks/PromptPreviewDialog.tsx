import { type JSX, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import FormControlLabel from '@mui/material/FormControlLabel'
import MenuItem from '@mui/material/MenuItem'
import Switch from '@mui/material/Switch'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { ApiError, apiDetail } from '../../api'
import { TabPanel, Tabs } from '../../components/Tabs'
import Banner from '../../ui/Banner'
import FormDialog from '../../ui/FormDialog'
import { PHONE_QUERY } from '../../ui/listStyles'
import LoadingState from '../../ui/LoadingState'
import { type PlaybookIn, type StageIn, type Vocabulary, issuesOf, labelOf, previewPrompt } from './api'
import { CodeBlock } from '../../ui/CodeBlock'

/* "PREVIEW PROMPT" — one stage's composed system and task prompt, rendered by the server against the fixed
 * sample fixture (`POST /api/playbooks/preview-prompt`, D4: no project picker). Rendered from the editor's
 * CURRENT draft, so an unsaved edit previews as readily as a saved one. A chosen-at-start stage gets an
 * expert picker; a stage that asks questions gets the Full Auto switch (the prompt differs by it). The text
 * is a scrollable monospace block that wraps — never clipped — and the dialog is full-screen on a phone. */

type Tab = 'system' | 'task'
const TABS = [{ id: 'system', label: 'System' }, { id: 'task', label: 'Task' }] as const

export default function PromptPreviewDialog({
  playbook,
  stage,
  vocab,
  onClose,
}: {
  playbook: PlaybookIn
  stage: StageIn
  vocab?: Vocabulary
  onClose: () => void
}): JSX.Element {
  const [tab, setTab] = useState<Tab>('system')
  const chosen = stage.expert.kind === 'chosen_at_start' ? stage.expert : null
  const [expert, setExpert] = useState<string>(chosen?.default ?? '')
  const interviews = stage.interviews === 'follows_full_auto'
  const [fullAuto, setFullAuto] = useState<boolean>(playbook.full_auto_default)

  const body = {
    playbook,
    stage_key: stage.key,
    ...(chosen && expert ? { expert } : {}),
    ...(interviews ? { full_auto: fullAuto } : {}),
  }
  const query = useQuery({
    queryKey: ['playbooks', 'preview', JSON.stringify(body)],
    queryFn: () => previewPrompt(body),
    retry: false,
    staleTime: Infinity,
  })

  const refused = query.error instanceof ApiError ? issuesOf(query.error.body) : null
  const text = query.data ? (tab === 'system' ? query.data.system_prompt : query.data.task_prompt) : ''
  const title = stage.label.trim() || stage.key

  return (
    <FormDialog
      title={`Prompt: ${title}`}
      submitLabel="Close"
      submitTestId="prompt-preview-close"
      hideCancel
      onSubmit={onClose}
      onClose={onClose}
      fullScreenOnPhone
      maxWidth="lg"
      testId="prompt-preview"
      paperSx={{ height: 'min(90vh, 60rem)', [`@media ${PHONE_QUERY}`]: { height: '100%' } }}
      contentSx={{ p: 0 }}
      header={
        <>
          <Box sx={{ px: 2, pb: 1, display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 2 }}>
            <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="prompt-preview-tier">
              Model tier: {query.data ? labelOf(vocab, 'tier', query.data.model_tier) : '…'} · Sample project
            </Typography>
            {chosen && (
              <TextField
                select
                size="small"
                label="Expert"
                value={expert}
                onChange={e => setExpert(e.target.value)}
                sx={{ minWidth: '12rem' }}
                slotProps={{ htmlInput: { 'data-testid': 'prompt-preview-expert' } }}
              >
                {chosen.allowed.map(k => (
                  <MenuItem key={k} value={k}>{labelOf(vocab, 'expert', k)}</MenuItem>
                ))}
              </TextField>
            )}
            {interviews && (
              <FormControlLabel
                label="Full Auto"
                sx={{ minHeight: 44 }}
                control={
                  <Switch
                    checked={fullAuto}
                    onChange={(_, v) => setFullAuto(v)}
                    slotProps={{ input: { 'data-testid': 'prompt-preview-full-auto' } as object }}
                  />
                }
              />
            )}
          </Box>
          <Box sx={{ px: 2 }}>
            <Tabs<Tab> tabs={TABS} tab={tab} onSelect={setTab} label="Prompt part" idPrefix="prompt" />
          </Box>
          {/* The inactive tab's empty panel; the content below is the active one in every state, so both
              tabs' `aria-controls` resolve. */}
          <TabPanel id={tab === 'system' ? 'task' : 'system'} current={tab} idPrefix="prompt">{null}</TabPanel>
        </>
      }
    >
      <Box
        id={`prompt-panel-${tab}`}
        role="tabpanel"
        aria-labelledby={`prompt-tab-${tab}`}
        sx={{ display: 'flex', flex: '1 1 auto', minHeight: 0 }}
      >
        {query.isPending ? (
          <Box sx={{ p: 2 }}><LoadingState /></Box>
        ) : query.isError ? (
          <Box sx={{ p: 2, display: 'grid', gap: 1 }}>
            {refused ? (
              refused.map((i, k) => <Banner key={k} severity="error">{i.message}</Banner>)
            ) : (
              <Banner severity="error">{apiDetail(query.error, 'Could not render the prompt.')}</Banner>
            )}
          </Box>
        ) : (
          <CodeBlock
            tabIndex={0}
            testId="prompt-preview-text"
            size="medium"
            sx={{ p: 2, flex: 1, overflow: 'auto', borderRadius: 0 }}
          >
            {text}
          </CodeBlock>
        )}
      </Box>
    </FormDialog>
  )
}
