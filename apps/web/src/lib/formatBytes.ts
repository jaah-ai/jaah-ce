/** A byte count as a short human string — `25 B`, `4.0 KB`, `28.6 MB`, `787.4 MB`, `3.0 GB`; with
 * `{ iec: true }`, `1.0 KiB`, `28.6 MiB`, `787 MiB`, `3.0 GiB`.
 *
 * THE ONE HOME of byte formatting. Binary units (1024) in both spellings, as `MAX_ATTACHMENT_SIZE` counts;
 * the ladder stops at TB/TiB (a petabyte reads `1024.0 TB`, and nothing here measures one), so a
 * total never reads `3072.0 MB`.
 *
 * DEFAULT (`KB`…`TB`): ONE DECIMAL FROM KB UP (`4.0 KB` and `4.1 KB` are one width). Width claims callers
 * rely on hold: `components/tasks/TaskArtifacts.tsx` prices nine characters (`1023.9 KB`), and every band
 * above MB is shorter.
 *
 * IEC (`KiB`…`TiB`, `pages/audit/EfficiencyTab.tsx`'s bytes scanned, as the Athena console reports them):
 * a whole byte count, one decimal below 10 and none from 10 up (`1.5 KiB`, `787 MiB`). */
const UNITS = ['KB', 'MB', 'GB', 'TB'] as const
const IEC_UNITS = ['KiB', 'MiB', 'GiB', 'TiB'] as const

export function formatBytes(bytes: number, { iec = false }: { iec?: boolean } = {}): string {
  if (bytes < 1024) return `${iec ? Math.max(0, Math.round(bytes)) : bytes} B`
  const units = iec ? IEC_UNITS : UNITS
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(iec && value >= 10 ? 0 : 1)} ${units[unit]}`
}

/** A kB figure (`/proc/meminfo`, `/proc/<pid>`, the disk sizes a node reports) in the default spelling. */
export const kbSize = (kb: number): string => formatBytes(kb * 1024)
