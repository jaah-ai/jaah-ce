import { type FormEvent, useRef, useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import Typography from '@mui/material/Typography'
import { useQueryClient } from '@tanstack/react-query'
import { useAuth } from '../../auth'
import { useSubmit } from '../../hooks/useSubmit'
import { postCeSetup, SETUP_PASSWORD_MIN, useCeSetup } from '../../lib/communityEdition'
import Banner from '../../ui/Banner'
import FormTextField from '../../ui/FormTextField'
import { EntryCard, EntryHeading, EntrySubmit, tapFloorInput } from '../authEntry'

/* THE COMMUNITY EDITION'S FIRST RUN (`/setup`, `App.tsx`): its one user, created once. The CP answers `needs_setup`
 * until a user exists, then refuses the write (409); `Login.tsx` sends a fresh install here. The session cookie
 * comes with the 201, so the form ends signed in, on `/`. */
export default function CeSetupPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { refresh } = useAuth()
  const setup = useCeSetup()
  const { busy, formError, run } = useSubmit()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const short = password.length > 0 && password.length < SETUP_PASSWORD_MIN
  // This page's own setup just landed: it moves on to `/` itself, never into the redirect below.
  const created = useRef(false)

  // Already set up: this machine has its user, who signs in.
  if (setup.data?.needs_setup === false && !created.current) return <Navigate to="/login" replace />

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (password.length < SETUP_PASSWORD_MIN) return
    void run('Setup failed', async () => {
      await postCeSetup(email.trim(), password)
      created.current = true
      queryClient.setQueryData(['ce', 'setup'], { needs_setup: false })
      await refresh()
      navigate('/', { replace: true })
    })
  }

  return (
    <EntryCard onSubmit={handleSubmit}>
      <EntryHeading title="Set up Jaah">
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          Create the account you sign in with on this machine.
        </Typography>
      </EntryHeading>
      {formError && <Banner severity="error">{formError}</Banner>}
      <FormTextField
        label="Email" required
        id="setup-email"
        testId="setup-email"
        type="email"
        value={email}
        onChange={setEmail}
        autoFocus
        autoComplete="username"
        margin="none"
        sx={tapFloorInput}
      />
      <FormTextField
        label="Password" required
        id="setup-password"
        testId="setup-password"
        type="password"
        value={password}
        onChange={setPassword}
        autoComplete="new-password"
        margin="none"
        hint={`At least ${SETUP_PASSWORD_MIN} characters`}
        error={short ? `At least ${SETUP_PASSWORD_MIN} characters` : undefined}
        sx={tapFloorInput}
      />
      <EntrySubmit type="submit" disabled={busy || setup.isPending} busy={busy}>
        {busy ? 'Creating…' : 'Create account'}
      </EntrySubmit>
    </EntryCard>
  )
}
