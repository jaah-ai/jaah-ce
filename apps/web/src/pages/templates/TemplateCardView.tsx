import { type JSX } from 'react'
import { Link as RouterLink, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { personaLabel } from '../../components/tasks/personaRegistry'
import GalleryCard from '../../ui/gallery/GalleryCard'
import {
  BoltIcon, CogIcon, DashboardIcon, LogIcon, ShieldIcon, StarFilledIcon, StarIcon, TasksIcon,
} from '../../ui/icons'
import { TEMPLATES_KEY, customizePath, duplicateTemplate, purposeLabel, setFavorite, type TemplateCard } from './api'

/* ONE TEMPLATE AS A GALLERY CARD (`ui/gallery/GalleryCard`; anatomy: Task Templates PRD §7) — image, name, summary,
 * "persona · origin", the favorite star, and Start / Customize for a viewer who may start tasks (`may_start`, §6a;
 * one who may not browses and stars only). Duplicate appears on a global card when the caller passes
 * `duplicable` (the org's Templates (org) switch is on and the viewer edits templates). Used by the gallery and
 * the empty Tasks state. */

const PURPOSE_ICON: Record<string, typeof BoltIcon> = {
  build_fix: BoltIcon,
  quality: TasksIcon,
  security: ShieldIcon,
  maintenance: CogIcon,
  docs_release: LogIcon,
  research_planning: DashboardIcon,
}

/** The purpose group's placeholder, drawn where a template has no image (or its image fails to load). */
export function PurposePlaceholder({ purpose }: { purpose: string }): JSX.Element {
  const Glyph = PURPOSE_ICON[purpose] ?? TasksIcon
  return (
    <Box
      role="img"
      aria-label={purposeLabel(purpose)}
      data-testid="template-placeholder"
      sx={{
        width: '100%',
        height: '100%',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: 'text.secondary',
        bgcolor: 'action.selected',
      }}
    >
      <Glyph size="48px" />
    </Box>
  )
}

export const originLabel = (card: TemplateCard): string => (card.origin === 'global' ? 'Official' : 'Your org')

export default function TemplateCardView({ card, onStart, duplicable = false }: {
  card: TemplateCard
  onStart: (card: TemplateCard) => void
  duplicable?: boolean
}): JSX.Element {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const favorite = useMutation({
    mutationFn: (on: boolean) => setFavorite(card.id, on),
    onSettled: () => queryClient.invalidateQueries({ queryKey: TEMPLATES_KEY }),
  })
  const duplicate = useMutation({
    mutationFn: () => duplicateTemplate(card.id),
    onSuccess: copy => {
      void queryClient.invalidateQueries({ queryKey: TEMPLATES_KEY })
      navigate(`/templates/${copy.id}`)
    },
  })
  // The pressed value shows at once; the refetch confirms it.
  const starred = favorite.isPending ? favorite.variables : card.favorite
  const testId = `template-card-${card.slug}`
  return (
    <GalleryCard
      testId={testId}
      name={card.name}
      summary={card.summary}
      image={card.image_url}
      placeholder={<PurposePlaceholder purpose={card.purpose} />}
      meta={`${personaLabel(card.persona)} · ${originLabel(card)}`}
      aside={(
        <IconButton
          aria-label={starred ? `Remove ${card.name} from favorites` : `Add ${card.name} to favorites`}
          aria-pressed={starred}
          title={starred ? 'Remove from favorites' : 'Add to favorites'}
          onClick={() => favorite.mutate(!starred)}
          data-testid={`${testId}-favorite`}
          sx={{ color: starred ? 'warning.main' : 'text.secondary' }}
        >
          {starred ? <StarFilledIcon size="20px" /> : <StarIcon size="20px" />}
        </IconButton>
      )}
      actions={(card.may_start || (duplicable && card.origin === 'global')) && (
        <>
          {card.may_start && (
            <>
              <Button variant="contained" onClick={() => onStart(card)} data-testid={`${testId}-start`}>
                Start
              </Button>
              <Button component={RouterLink} to={customizePath(card.id)} data-testid={`${testId}-customize`}>
                Customize
              </Button>
            </>
          )}
          {duplicable && card.origin === 'global' && (
            <Button
              onClick={() => duplicate.mutate()}
              disabled={duplicate.isPending}
              title={duplicate.isError ? 'Could not duplicate. Try again.' : 'Copy into your org to edit'}
              data-testid={`${testId}-duplicate`}
            >
              Duplicate
            </Button>
          )}
        </>
      )}
    />
  )
}
