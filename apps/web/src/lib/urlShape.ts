/* The shape check behind a free-form URL field (`registry.CredentialTypeSpec.free_form_url`; the REST custom
 * connectors whose connections are those rows). It only WARNS: the server stores any text there.
 *
 * Well-formed: `[protocol://][user[:password]@]host[:port][/path?query#fragment]`, where the protocol is any scheme
 * word, the login part needs a protocol, the host is a hostname (letters and marks of any script, digits, `-`, `_`),
 * a dotted IPv4 address or a bracketed IPv6 one (its inside checked loosely), and the port 1-65535. Anything else
 * (spaces, a bare IPv6, a port out of range) earns the warning. */

const SCHEME = String.raw`(?:[a-z][a-z0-9+.-]*:\/\/(?:[^@\s/?#]+@)?)?`
const LABEL = String.raw`[\p{L}\p{N}_](?:[\p{L}\p{M}\p{N}_-]*[\p{L}\p{M}\p{N}_])?`
const HOST = String.raw`(?<host>${LABEL}(?:\.${LABEL})*\.?|\[[0-9a-f:.]+(?:%[\p{L}\p{N}._-]+)?\])`
const PORT = String.raw`(?::(?<port>\d{1,5}))?`
const REST = String.raw`(?:[/?#]\S*)?`
const URL_SHAPE = new RegExp(`^${SCHEME}${HOST}${PORT}${REST}$`, 'iu')
const DOTTED_NUMBERS = /^[\d.]+$/

/** What the ⚠ says on hover. */
export const URL_SHAPE_WARNING = 'This URL may not be well-formed.'

/** Whether `value` reads as `[protocol://]host[:port][/…]` (see above). Blank counts as fine. */
export function isWellFormedUrl(value: string): boolean {
  const trimmed = value.trim()
  if (!trimmed) return true
  const match = URL_SHAPE.exec(trimmed)
  if (!match?.groups) return false
  const { host, port } = match.groups
  if (port !== undefined && (Number(port) < 1 || Number(port) > 65535)) return false
  // All digits and dots: an IPv4 address, so exactly four octets of 0-255.
  if (DOTTED_NUMBERS.test(host)) {
    const octets = host.replace(/\.$/, '').split('.')
    return octets.length === 4 && octets.every(o => o !== '' && Number(o) <= 255)
  }
  return true
}

/** The warning for a free-form URL field's value, or undefined when it looks well-formed. */
export function urlShapeWarning(value: string): string | undefined {
  return isWellFormedUrl(value) ? undefined : URL_SHAPE_WARNING
}

/** Whether a Test can dial `value`: an http(s) URL with a host (`custom.is_http_url` on the server). The literal
 *  `http(s)://` comes first: `new URL` repairs `http:/host` and `https:\\host`, which the server refuses. */
export function isHttpUrl(value: string): boolean {
  if (!/^https?:\/\//i.test(value.trim())) return false
  try {
    const url = new URL(value.trim())
    return (url.protocol === 'http:' || url.protocol === 'https:') && url.hostname !== ''
  } catch {
    return false
  }
}
