import type { JSX } from 'react'
import Box from '@mui/material/Box'
import { FONT_SIZE, grid } from '../../theme'
import RecordFeed from '../../ui/feed/RecordFeed'
import type { FeedColumns, FeedIndicator, FeedRowSpec } from '../../ui/feed/types'
import {
  listTemplates, NEW_PIPELINE_TEMPLATE_PATH, STATE_LABEL, templatePath, TEMPLATES_KEY, type PipelineTemplateSummary,
  type TemplateState,
} from './api'

/* SETTINGS › PROJECTS › PIPELINE TEMPLATES — the org's templates, one feed row each (`GET /api/org/pipeline-templates`):
 * the name with its whole stage chain under it (wrapped, never cut), how many projects are linked, and its state
 * (Never published, Published, Draft changes). A row opens the template's tabbed page
 * (`PipelineTemplateDetailPage.tsx`); New opens the same page in create mode. The Settings panel's body
 * (`pages/Settings.tsx`), shown to an Org Owner/Admin. */

const COLUMNS: FeedColumns = {
  title: { key: 'name', label: 'Name' },
  slots: [
    { key: 'linked', label: 'Linked' },
    { key: 'state', label: 'State' },
  ],
}

const INDICATOR: Record<TemplateState, FeedIndicator> = {
  published: { label: STATE_LABEL.published, tone: 'success', glyph: '●' },
  draft_changes: { label: STATE_LABEL.draft_changes, tone: 'warning', glyph: '◐' },
  never_published: { label: STATE_LABEL.never_published, tone: 'neutral', glyph: '○' },
}

function rowOf(template: PipelineTemplateSummary): FeedRowSpec {
  const linked = `${template.linked_count} ${template.linked_count === 1 ? 'project' : 'projects'}`
  return {
    key: template.id,
    indicator: INDICATOR[template.state],
    title: (
      <Box sx={{ minWidth: 0 }}>
        <Box component="span" sx={{ display: 'block', overflowWrap: 'anywhere' }}>{template.name}</Box>
        <Box component="span" sx={{ display: 'block', color: 'text.secondary', fontSize: FONT_SIZE.sm, overflowWrap: 'anywhere' }}
          data-testid="template-chain">
          {template.chain}
        </Box>
      </Box>
    ),
    titleText: template.name,
    titleLines: 'all',
    href: templatePath(template.id),
    data: { state: template.state },
    slots: [
      { key: 'linked', width: grid(13), align: 'right', node: linked, title: linked, testId: 'template-linked' },
      { key: 'state', width: grid(15), align: 'right', node: STATE_LABEL[template.state], testId: 'template-state' },
    ],
  }
}

/** Settings › Pipeline templates' body: loading, failed, none yet, or the rows. */
export const PipelineTemplateFeed = (): JSX.Element => (
  <RecordFeed id="pipeline-templates" label="Pipeline templates" queryKey={TEMPLATES_KEY} list={listTemplates} rowOf={rowOf}
    columns={COLUMNS} emptyTitle="No templates yet" newLabel="New template" newTo={NEW_PIPELINE_TEMPLATE_PATH} />
)

