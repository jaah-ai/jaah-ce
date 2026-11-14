import Typography from '@mui/material/Typography'
import { useQuery } from '@tanstack/react-query'
import { get } from '../../../api'
import type { FeatureOut } from '../../../types'
import { featureQueryKey } from './queryKeys'

/* THE FEATURE'S DESCRIPTION above the task list when the selection is one feature — read-only, and nothing at
 * all while it loads, fails, or is empty. The feature's name is the pane header's; Edit and Delete are the rail
 * row menu's (`rail/TaskTree.tsx`). */
export default function FeatureDescription({ featureId }: { featureId: string }) {
  const { data: feature } = useQuery({
    queryKey: featureQueryKey(featureId),
    queryFn: () => get<FeatureOut>(`/api/features/${featureId}`),
  })
  if (!feature?.description) return null
  return (
    <Typography
      variant="body2"
      data-testid="feature-description"
      sx={{ px: 2, py: 1.5, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}
    >
      {feature.description}
    </Typography>
  )
}
