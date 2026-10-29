import type { NodeOs } from './nodeOs'

/* HOW LONG A LAUNCHED MACHINE TAKES TO COME ONLINE — the one boot estimate the order countdown
 * (`components/addNode/orderEta.ts`) and the roster's "Stalled" bound (`nodeStatus.ts`) both start from. Measured
 * on dev: Ubuntu ~3 min; Windows Server m8i.large 8 min; Windows Server m7i.metal-24xl 30 min. A bare-metal
 * instance's firmware and memory checks run before the OS starts. */

const BOOT_S: Record<'ubuntu' | 'windows', { plain: number; metal: number }> = {
  ubuntu: { plain: 180, metal: 600 },
  windows: { plain: 480, metal: 1800 },
}

/** A bare-metal instance type (`m7i.metal-24xl`, `c5.metal`). */
export function isMetalType(instanceType: string | null | undefined): boolean {
  return /\.metal(\b|-)/.test(instanceType ?? '')
}

/** Seconds a launched machine of `os` on `instanceType` usually takes from launch to online; an unknown OS or
 *  type reads as Ubuntu on a plain instance. */
export function bootEstimateS(os: NodeOs | null | undefined, instanceType: string | null | undefined): number {
  const row = BOOT_S[os === 'windows' ? 'windows' : 'ubuntu']
  return isMetalType(instanceType) ? row.metal : row.plain
}
