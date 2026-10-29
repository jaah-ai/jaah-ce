import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import type { JSX, ReactNode } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { EmptyState } from './EmptyState'
import { tapTargetSx } from './listStyles'
import PageHeader from './PageHeader'

/* A detail page whose record 404'd: the header (and its trail) stays, then what is missing and one link back. */
export function MissingRecordState({ breadcrumb, heading, title, headerTestId, backTo, backLabel }: {
  breadcrumb: ReactNode
  heading: string
  title: string
  headerTestId: string
  backTo: string
  backLabel: string
}): JSX.Element {
  const back = <Button component={RouterLink} to={backTo} variant="outlined" sx={tapTargetSx}>{backLabel}</Button>
  return (
    <Box>
      <PageHeader breadcrumb={breadcrumb} title={heading} testId={headerTestId} />
      <EmptyState title={title} action={back} />
    </Box>
  )
}

export default MissingRecordState
