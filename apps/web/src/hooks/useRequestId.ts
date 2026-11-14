import { useState } from 'react'
import { mintRequestId } from '../lib/mintUuid'

/** The ONE per-dialog request id (`lib/mintUuid.ts::mintRequestId`, React-free): minted on mount so a
 *  double-sent request acts once; `fresh()` mints the next when anything in the request changes. */
export function useRequestId(): [string | undefined, () => void] {
  const [id, setId] = useState(mintRequestId)
  return [id, () => setId(mintRequestId())]
}
