/* The console's ONE verdict on an href it did NOT build (`outcome_pr_url`, `pr_url`, `repo_url`,
 * `oauth_url` — agent-attested, typed or scraped, all prompt-injectable). React does NOT block a
 * `javascript:` href in production, so an unvalidated one executes in the clicking operator's session.
 * Whitelist: an absolute `http:`/`https:` URL with no userinfo, or no link. Keep it the only copy — a
 * second spelling drifted before. It is strict about whitespace because the value also travels as
 * text, clipboard and API payload.
 * Returns the ORIGINAL string, never `URL.toString()`, which would normalise (lowercase host, add a
 * root path) every approved link. */
export function safeHref(value: string | null | undefined): string | undefined {
  if (!value) return undefined
  /* Whitespace and control bytes are refused, not stripped: WHATWG strips them before parsing, but the
   * approved string is also read by parsers that do not (Python `urllib`, Go `net/url`), so the browser's
   * URL and the next reader's would differ. No legitimate value carries a raw one. */
  if (/[\u0000-\u0020]/.test(value)) return undefined
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    // Unparseable, relative or scheme-relative: never resolved against the console's own origin.
    return undefined
  }
  // A whitelist, not a blocklist, so a novel scheme is refused. `protocol` is already lowercased.
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return undefined
  /* Userinfo, tested on the RAW string (the parser erases `@` for `https://@evil/`) as an `@` inside the
   * authority — up to the first `/`, `?`, `#` or `\` (`https://github.com/u/@roman` must pass). The
   * separator is `[/\\]*`, not `//`: a special scheme treats any run of slashes/backslashes as the
   * delimiter, so `https:\\host@evil/` and `https:///host@evil/` resolve to `evil`. */
  if (/@/.test(value.replace(/^[a-z][a-z0-9+.-]*:[/\\]*/i, '').split(/[/?#\\]/)[0])) return undefined
  return value
}
