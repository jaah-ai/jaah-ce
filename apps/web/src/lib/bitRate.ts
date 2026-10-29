/** The network-rate stepping `components/NodeLoadMeter.tsx::formatRate` and `components/NodeMetrics.tsx::formatRateTick`
 *  share: the wire's bytes per second as bits per second (the unit network speed is quoted in), stepped
 *  through the decimal prefixes until the value is under `roll` — 1000 for a tick, 999.5 for prose, where a
 *  value that ROUNDS to 1000 is spelled in the next unit ("1.00 kbit/s"). The caller words the figure. */
export function scaleBitRate(bytesPerSec: number, roll = 1000): { value: number; prefix: string } {
  const prefixes = ['', 'k', 'M', 'G', 'T']
  let value = Math.max(0, bytesPerSec) * 8
  let step = 0
  while (value >= roll && step < prefixes.length - 1) {
    value /= 1000
    step += 1
  }
  return { value, prefix: prefixes[step] }
}
