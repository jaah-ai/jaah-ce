import { type JSX, useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import ButtonBase from '@mui/material/ButtonBase'
import Popover from '@mui/material/Popover'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { STEP_ROLES, type Who } from '../../lib/pipeline'
import { userOption } from '../../lib/users'
import type { ProjectMember } from '../../types'
import { matchesAny } from '../../ui/FilterRow'
import { grid, RADIUS, SIZE } from '../../theme'
import { AsyncMultiLookup, CHIP_SX, CHIP_TEXT_SX } from '../../ui/AsyncMultiLookup'
import type { LookupOption } from '../../ui/AsyncLookup'
import SettingCheckbox from '../../ui/SettingCheckbox'
import { tapTargetSx } from '../../ui/listStyles'

/* A set of people on a pipeline step (`lib/pipeline.ts::Who`): the roles and users as chips on one button that opens a
 * picker (role ticks, a member search). Project settings › Pipeline uses it twice per step — who may press it and who
 * may approve it (`PipelineStepPanel.tsx`); `testPrefix` keeps the two apart. An org pipeline template's table
 * (`PipelineEditor.tsx`) uses it for who may press, roles only. */

/* The chips as one button that opens the picker; a phone keeps the 44px tap floor. */
const WHO_BUTTON_SX = {
  display: 'flex', flexWrap: 'wrap', justifyContent: 'flex-start', alignItems: 'center', gap: 0.5, width: '100%',
  minHeight: SIZE.tapTarget, textAlign: 'left', borderRadius: RADIUS.sm, px: 0.5, py: 0.5,
  border: '1px solid', borderColor: 'divider',
  '&:hover:not(.Mui-disabled), &:focus-visible': { bgcolor: 'action.hover' },
} as const
const WHO_CHIP_SX = { ...CHIP_SX, pr: 1, mr: 0 } as const

/** The members as picker rows. One person is one account per org, so two rows can read alike; each of those adds
 *  its org to the hint, so the right account can be picked. */
export function memberOptions(members: ProjectMember[]): LookupOption[] {
  const rows = members.map(m => ({ org: m.org_name, option: userOption(m) }))
  const shown = (o: LookupOption) => `${o.label}\n${o.hint ?? ''}`.toLowerCase()
  const count = new Map<string, number>()
  for (const { option } of rows) count.set(shown(option), (count.get(shown(option)) ?? 0) + 1)
  return rows.map(({ org, option }) => (org && (count.get(shown(option)) ?? 0) > 1
    ? { ...option, hint: [option.hint, org].filter(Boolean).join(' · ') }
    : option))
}

export function WhoPicker({ who, names, title, label, disabled, members, onChange, onName, testPrefix = 'pipeline-step' }: {
  who: Who
  /** The visible label beside the button ("Can press"), kept first in its accessible name. */
  label?: string
  names: Record<string, string>
  /** The picker's heading and the button's name: "Who can merge", "Approvers of merge". */
  title: string
  disabled: boolean
  /** The project's members (`GET /api/projects/{id}/members`, `projects:view`): whom a step may name. Not
   *  `/api/users`, which needs `users:view` a Maintainer lacks and would offer people of other orgs. Absent (an org
   *  pipeline template), roles only. */
  members?: () => Promise<ProjectMember[]>
  onChange: (next: Who) => void
  onName: (option: LookupOption) => void
  /** `pipeline-step` for who may press, `pipeline-approvers` for who may approve. */
  testPrefix?: string
}): JSX.Element {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const roles = STEP_ROLES.filter(r => who.roles.includes(r.value))
  const users = who.users.map(id => ({ key: id, label: names[id] || 'Unknown' }))
  const chips = [...roles.map(r => ({ key: r.value, label: r.label })), ...users]
  const picker = testPrefix === 'pipeline-step' ? 'pipeline-who' : testPrefix
  return (
    <>
      <ButtonBase sx={WHO_BUTTON_SX} disabled={disabled} onClick={e => setAnchor(e.currentTarget)}
        aria-label={`${label ? `${label}, ` : ''}${title}: ${chips.map(c => c.label).join(', ') || 'nobody'}`} aria-haspopup="dialog"
        data-testid={`${testPrefix}-who`}>
        {chips.length
          ? chips.map(c => (
            <Box key={c.key} component="span" sx={WHO_CHIP_SX} data-testid={`${testPrefix}-who-chip`}>
              <Box component="span" sx={CHIP_TEXT_SX}>{c.label}</Box>
            </Box>
          ))
          : <Box component="span" sx={{ color: 'text.secondary' }}>Nobody</Box>}
      </ButtonBase>
      <Popover open={anchor !== null} anchorEl={anchor} onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }} transformOrigin={{ vertical: 'top', horizontal: 'left' }}
        slotProps={{ paper: { sx: { p: 2, width: grid(40), maxWidth: '100%' }, role: 'dialog',
          'aria-label': title, 'data-testid': `${picker}-picker` } as object }}>
        <Typography variant="subtitle2" sx={{ mb: 1 }}>{title}</Typography>
        <Stack spacing={0.5}>
          {STEP_ROLES.map(role => (
            <SettingCheckbox key={role.value} name={`${testPrefix}-${role.value}`} label={role.label} labelPlacement="end" dense
              checked={who.roles.includes(role.value)} testId={`${testPrefix}-role-${role.value}`}
              onChange={on => onChange({
                roles: on ? [...who.roles, role.value] : who.roles.filter(x => x !== role.value), users: who.users,
              })} />
          ))}
        </Stack>
        {members && <Box sx={{ mt: 1.5 }}>
          <AsyncMultiLookup id={`${testPrefix}-users`} label="Users" placeholder="Add a member" itemNoun="user" fetchOnFocus
            stacked testId={`${testPrefix}-users`} chipTestId={`${testPrefix}-user`}
            noOptionsText="No matching member"
            value={users}
            fetchOptions={async q => {
              const all = await members()
              return memberOptions(all).filter((o, i) => matchesAny(q, o.label, all[i].email))
            }}
            onChange={next => {
              next.forEach(onName)
              onChange({ roles: who.roles, users: next.map(o => o.key) })
            }} />
        </Box>}
        <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 1.5 }}>
          <Button size="small" onClick={() => setAnchor(null)} sx={tapTargetSx} data-testid={`${picker}-done`}>Done</Button>
        </Box>
      </Popover>
    </>
  )
}

export default WhoPicker
