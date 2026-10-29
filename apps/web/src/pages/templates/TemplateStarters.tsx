import { useState, type JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { useQuery } from '@tanstack/react-query'
import { get } from '../../api'
import { useCompanySetupGuide } from '../../components/onboarding/companySetupGuide'
import SetupDiagram from '../../components/onboarding/SetupDiagram'
import CardGrid from '../../ui/gallery/CardGrid'
import type { TaskTreeCounts } from '../../types'
import { TEMPLATES_KEY, listTemplates, type TemplateCard } from './api'
import { STARTER_MAX_TASKS, mostPopular } from './sections'
import StartTemplateDialog from './StartTemplateDialog'
import TemplateCardView from './TemplateCardView'

/* THE TASKS BOARD'S STARTERS, under the unfiltered list while the org has at most `STARTER_MAX_TASKS` tasks.
 * While the company setup is unfinished up to First task (every step before it), the setup diagram
 * (`components/onboarding/SetupDiagram.tsx`, the company guide's own rows); once it is, the Most popular templates
 * (`sections.ts::mostPopular`: starts across every org for a global template) with Start and Customize, and "All
 * templates" onto the gallery. A viewer the company guide is not for (not an org admin) goes straight to the
 * templates. None started yet: nothing. */
export default function TemplateStarters(): JSX.Element | null {
  const counts = useQuery({
    queryKey: ['tasks', 'counts', 'all'],
    queryFn: () => get<TaskTreeCounts>('/api/tasks/counts'),
    staleTime: 30_000,
  })
  const few = counts.data !== undefined && counts.data.total <= STARTER_MAX_TASKS
  const setup = useCompanySetupGuide({ passive: true })
  const steps = setup.guide?.steps ?? []
  const setUp = steps.every(step => step.done || step.id === 'task')
  const templates = useQuery({ queryKey: TEMPLATES_KEY, queryFn: () => listTemplates(), enabled: few && !setup.pending && setUp })
  const [starting, setStarting] = useState<TemplateCard | null>(null)
  if (!few || setup.pending) return null
  if (!setUp) return <SetupDiagram steps={steps} />
  const cards = mostPopular(templates.data ?? [])
  if (cards.length === 0) return null
  return (
    <Box component="section" aria-labelledby="task-starters-title" data-testid="task-template-starters" sx={{ px: { xs: 0, sm: 1 }, py: 2 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1, flexWrap: 'wrap', mb: 1.5 }}>
        <Typography id="task-starters-title" component="h2" variant="subtitle1" sx={{ fontWeight: 600 }}>
          Most popular templates
        </Typography>
        <Button component={RouterLink} to="/templates" data-testid="task-template-starters-all" sx={{ minHeight: 44 }}>
          All templates
        </Button>
      </Box>
      <CardGrid label="Templates">
        {cards.map(c => <TemplateCardView key={c.id} card={c} onStart={setStarting} />)}
      </CardGrid>
      {starting && <StartTemplateDialog card={starting} onClose={() => setStarting(null)} />}
    </Box>
  )
}
