import { useId, useMemo, useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { useSubmit } from '../../hooks/useSubmit'
import { formatBytes } from '../../lib/formatBytes'
import Banner from '../../ui/Banner'
import { putTriggerState } from './api'

/* The preflight's state document (R10): a JSON object the preflight reads each run and may replace. The
 * server is the authority (`PUT /api/triggers/{id}/state` refuses a non-object or one over 64 KiB); this
 * validates live so Save is offered only for what it would accept. */

/** `routes/triggers.py::MAX_STATE_BYTES`. */
const MAX_STATE_BYTES = 65536

function seedOf(document: Record<string, unknown>): string {
  return JSON.stringify(document, null, 2)
}

type Parsed =
  | { ok: true; value: Record<string, unknown>; bytes: number; unsafe: boolean }
  | { ok: false; error: string }

/* A whole number past 2^53 cannot survive a browser round trip (Slack `ts`-style or snowflake cursors): the read
 * already rounded it, and a Save would write the rounded value back over every such key, touched or not. */
function hasUnsafeInteger(value: unknown): boolean {
  if (typeof value === 'number') return Number.isInteger(value) && !Number.isSafeInteger(value)
  if (Array.isArray(value)) return value.some(hasUnsafeInteger)
  if (value !== null && typeof value === 'object') return Object.values(value).some(hasUnsafeInteger)
  return false
}

function parse(text: string): Parsed {
  let value: unknown
  try {
    value = JSON.parse(text)
  } catch (err) {
    return { ok: false, error: `Not valid JSON: ${err instanceof Error ? err.message : String(err)}` }
  }
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return { ok: false, error: 'Must be a JSON object ({ … }).' }
  }
  const bytes = pythonDumpsLength(value)
  return { ok: true, value: value as Record<string, unknown>, bytes, unsafe: hasUnsafeInteger(value) }
}

/* The server's measure, exactly: `len(json.dumps(document).encode())` — Python's default separators
 * (`", "` and `": "`) and `ensure_ascii`, so every non-ASCII code unit is a six-byte `\uXXXX` escape and the
 * output is pure ASCII (its length IS its byte count). A compact count would read "fits" for a document the
 * server refuses near the cap. Numbers are counted as JS prints them, close enough for a 64 KiB budget. */
export function pythonDumpsLength(value: unknown): number {
  if (value === null) return 4
  if (typeof value === 'string') {
    let n = 2
    for (const char of JSON.stringify(value).slice(1, -1)) n += char.charCodeAt(0) > 0x7f ? 6 : 1
    // `JSON.stringify` keeps astral characters as one surrogate pair (two code units, two escapes in Python).
    return n + [...value].filter(ch => ch.length === 2).length * 6
  }
  if (typeof value === 'number' || typeof value === 'boolean') return JSON.stringify(value).length
  if (Array.isArray(value)) {
    if (value.length === 0) return 2
    return 2 + value.reduce<number>((sum, item) => sum + pythonDumpsLength(item), 0) + 2 * (value.length - 1)
  }
  const entries = Object.entries(value as Record<string, unknown>)
  if (entries.length === 0) return 2
  return 2 + entries.reduce((sum, [key, item]) => sum + pythonDumpsLength(key) + 2 + pythonDumpsLength(item), 0)
    + 2 * (entries.length - 1)
}

/* `triggerId` is the event automation's trigger row (`automation.legacy.id`): the state route is the trigger's. */
export default function StateDocumentEditor({
  triggerId,
  document,
  onSaved,
}: {
  triggerId: string
  document: Record<string, unknown>
  onSaved: (document: Record<string, unknown>) => void
}) {
  const sizeId = useId()
  const seed = seedOf(document)
  const [text, setText] = useState(seed)
  const { busy: saving, formError: error, setFormError: setError, run } = useSubmit()
  const parsed = useMemo(() => parse(text), [text])
  const tooBig = parsed.ok && parsed.bytes > MAX_STATE_BYTES
  const unsafe = parsed.ok && parsed.unsafe
  const errorId = useId()
  const dirty = text !== seed
  const empty = parsed.ok && Object.keys(parsed.value).length === 0

  async function save() {
    if (!parsed.ok || tooBig || unsafe) return
    await run('Could not save the state document', async () => {
      const next = (await putTriggerState(triggerId, parsed.value)).state_document ?? parsed.value
      setText(seedOf(next))
      onSaved(next)
    })
  }

  return (
    <Box data-testid="trigger-state-editor" sx={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0 }}>
      {empty && (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          The state document is empty.
        </Typography>
      )}
      <TextField
        label="State document"
        multiline
        minRows={4}
        maxRows={24}
        value={text}
        onChange={e => setText(e.target.value)}
        error={!parsed.ok || tooBig}
        slotProps={{
          htmlInput: { spellCheck: false, 'aria-describedby': parsed.ok ? sizeId : errorId },
        }}
        sx={{ '& textarea': { fontFamily: theme => theme.typography.mono.fontFamily, fontSize: '0.8125rem' } }}
      />
      {!parsed.ok && (
        <Typography id={errorId} variant="body2" color="error" data-testid="trigger-state-error" sx={{ overflowWrap: 'anywhere' }}>
          {parsed.error}
        </Typography>
      )}
      {parsed.ok && (
        <Typography
          id={sizeId}
          variant="caption"
          data-testid="trigger-state-size"
          sx={{ color: tooBig ? 'error.main' : 'text.secondary' }}
        >
          {formatBytes(parsed.bytes)} of 64 KB
        </Typography>
      )}
      {unsafe && (
        <Banner severity="warning">
          This document holds whole numbers too large for a browser to keep exactly, so saving it here would change
          them. Save is off for this document.
        </Banner>
      )}
      {error && <Banner severity="error">{error}</Banner>}
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
        <Button
          variant="contained"
          size="small"
          data-testid="trigger-state-save"
          disabled={!dirty || !parsed.ok || tooBig || unsafe || saving}
          onClick={() => void save()}
        >
          Save
        </Button>
        <Button
          size="small"
          disabled={!dirty || saving}
          onClick={() => {
            setText(seed)
            setError('')
          }}
        >
          Reset
        </Button>
      </Box>
    </Box>
  )
}
