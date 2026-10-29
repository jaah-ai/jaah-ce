import { useState } from 'react'
import Typography from '@mui/material/Typography'
import FormDialog from '../../ui/FormDialog'
import FormTextField from '../../ui/FormTextField'
import { monoSx } from '../../ui/listStyles'

/* A VERIFY-ONLY check of which secret a sender signs with: a console-created secret is never shown again, so
 * an admin pastes the secret they believe the sender holds, one request's raw body and its signature header,
 * and learns whether they match — without rotating. Everything runs in the browser (WebCrypto); nothing is
 * sent to the server or stored, and the fields live only while the dialog is mounted.
 *
 * The scheme mirrors the server's (`apps/api/app/trigger_secrets.py`): `sha256=<hex HMAC-SHA256(secret, raw
 * body)>`, the same for `X-Jaah-Signature-256` and GitHub's `X-Hub-Signature-256`. The body is NOT trimmed:
 * one changed byte changes the HMAC (a textarea also turns CRLF into LF, so a CRLF body reads as no match). */
export async function signatureMatches(secret: string, body: string, signature: string): Promise<boolean> {
  const enc = new TextEncoder()
  const key = await crypto.subtle.importKey('raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
  const mac = new Uint8Array(await crypto.subtle.sign('HMAC', key, enc.encode(body)))
  const hex = Array.from(mac, b => b.toString(16).padStart(2, '0')).join('')
  return signature.trim().replace(/^sha256=/i, '').toLowerCase() === hex
}

export default function SignatureCheckDialog({ onClose }: { onClose: () => void }) {
  const [secret, setSecret] = useState('')
  const [body, setBody] = useState('')
  const [signature, setSignature] = useState('')
  const [match, setMatch] = useState<boolean | null>(null)
  // Any edit voids the last answer: it was about the old inputs.
  const edit = (set: (v: string) => void) => (v: string) => { set(v); setMatch(null) }

  return (
    <FormDialog
      // A typed secret: a stray backdrop click must not drop it (`FormDialog`'s `modal`).
      modal
      title="Check a signature"
      submitLabel="Check"
      cancelLabel="Close"
      testId="automation-signature-check"
      submitTestId="automation-signature-check-submit"
      submitDisabled={!secret || !signature}
      onSubmit={() => void signatureMatches(secret, body, signature).then(setMatch)}
      onClose={onClose}
    >
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        Checked in your browser. Nothing is sent or saved.
      </Typography>
      <FormTextField
        label="Secret"
        type="password"
        autoComplete="new-password"
        value={secret}
        onChange={edit(setSecret)}
        testId="automation-signature-check-secret"
      />
      <FormTextField
        label="Body"
        hint="CRLF line endings won’t match here."
        multiline
        minRows={3}
        value={body}
        onChange={edit(setBody)}
        testId="automation-signature-check-body"
        slotProps={{ htmlInput: { sx: monoSx } }}
      />
      <FormTextField
        label="Signature"
        placeholder="sha256=…"
        value={signature}
        onChange={edit(setSignature)}
        testId="automation-signature-check-signature"
        slotProps={{ htmlInput: { sx: monoSx } }}
      />
      {match !== null && (
        <Typography variant="body2" sx={{ color: match ? 'success.main' : 'error.main' }} role="status" data-testid="automation-signature-check-result">
          {match ? 'Match.' : 'No match.'}
        </Typography>
      )}
    </FormDialog>
  )
}
