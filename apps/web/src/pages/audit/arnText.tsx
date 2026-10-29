import { Fragment, type JSX } from 'react'
import Box from '@mui/material/Box'
import { CopyButton } from '../../ui/CopyButton'
import { monoSx } from '../../ui/listStyles'

/* ECS's sentences (a task's stop reason, a service event) quote whole ARNs; one target-group ARN wraps a
 * table cell to eight lines. Each ARN renders as its resource NAME (its last segment that is not an ELB id), the
 * full ARN on hover and behind a copy icon (phones have no hover). Text with no ARN renders unchanged. */

/* `arn:partition:service:region:account:resource`; the resource stops at whitespace, a bracket or a quote. */
const ARN_RE = /arn:aws[\w-]*:[\w-]+:[\w-]*:\d*:[^\s()[\]{},;"']+/g

/* ELB names its resources `targetgroup/<name>/<id>` and `loadbalancer/app/<name>/<id>` (a listener adds an id). */
const ELB_KIND = new Set(['app', 'net', 'gwy'])
const ELB_ID = /^[0-9a-f]{16}$/
/* A sentence's own punctuation after an ARN; no ARN ends in one. */
const TRAILING_PUNCTUATION = /[.:!?]+$/
/* What closes the ARN's clause in ECS's sentences: `)` and `]`, then the sentence's own punctuation. */
const CLOSING = /^[)\]]*[.,;:!?]*/

/** The resource's own name: `targetgroup/jaah-dev-cp/0123…` → `jaah-dev-cp`, `task/jaah/fedc…` → `fedc…`. */
export function arnName(arn: string): string {
  const [, , service = '', , , ...rest] = arn.split(':')
  const segments = rest.join(':').split('/')
  if (segments.length === 1) return segments[0]
  let named = segments.slice(1)
  if (service === 'elasticloadbalancing') {
    if (segments[0] !== 'targetgroup' && ELB_KIND.has(named[0])) named = named.slice(1)
    named = named.filter(s => !ELB_ID.test(s))
  }
  return named.at(-1) || arn
}

/** `text` with each ARN shortened to its name; a sentence's own punctuation is not part of the ARN. */
export function ArnText({ text }: { text: string }): JSX.Element {
  const parts: (string | JSX.Element)[] = []
  let last = 0
  for (const match of text.matchAll(ARN_RE)) {
    const arn = match[0].replace(TRAILING_PUNCTUATION, '')
    const start = match.index
    /* The sentence's closing bracket and punctuation ride inside the ARN's unbreakable span: a line never
       starts with a lone `)`. */
    const closing = CLOSING.exec(text.slice(start + arn.length))?.[0] ?? ''
    parts.push(text.slice(last, start))
    parts.push(
      <Box component="span" data-testid="arn" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.25 }}>
        <Box component="span" title={arn} sx={monoSx}>{arnName(arn)}</Box>
        {/* The 24px target pads the 14px glyph by 5px a side: pulled in on the right, the closing bracket sits
            against the glyph, not 5px off it. */}
        <CopyButton variant="icon" text={() => arn} label="Copy ARN" sx={closing ? { mr: '-5px' } : undefined} />
        {closing}
      </Box>,
    )
    last = start + arn.length + closing.length
  }
  parts.push(text.slice(last))
  return <>{parts.map((part, i) => <Fragment key={i}>{part}</Fragment>)}</>
}
