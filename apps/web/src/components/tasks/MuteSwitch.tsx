import FormControlLabel from '@mui/material/FormControlLabel'
import Switch from '@mui/material/Switch'
import { useAuth } from '../../auth'
import { MUTED_PREF, readMuted, toggledMuted } from '../../lib/taskMute'
import type { TaskScope } from '../../lib/taskScope'
import { scopeKey } from '../../lib/taskScope'
import HelpLabel from '../../ui/HelpLabel'

/* "Mute for me" on a project's or feature's edit page — the same per-user `task_muted` entry the rail's row menu
 * toggles (`lib/taskMute.ts`). Saved on change, apart from the page's form: it is this user's view, not the
 * project's or feature's data, so it needs no edit right and no Save. */
export default function MuteSwitch({ scope, noun }: { scope: TaskScope; noun: 'project' | 'feature' }) {
  const { admin, updatePreferences } = useAuth()
  const key = scopeKey(scope)
  const muted = readMuted(admin?.preferences)
  const help = `Hides its tasks above this ${noun}, for you only.`
  return (
    <FormControlLabel
      control={
        <Switch
          checked={muted.includes(key)}
          onChange={() => void updatePreferences({ [MUTED_PREF]: toggledMuted(muted, key) })}
          slotProps={{ input: { 'aria-description': help } }}
          data-testid={`mute-${noun}`}
        />
      }
      label={<HelpLabel label="Mute for me" help={help} />}
    />
  )
}
