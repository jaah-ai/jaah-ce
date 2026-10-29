import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Banner from '../ui/Banner'

/** A node Delete confirm's body: `lib/nodeDelete.ts`'s facts as a short list in the warning tone, one line each, so
 *  the phone reads what goes instead of a paragraph. Shared by `/nodes` and `/nodes/:nodeId`. */
export function NodeDeleteFacts({ facts }: { facts: readonly string[] }): JSX.Element {
  return (
    <Banner severity="warning">
      <Box component="ul" data-testid="node-delete-facts" sx={{ m: 0, pl: 2.5, display: 'grid', rowGap: 0.25 }}>
        {facts.map(fact => <li key={fact}>{fact}</li>)}
      </Box>
    </Banner>
  )
}

export default NodeDeleteFacts
