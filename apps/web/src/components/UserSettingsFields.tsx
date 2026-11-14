import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { resolveDefaultTheme, type ColorScheme } from '../lib/colorScheme'
import type { UserSettingsDraft } from '../lib/userSettings'
import { HelpLabel } from '../ui/HelpLabel'
// The 44px phone floor from its single home (`ui/listStyles`), for the theme chips.
import { tapTargetSx } from '../ui/listStyles'
import SegmentedControl, { type SegmentedOption } from '../ui/SegmentedControl'

/* The user-settings controls, rendered once for both surfaces (`/profile` Settings card and a superadmin's
 * `/users/:id` Settings tab) so they cannot drift; `lib/userSettings.ts` holds the field set, hydrate and
 * write shape.
 * Presentation only: no state, no fetch, no endpoint — "which surface am I on" must never become a branch. */

/* "Default" names what it resolves to, so the choice is never a guess; `defaultTheme` is the operator's. */
function themeOptions(defaultTheme: ColorScheme): SegmentedOption<UserSettingsDraft['theme']>[] {
  return [
    { value: 'default', label: `Default (${defaultTheme === 'dark' ? 'Dark' : 'Light'})`, help: 'Follows the console default the operator chose.' },
    { value: 'light', label: 'Light', help: 'Always light colours, whatever the default.' },
    { value: 'dark', label: 'Dark', help: 'Always dark colours, whatever the default.' },
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
      <Typography variant="body2" component="span" sx={{ display: 'block', mb: 0.5 }}>
        <HelpLabel label="Theme" help="Console colours; Default follows the operator's choice." />
      </Typography>
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

function UserSettingsFields({
  value,
  onChange,
  disabled = false,
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
  /** The operator's default theme (`admin.default_theme`), which "Default" resolves to; absent = the fallback. */
  defaultTheme?: string
}): JSX.Element {
  return (
    <ThemeField
      value={value.theme}
      onChange={next => onChange({ ...value, theme: next })}
      defaultTheme={defaultTheme}
      disabled={disabled}
    />
  )
}

export default UserSettingsFields
