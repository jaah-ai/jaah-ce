/* Which edition this build is: the cloud console or the single-user community edition. A literal, never a knob — no
   env var or build flag selects it; only the mirror build sets `EDITION` (tools/community_edition/build_tree.py, from
   the manifest's `[mirror] edition`). Guard a cloud-only feature by its name (`lib/editionFeatures.ts`), never by
   `IS_CE`. Import-free: it is a rewrite target in the public tree. */

export const EDITION: string = 'ce'
export const IS_CE: boolean = EDITION === 'ce'

/* Where a CE user is pointed to for the cloud product. A static link only: the community edition calls no Jaah host. */
export const CLOUD_SIGNUP_URL = 'https://app.jaah.ai/signup'

/** The main website: the live cast viewer's brand link. */
export const SITE_URL = 'https://jaah.ai'
