/** Hands `text` to the browser's download as a file called `name` (a Blob + object URL + a temporary anchor).
 * Nothing leaves the browser. */
export function downloadText(name: string, text: string, type = 'application/json'): void {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  // Later, not at once: some browsers cancel a download whose URL is revoked before it starts.
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
}
