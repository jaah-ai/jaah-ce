import type { JSX } from 'react'
import CloudUpsell from '../../components/CloudUpsell'
import { CLOUD_SIGNUP_URL } from '../../lib/edition'
import { LABELS, type FeatureName } from '../../lib/editionFeatures'
import { InterstitialPanel } from '../../ui/InterstitialPanel'

/** The sentence the API answers a removed route with too (`apps/api/app/ce_caps.py::NOT_IN_CE`). */
export const NOT_IN_EDITION = 'Not in Jaah Community Edition. Available on Jaah Cloud.'

/* A console path of a feature this edition leaves out (`REMOVED_CONSOLE_ROUTES`, mounted by `App.tsx`): a real page
 * naming the feature by its label, never a redirect, a blank page or "Page not found". */
export default function NotInEdition({ feature }: { feature: FeatureName }): JSX.Element {
  return (
    <InterstitialPanel testId="not-in-edition" titleTestId="not-in-edition-title" title={LABELS[feature]}>
      <CloudUpsell cta="Get Jaah Cloud" href={CLOUD_SIGNUP_URL} testId="not-in-edition-upsell">
        {NOT_IN_EDITION}
      </CloudUpsell>
    </InterstitialPanel>
  )
}
