import { lazy } from 'react'
import { Route } from 'react-router-dom'
import { REMOVED_CONSOLE_ROUTES } from './lib/editionFeatures'

const NotInEdition = lazy(() => import('./pages/ce/NotInEdition'))

/* A feature this edition leaves out answers its console paths with the upsell, never "Page not found"
 * (`pages/ce/NotInEdition.tsx`; the paths are the edition map's `console_routes`). None in the cloud. A function, as
 * `App.tsx`'s `shellRoutes`: fresh elements per render. */
export function notInEditionRoutes() {
  return REMOVED_CONSOLE_ROUTES.map(({ path, feature }) => (
    <Route key={path} path={path} element={<NotInEdition feature={feature} />} />
  ))
}
