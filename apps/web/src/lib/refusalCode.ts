/* A refusal's machine code: here, not in the sign-up flow (`pages/signup/signupFlow.tsx` re-exports it), because the
 * Terms gate reads it too and ships where sign-up does not (the community edition, `signup`). */
import { ApiError } from '../api'

/** A refusal's machine code (`detail.code`), or null. */
export function refusalCode(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null
  const code = (err.detail as { code?: unknown } | null)?.code
  return typeof code === 'string' ? code : null
}
