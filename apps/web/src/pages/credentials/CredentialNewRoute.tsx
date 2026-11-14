import { type JSX } from 'react'
import { useSearchParams } from 'react-router-dom'
import CredentialEditPage from './CredentialEditPage'
import IntegrationGallery from './integrations/IntegrationGallery'

/* `/credentials/new`: a link that knows what it needs (`?type=<slug>`, or a catalog vendor's `?integration=<slug>`)
 * opens the form on it; a bare New secret opens the integrations gallery to pick from. */
export default function CredentialNewRoute(): JSX.Element {
  const [params] = useSearchParams()
  return params.has('type') || params.has('integration') ? <CredentialEditPage /> : <IntegrationGallery />
}
