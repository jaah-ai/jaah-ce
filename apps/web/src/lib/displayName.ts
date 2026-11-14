/** A code's name from the browser (`Intl.DisplayNames`, in `locale`, else the viewer's language); the code itself
 *  where the runtime has no name for it, or `of` throws (a string that is not such a code). */
export function displayNames(type: 'region' | 'language', locale?: string): (code: string) => string {
  let names: Intl.DisplayNames | null | undefined
  return code => {
    try {
      names ??= typeof Intl.DisplayNames === 'function' ? new Intl.DisplayNames(locale, { type }) : null
      return names?.of(code) ?? code
    } catch {
      return code
    }
  }
}
