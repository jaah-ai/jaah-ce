import type { ComponentType, JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { STATUS_WORDS, isStepActionable, nextStepId, statusOf, type SetupChecklistStatus, type SetupChecklistStep } from '../setupChecklist/SetupChecklist'
import { Tip } from '../../ui/Tip'
import { VISUALLY_HIDDEN } from '../../ui/visuallyHidden'
import { ELLIPSIS_SX } from '../../ui/listStyles'
import { FONT_WEIGHT, LINE_HEIGHT, RADIUS, SIZE, grid } from '../../theme'
import { BoltIcon, CardIcon, FolderIcon, ServerIcon, TasksIcon, type IconProps } from '../../ui/icons'

/* THE SETUP DIAGRAM — the Tasks board's picture of the company setup while it is unfinished (`pages/templates/
 * TemplateStarters.tsx` picks it over the templates). Presentation only: the steps, their states and links are the
 * company guide's rows (`companySetupGuide.tsx`), so the diagram and the docked checklist never disagree. One tile
 * per step in a left-to-right flow (top-to-bottom on a phone): an icon, the step's 2–4 word label, its state, and
 * the step's one-word link while it is open. Words are the rows' own; nothing here explains more. */

const ICONS: Record<string, ComponentType<IconProps>> = {
  plan: CardIcon,
  node: ServerIcon,
  account: BoltIcon,
  project: FolderIcon,
  task: TasksIcon,
}

/** The most words a tile's note shows; a longer one shows its state word, the note on hover. */
const SHORT_WORDS = 4

/** The icon disc's colours per state; `next` is the one step to do now. */
function discSx(status: SetupChecklistStatus, next: boolean) {
  if (status === 'done') return { bgcolor: 'success.main', color: 'success.contrastText', borderColor: 'success.main' }
  if (status === 'failed') return { bgcolor: 'transparent', color: 'error.main', borderColor: 'error.main' }
  if (next) return { bgcolor: 'primary.main', color: 'primary.contrastText', borderColor: 'primary.main' }
  return { bgcolor: 'transparent', color: 'text.secondary', borderColor: 'divider', borderStyle: status === 'blocked' ? 'dashed' : 'solid' }
}

function Tile({ step, next }: { step: SetupChecklistStep; next: boolean }): JSX.Element {
  const status = statusOf(step)
  const Glyph = ICONS[step.id] ?? TasksIcon
  const linked = isStepActionable(step)
  /* The step's words under its label: who must act or what it waits on (`note`), else where it stands. */
  const words = step.note || step.statusNote || (status === 'done' ? null : status === 'todo' ? null : STATUS_WORDS[status])
  /* A sentence (a refused add's "…not available for your organization right now. Ask an operator.") stood as a tall
     column under its label: the tile says where the step stands, the sentence is its hover. */
  const shown = words && words.split(/\s+/).length > SHORT_WORDS ? STATUS_WORDS[status] : words
  return (
    <Box
      component={linked ? RouterLink : 'div'}
      {...(linked ? { to: step.to } : {})}
      data-testid={`setup-diagram-step-${step.id}`}
      data-status={status}
      aria-current={next ? 'step' : undefined}
      sx={{
        display: 'flex', flexDirection: { xs: 'row', md: 'column' }, alignItems: 'center', gap: 1,
        flex: 1, alignSelf: 'stretch', minWidth: 0, p: 1.5, borderRadius: RADIUS.md, color: 'inherit', textDecoration: 'none',
        textAlign: { xs: 'left', md: 'center' },
        border: 1, borderColor: next ? 'primary.main' : 'transparent',
        ...(linked && { '&:hover, &:focus-visible': { bgcolor: 'action.hover' } }),
      }}
    >
      <Box
        sx={{
          width: grid(8), height: grid(8), borderRadius: RADIUS.round, border: 2, flexShrink: 0,
          display: 'flex', alignItems: 'center', justifyContent: 'center', ...discSx(status, next),
        }}
      >
        <Glyph size={SIZE.iconLg} />
      </Box>
      <Box sx={{ minWidth: 0, maxWidth: '100%' }}>
        <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.semibold, color: status === 'blocked' ? 'text.secondary' : 'text.primary' }}>
          {step.label}
          <Box component="span" sx={VISUALLY_HIDDEN}>{`, ${STATUS_WORDS[status]}`}</Box>
        </Typography>
        {words && (
          <Tip title={words}>
            <Typography variant="caption" component="div" data-testid={`setup-diagram-words-${step.id}`} sx={{ ...ELLIPSIS_SX, color: 'text.secondary' }}>
              {shown}
            </Typography>
          </Tip>
        )}
        {linked && <Typography variant="caption" color="primary" component="div" sx={{ fontWeight: FONT_WEIGHT.semibold }}>{step.cta}</Typography>}
      </Box>
    </Box>
  )
}

/** The arrow between two tiles: right on a desktop, down on a phone. */
function Arrow(): JSX.Element {
  return (
    <Box aria-hidden="true" sx={{ color: 'text.disabled', alignSelf: { xs: 'flex-start', md: 'center' }, lineHeight: LINE_HEIGHT.tight, fontSize: t => t.typography.h6.fontSize, transform: { xs: 'rotate(90deg)', md: 'none' }, ml: { xs: 3.5, md: 0 } }}>
      →
    </Box>
  )
}

export default function SetupDiagram({ steps }: { steps: readonly SetupChecklistStep[] }): JSX.Element {
  const next = nextStepId(steps)
  return (
    <Box component="section" aria-labelledby="setup-diagram-title" data-testid="setup-diagram" sx={{ px: { xs: 0, sm: 1 }, py: 2 }}>
      <Typography id="setup-diagram-title" component="h2" variant="subtitle1" sx={{ fontWeight: FONT_WEIGHT.semibold, mb: 1.5 }}>
        Set up, then start tasks
      </Typography>
      <Box
        component="ol"
        sx={{
          listStyle: 'none', m: 0, p: 1, display: 'flex', flexDirection: { xs: 'column', md: 'row' },
          gap: 0.5, border: 1, borderColor: 'divider', borderRadius: RADIUS.md,
        }}
      >
        {steps.map((step, i) => (
          <Box
            component="li"
            key={step.id}
            sx={{ display: 'flex', flexDirection: { xs: 'column', md: 'row' }, alignItems: { xs: 'stretch', md: 'center' }, gap: 0.5, flex: { md: '1 1 0' }, minWidth: 0 }}
          >
            {i > 0 && <Arrow />}
            <Tile step={step} next={step.id === next} />
          </Box>
        ))}
      </Box>
    </Box>
  )
}
