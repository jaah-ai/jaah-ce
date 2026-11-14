/** A task's language picker (`apps/api/app/task_language.py`): ISO 639-1 codes. Names come from the browser
 *  (`Intl.DisplayNames`), so only the codes live here. A detected code outside this list is still offered. */
import { displayNames } from './displayName'

const CODES = (
  'ar bg bn ca cs da de el en es et fa fi fr he hi hr hu id it ja ka kk ko lt lv ms nl no pl pt ro ru sk sl sr ' +
  'sv th tr uk ur uz vi zh'
).split(' ')

/** What a task with no detected language is treated as, as the server reads it. */
export const DEFAULT_LANGUAGE = 'en'

/** The language's English name, or the code itself when the browser has none. */
export const languageName = displayNames('language', 'en')

/** Every offered code by name, plus `current` when the list lacks it. */
export function languageOptions(current: string): { code: string; name: string }[] {
  const codes = CODES.includes(current) ? CODES : [...CODES, current]
  return codes.map(code => ({ code, name: languageName(code) })).sort((a, b) => a.name.localeCompare(b.name))
}
