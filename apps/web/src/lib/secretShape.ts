/* The shape check behind a credential's Description: text that reads like a secret earns a warning, since the
 * description is not secret (every task that may use the credential is listed it). It only WARNS: the server
 * stores any text there, and a guess must never block a save.
 *
 * It looks for a secret-named label followed by a credential-looking value (`2FA seed: abcd efgh …`,
 * `token=…`), a private-key block, a self-identifying token prefix, and a run of base32 (a TOTP seed, often
 * grouped in fours) or of hex long enough to be a key. A label followed by prose (`Password: stored in the
 * vault`) stays quiet: its value must hold a digit, run 16+ characters, or be a grouped seed. A long hex ID
 * (a git SHA) still warns; a warning is cheap. */

// A grouped TOTP seed, either case: `GEZD GNBV GY3T QOJQ` or `gezd-gnbv-gy3t-qojq`.
const GROUPED_SEED = String.raw`(?:[A-Za-z2-7]{4}[ -]){3,}[A-Za-z2-7]{4}`
const LABEL = String.raw`\b(?:2fa|totp|otp|mfa|seed|secret|token|api[ _-]?key|password|passwd|pwd|pw|private[ _-]?key)\b`
// After the separator: a grouped seed, or one word holding a digit or running 16+ characters.
const VALUE = String.raw`(?:${GROUPED_SEED}|(?=\S*\d)\S{6,}|\S{16,})`
const LABELLED = new RegExp(String.raw`${LABEL}[^\S\r\n]*(?:[:=]|is\b)[^\S\r\n]*${VALUE}`, 'i')
const PRIVATE_KEY = /-----BEGIN [A-Z ]*PRIVATE KEY-----/
const SELF_IDENTIFYING = new RegExp([
  String.raw`\bgh[pousr]_[A-Za-z0-9]{20,}`, String.raw`\bgithub_pat_\w{20,}`, String.raw`\bglpat-[\w-]{20,}`,
  String.raw`\bxox[abposr]-[\w-]{10,}`, String.raw`\bsk-[\w-]{20,}`, String.raw`\bsk_(?:live|test)_\w{16,}`,
  String.raw`\bwhsec_\w{16,}`, String.raw`\bAIza[\w-]{35}`, String.raw`\bAKIA[0-9A-Z]{16}\b`,
  String.raw`\beyJ[\w-]+\.eyJ[\w-]+`, String.raw`otpauth://`,
].join('|'))
// Upper-case base32, grouped in fours or unbroken, holding a base32 digit (2-7): all-caps prose has none.
const BASE32_RUN = new RegExp(String.raw`\b(?=[A-Z2-7 -]*[2-7])(?:[A-Z2-7]{4}[ -]){3,}[A-Z2-7]{4}\b|\b(?=[A-Z2-7]*[2-7])[A-Z2-7]{16,}\b`)
const HEX_RUN = /\b[0-9a-f]{32,}\b/i

/** What the ⚠ says on hover. */
export const SECRET_SHAPE_WARNING =
  'This looks like a secret. Every task that may use this credential can read the description: put secrets in a secret field.'

/** Whether `text` holds something shaped like a secret (see above). */
export function looksLikeSecret(text: string): boolean {
  return [LABELLED, PRIVATE_KEY, SELF_IDENTIFYING, BASE32_RUN, HEX_RUN].some(pattern => pattern.test(text))
}

/** The warning for a credential description, or undefined when nothing in it looks secret. */
export function secretShapeWarning(text: string): string | undefined {
  return looksLikeSecret(text) ? SECRET_SHAPE_WARNING : undefined
}
