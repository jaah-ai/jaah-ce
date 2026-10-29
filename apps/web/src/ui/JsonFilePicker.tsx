import { type JSX, useRef, useState } from 'react'
import Button from '@mui/material/Button'
import { tapTargetSx } from './listStyles'
import { VISUALLY_HIDDEN } from './visuallyHidden'

/* ONE JSON key-file picker for the app (an account export, `pages/credentials/AccountTransfer.tsx`; a service-account
 * key, `pages/settings/email/ServiceAccountDialog.tsx`): the labelled button over a hidden file input, and the hook
 * that reads what was picked. */

/** The file's text through `FileReader`, which every browser (and jsdom) has; `Blob.text()` is newer. */
function readText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result ?? ''))
    reader.onerror = () => reject(reader.error ?? new Error('read failed'))
    reader.readAsText(file)
  })
}

/** The picked file's name and the `pick` the button calls. `clear` runs on every pick before any read; `text` gets
 * what was read and `fail` the file whose read (or `text`) threw — those two only for the latest pick: an earlier
 * file's read finishing last must not replace it. */
export function useJsonFilePick({ clear, text, fail }: {
  clear: () => void
  text: (text: string) => void
  fail: (file: File) => void
}): [string, (file: File | null) => Promise<void>] {
  const pickSeq = useRef(0)
  const [fileName, setFileName] = useState('')

  async function pick(file: File | null) {
    const seq = ++pickSeq.current
    clear()
    setFileName(file?.name ?? '')
    if (!file) return
    try {
      const content = await readText(file)
      if (seq === pickSeq.current) text(content)
    } catch {
      if (seq === pickSeq.current) fail(file)
    }
  }

  return [fileName, pick]
}

/** The button that opens the chooser — `label` on it, `testId` on the input a test uploads to. */
export function JsonFilePicker({ label, testId, onPick }: {
  label: string
  testId: string
  onPick: (file: File | null) => void | Promise<void>
}): JSX.Element {
  return (
    <Button variant="outlined" component="label" sx={tapTargetSx}>
      {label}
      <input
        style={VISUALLY_HIDDEN}
        type="file"
        accept=".json,application/json"
        data-testid={testId}
        onChange={e => {
          const file = e.target.files?.[0] ?? null
          // Cleared, so choosing the same (fixed) file again fires `change` again.
          e.target.value = ''
          void onPick(file)
        }}
      />
    </Button>
  )
}
