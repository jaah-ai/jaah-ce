/* The largest file the viewer's org may upload, in bytes: `org_quotas.file_cap_bytes` (override → plan → the
 * Operator Settings knob), delivered on the identity payload as `admin.max_file_bytes`
 * (`routes/auth.py::_admin_out_bound`). Never above `MAX_ATTACHMENT_SIZE`, the ceiling the server signs;
 * absent (old snapshot, first paint) it is that ceiling, and the server still refuses with its own 413. */
import { useAuth } from '../auth'
import { MAX_ATTACHMENT_SIZE } from '../generated/inlinePreviewTypes'

/** The payload's value, held to the ceiling, or the ceiling. */
export function resolveMaxFileBytes(raw: unknown): number {
  if (!Number.isFinite(raw as number) || (raw as number) <= 0) return MAX_ATTACHMENT_SIZE
  return Math.min(raw as number, MAX_ATTACHMENT_SIZE)
}

/** The viewer's org's per-file cap, for pick-time checks. */
export function useMaxFileBytes(): number {
  const { admin } = useAuth()
  return resolveMaxFileBytes(admin?.max_file_bytes)
}
