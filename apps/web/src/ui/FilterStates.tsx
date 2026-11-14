/* The filter row's Clear. A failure over a good load keeps its rows under a `ui/RetryBanner`. */
import Button from '@mui/material/Button'
import { tapTargetSx } from './listStyles'

/** The filter row's Clear, rendered only while a filter is set. */
export function ClearFiltersButton(props: { show: boolean; onClick: () => void; testId: string }) {
  if (!props.show) return null
  return <Button variant="text" onClick={props.onClick} sx={tapTargetSx} data-testid={props.testId}>Clear filters</Button>
}
