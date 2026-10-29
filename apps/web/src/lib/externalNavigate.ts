/* Same-frame navigation to another origin, behind a module so tests can `vi.mock` it (jsdom implements no
 * navigation and `window.location` is not reliably reconfigurable).
 * Not for in-app routes (use `RouterLink`/`useNavigate`). The caller validates the URL: pass only a string
 * returned by `lib/safeHref`. No `noopener` needed — this replaces the document, so there is no opener. */
export function navigateExternal(url: string): void {
  window.location.assign(url)
}

