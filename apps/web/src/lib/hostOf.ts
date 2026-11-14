/** `https://app.example.com/x` -> `app.example.com`; the raw value when it does not parse. */
export function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}
