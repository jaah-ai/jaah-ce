import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { resolveDefaultTheme, type ColorScheme } from '../lib/colorScheme'
import type { UserSettingsDraft } from '../lib/userSettings'
import FormTextField from '../ui/FormTextField'
// The 44px phone floor from its single home (`ui/listStyles`), for the theme chips.
import { tapTargetSx } from '../ui/listStyles'
import SegmentedControl, { type SegmentedOption } from '../ui/SegmentedControl'
import SettingCheckbox from '../ui/SettingCheckbox'

/* The user-settings controls, rendered once for both surfaces (`/profile` Settings card and a superadmin's
 * `/users/:id` Settings tab) so they cannot drift; `lib/userSettings.ts` holds the field set, hydrate and
 * write shape.
 * Presentation only: no state, no fetch, no endpoint — "which surface am I on" must never become a branch.
 * The project roster is a PROP: `/profile` offers the signed-in user's own, `/users/:id` the EDITED user's
 * (the admin's own would offer a focus the user cannot see, which does nothing). */

/** A project the focused-project dropdown can offer. */
export type FocusableProject = { id: number; name: string }

/* "Default" names what it resolves to, so the choice is never a guess; `defaultTheme` is the operator's. */
function themeOptions(defaultTheme: ColorScheme): SegmentedOption<UserSettingsDraft['theme']>[] {
  return [
    { value: 'default', label: `Default (${defaultTheme === 'dark' ? 'Dark' : 'Light'})` },
    { value: 'light', label: 'Light' },
    { value: 'dark', label: 'Dark' },
  ]
}

/** The Theme control alone — the Settings card's own, and the Live Cast viewer's profile menu (`Sidebar`). */
export function ThemeField({
  value,
  onChange,
  defaultTheme,
  disabled = false,
}: {
  value: UserSettingsDraft['theme']
  onChange: (next: UserSettingsDraft['theme']) => void
  /** What "Default" resolves to (`admin.default_theme`); absent = the fallback. */
  defaultTheme?: string
  disabled?: boolean
}): JSX.Element {
  return (
    <Box>
      <Typography variant="body2" component="span" sx={{ display: 'block', mb: 0.5 }}>Theme</Typography>
      {/* `color="primary"`: MUI's standard selected state is a faint grey, and LIGHT vs DARK must be unmistakable. */}
      <SegmentedControl
        value={value}
        options={themeOptions(resolveDefaultTheme(defaultTheme))}
        ariaLabel="Theme"
        disabled={disabled}
        color="primary"
        buttonSx={tapTargetSx}
        onChange={onChange}
      />
    </Box>
  )
}

const FOCUSED_PROJECT_HINT = 'Where Jaah opens, and where a task created on All projects goes.'
const FOCUSED_PROJECT_HINT_UNAVAILABLE = 'The project list could not be loaded, so this cannot be changed here.'

function UserSettingsFields({
  value,
  onChange,
  projects,
  projectsUnavailable = false,
  disabled = false,
  idPrefix,
  defaultTheme,
}: {
  value: UserSettingsDraft
  /** Called with the WHOLE next draft, so a host page holds one piece of state per surface. */
  onChange: (next: UserSettingsDraft) => void
  /**
   * Locks the controls while the host's save is in flight — a SILENT-LOSS guard: a host re-seeding
   * from the written record destroys input that landed mid-save under a green "Saved". Defaults to
   * false (`/users/:id` does not re-seed). Only ever adds a disable reason.
   */
  disabled?: boolean
  /** The projects the edited user can see, in server order. `null` = not told yet; collapsing it into `[]`
   *  would read "None" mid-flight. */
  projects: FocusableProject[] | null
  /** The roster could not be fetched: the dropdown disables and the stored value is saved back untouched. */
  projectsUnavailable?: boolean
  /** Namespaces control `id`s per surface (`'profile'`, `'user'`). */
  idPrefix: string
  /** The operator's default theme (`admin.default_theme`), which "Default" resolves to; absent = the fallback. */
  defaultTheme?: string
}): JSX.Element {
  const { theme, wideMode, focusedProjectId } = value
  const patch = (changes: Partial<UserSettingsDraft>) => onChange({ ...value, ...changes })

  const projectsLoading = projects === null && !projectsUnavailable
  /* A stored id the roster lacks still gets an option: a `<select>` whose value matches nothing paints blank,
   * and the next save would write whatever gets clicked. */
  const storedIsMissing = focusedProjectId !== null && !(projects ?? []).some(p => p.id === focusedProjectId)
  const selectValue = focusedProjectId === null ? '' : String(focusedProjectId)
  const storedLabel = focusedProjectId === null ? 'None' : 'Unknown project'

  return (
    <>
      <ThemeField value={theme} onChange={next => patch({ theme: next })} defaultTheme={defaultTheme} disabled={disabled} />

      <SettingCheckbox
        name={`${idPrefix}-wide-mode`}
        label="Wide mode"
        checked={wideMode}
        disabled={disabled}
        onChange={next => patch({ wideMode: next })}
      />

      {/* A native select, the console's select idiom (the platform picker on phones). */}
      <Box sx={{ pt: 1 }}>
        <FormTextField
          id={`${idPrefix}-focused-project`}
          label="Focused project"
          help={FOCUSED_PROJECT_HINT}
          hint={projectsUnavailable ? FOCUSED_PROJECT_HINT_UNAVAILABLE : undefined}
          select
          /* `inputLabel.shrink` pinned: with the empty-value "None" option MUI would leave the label over it. */
          slotProps={{ inputLabel: { shrink: true } }}
          disabled={disabled || projectsLoading || projectsUnavailable}
          value={selectValue}
          onChange={v => patch({ focusedProjectId: v === '' ? null : Number(v) })}
        >
          {projects === null ? (
            <option value={selectValue}>{projectsLoading ? 'Loading…' : storedLabel}</option>
          ) : (
            <>
              {/* The way back to All projects: a setting that cannot be cleared is a one-way door. */}
              <option value="">None</option>
              {storedIsMissing && <option value={selectValue}>{storedLabel}</option>}
              {projects.map(p => <option key={p.id} value={String(p.id)}>{p.name}</option>)}
            </>
          )}
        </FormTextField>
      </Box>
    </>
  )
}

export default UserSettingsFields
