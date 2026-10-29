import { useMemo } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import { DIRECTORY_TAB_PATHS } from '../routeGroups'
import type { GitCredential } from '../pages/credentials/api'
import CredentialSelect, { type CredentialOption } from '../ui/CredentialSelect'
import { credentialMark } from '../ui/credentialMarks'
import { matchesAny } from '../ui/FilterRow'
import FormField from '../ui/FormField'

/* ── WHICH CREDENTIAL THIS ACCOUNT'S RUNNERS PUSH WITH ──────────────────────────────────
 *
 * /users/:id's control (somebody else's account). /profile has one row per forge instead
 * (`components/GitConnectionsCard`), whose routes are self-service only. `ui/CredentialSelect` renders the
 * picker; this file keeps the JUDGEMENT — why a stored id is missing from the list and whether that is a
 * fault — in its helper text. The roster is what the EDITED account can reach
 * (`GET /api/users/{id}/git-credentials`), else a superadmin could store a selection
 * `git_access.resolve_for_user` refuses at the first push.
 * A SELECTION IS A PREFERENCE, NOT AN AUTHORIZATION: the server re-checks it against live grants on every
 * resolution, so a deleted or ungranted row stays shown as selected rather than being swapped or cleared.
 */

/** A module constant, not a destructure default (a fresh array per render breaks memoisation). */
const EMPTY_ROSTER: readonly GitCredential[] = []

/* One hint per roster state (as `UserSettingsFields`' focused-project dropdown). Not-told-yet differs from
 * told-nothing: an in-flight fetch must not claim "none". Worded for both subjects ("this account"). */
const HINT_READY =
  'Empty: pushes are refused.'
const HINT_LOADING = 'Loading the credential list…'
const HINT_UNAVAILABLE =
  'The credential list is unavailable, so this cannot be changed here. The saved selection is untouched.'
const HINT_EMPTY = (
  <>No GitHub or GitLab credential yet. Add one in <Link component={RouterLink} to={DIRECTORY_TAB_PATHS.credentials}>Secrets</Link>.</>
)
/* Appended, not substituted: the displayed row is no longer reachable, so runners already fail; names
 * the fix. */
const HINT_STALE =
  ' The selection below is no longer available to this account — it was deleted, or the grant on it was revoked. Pick another; until then pushes are refused.'

/** An empty box reads as a STATE ("Not connected"), shown as the placeholder while focused; unfocused,
 *  `HINT_READY`'s closing sentence says it, so a deliberate disconnection does not look unfilled. */
const NONE_LABEL = 'Not connected'

/** One roster row as an option; the hint is the server's `type_label` (a `github` and a `gitlab` row may
 *  share a name). */
function toOption(row: GitCredential): CredentialOption {
  return { id: row.id, name: row.name, hint: row.type_label, icon: credentialMark(row.type, row.type_label) }
}

function GitAccessField({
  value,
  onChange,
  credentials = null,
  unavailable = false,
  disabled = false,
  idPrefix,
}: {
  /** The selected `credentials.id`, or null = connected nothing (a real state). */
  value: string | null
  /** Next selection; `null` when cleared (the wire field is a nullable uuid, never `''`). */
  onChange: (next: string | null) => void
  /** `null` = not told yet (in flight), `[]` = the server says none, else the roster. Never collapse `null`. */
  credentials?: readonly GitCredential[] | null
  /** We could not ask at all. The control goes read-only and says so; the stored value is untouched. */
  unavailable?: boolean
  /** A write is in flight on this surface. */
  disabled?: boolean
  /** Namespaces the control id. */
  idPrefix: string
}) {
  const rows = credentials ?? EMPTY_ROSTER
  const loading = credentials === null && !unavailable

  /* The roster is in memory, so `CredentialSelect`'s seam is a FILTER, not a fetch (no second answer to
     disagree with the page). No `AbortSignal`: nothing is in flight. */
  const fetchOptions = async (query: string): Promise<CredentialOption[]> => {
    // Match name AND type label — both are rendered.
    return rows.filter(row => matchesAny(query, `${row.name} ${row.type_label}`)).map(toOption)
  }

  /* The stored selection's name from the in-hand roster, so the field never shows a bare id. */
  const selectedRow = value === null ? undefined : rows.find(row => row.id === value)
  // Memoised: `CredentialSelect` watches it by identity.
  const selected = useMemo(() => (selectedRow ? toOption(selectedRow) : null), [selectedRow])

  /* A selection missing from the roster is still SHOWN (`ui/CredentialSelect`'s invariant). Only with a
     roster in hand is it "gone"; in flight or after a failed fetch we know nothing and must not invite
     clearing a good selection. */
  const missing = value !== null && selectedRow === undefined
  const stale = missing && credentials !== null

  const hint = unavailable
    ? HINT_UNAVAILABLE
    : loading
      ? HINT_LOADING
      : stale
        ? HINT_READY + HINT_STALE
        : rows.length === 0
          ? HINT_EMPTY
          : HINT_READY

  return (
    <Box>
      <FormField label="Git Access" hint={hint}>
        <CredentialSelect
          id={`${idPrefix}-git-credential`}
          value={value}
          onChange={onChange}
          fetchOptions={fetchOptions}
          selected={selected}
          placeholder={NONE_LABEL}
          // Read-only rather than hidden when we could not ask: the stored answer is still shown.
          disabled={disabled || unavailable || loading}
        />
      </FormField>
    </Box>
  )
}

export default GitAccessField
