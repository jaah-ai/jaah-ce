import { humanize } from '../../lib/humanize'
import type { OperatorSetting } from '../../types'
import type { ChoiceHelp, Explain } from '../../ui/explain'

/* A catalogue knob's explanation as `ui/HelpLabel` takes it — one builder, so Settings and Org → Settings show
 * the same tooltip and pop-up for the same knob. */

/** A choice token's display words: the paired label when the catalogue declares one, else humanised. */
export function choiceLabel(s: Pick<OperatorSetting, 'choices' | 'choice_labels'>, token: string): string {
  const i = s.choices?.indexOf(token) ?? -1
  return (i >= 0 ? s.choice_labels?.[i] : undefined) || humanize(token)
}

/** The tooltip, the pop-up's paragraph and its docs page (`Knob.help`/`more`/`docs`). */
export function knobExplain(s: Pick<OperatorSetting, 'help' | 'more' | 'docs'>): Explain {
  return { tip: s.help, more: s.more ?? undefined, docs: s.docs ?? undefined }
}

/** One pop-up line per choice (`Knob.choice_help`, paired with `choices`); none when the knob carries none. */
export function knobChoices(s: Pick<OperatorSetting, 'choices' | 'choice_labels' | 'choice_help'>): ChoiceHelp[] | undefined {
  if (!s.choices?.length || !s.choice_help?.length) return undefined
  return s.choices.flatMap((token, i) => s.choice_help?.[i] ? [{ label: choiceLabel(s, token), help: s.choice_help[i] }] : [])
}
