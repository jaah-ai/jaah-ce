import type { OperatorSetting } from '../../types'

const GROUPED = new Intl.NumberFormat('en-US', { maximumFractionDigits: 20 })
const DECIMAL = /^-?\d+(\.\d+)?$/

/** A number knob's stored value as words: a QUANTITY (a knob with a unit — "86,400 s", "8,192 MiB") has its
 *  thousands grouped, so "Default ·" and "Still saved:" read at a glance. A unit-less number may be an
 *  identifier ("Auto-assign floor" 20800 is a port) and is left as typed, as is any `str` or non-numeric value. */
export function knobNumber(s: Pick<OperatorSetting, 'kind' | 'unit'>, text: string): string {
  return (s.kind === 'int' || s.kind === 'float') && s.unit && DECIMAL.test(text) ? GROUPED.format(Number(text)) : text
}
