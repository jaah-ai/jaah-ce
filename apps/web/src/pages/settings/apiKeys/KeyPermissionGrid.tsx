import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import ButtonGroup from '@mui/material/ButtonGroup'
import Typography from '@mui/material/Typography'
import HelpLabel from '../../../ui/HelpLabel'
import { PHONE, tapTargetSx } from '../../../ui/listStyles'
import {
  PERMISSION_CELL_SX,
  PERMISSION_HEADING_SX,
  PERMISSION_ROW_MIN_HEIGHT,
  PERMISSION_ROW_RULE_SX,
} from '../../../ui/permissionGridStyles'
import SettingCheckbox from '../../../ui/SettingCheckbox'
import { ACTION_LABELS, GRID_ACTIONS, preset, presetOf, type GridRow, type GridValue, type Preset } from './keyPermissions'

/* What an API key may do: one row per feature (the catalog's own labels), a box per View/Create/Edit/Delete, and
 * the Administration row. Presets above set the whole grid in one click. The look is the console's permission
 * grid (`ui/permissionGridStyles.ts`). On a phone every feature is a card of its own with four labelled boxes —
 * never a squeezed or sideways-scrolled table; labels wrap, never truncate. */

const PRESETS: readonly { id: Preset; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'read', label: 'Read-only' },
  { id: 'none', label: 'None' },
]

const COLUMNS = 'minmax(10rem, 1fr) repeat(4, 4.5rem)'

const ORG_ADMIN_HELP = 'Acts with the owner’s admin rank: people, groups, access and settings.'

const ROW_SX = {
  display: 'grid',
  gridTemplateColumns: COLUMNS,
  alignItems: 'center',
  columnGap: '1rem',
  minHeight: PERMISSION_ROW_MIN_HEIGHT,
  [PHONE]: {
    // A card per feature: the name across the top, its boxes two by two under it, each with its word.
    gridTemplateColumns: '1fr 1fr',
    rowGap: 0,
    py: 1,
  },
} as const

/* A box's word beside it: hidden on a wide screen (the column heading says it), shown on a phone. */
const CELL_WORD_SX = { display: 'none', [PHONE]: { display: 'inline' } } as const

const CELL_SX = {
  ...PERMISSION_CELL_SX,
  [PHONE]: { justifyContent: 'flex-start' },
} as const

const LABEL_SX = {
  fontWeight: 500,
  minWidth: 0,
  overflowWrap: 'anywhere',
  [PHONE]: { gridColumn: '1 / -1' },
} as const

export default function KeyPermissionGrid({
  rows,
  value,
  onChange,
  disabled = false,
}: {
  rows: readonly GridRow[]
  value: GridValue
  onChange: (next: GridValue) => void
  disabled?: boolean
}): JSX.Element {
  const inForce = presetOf(rows, value)

  const toggle = (key: string, checked: boolean) => {
    const ticked = new Set(value.ticked)
    if (checked) ticked.add(key)
    else ticked.delete(key)
    onChange({ ...value, ticked })
  }

  return (
    <Box data-testid="key-permission-grid" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      <ButtonGroup size="small" variant="outlined" aria-label="Presets" data-testid="key-permission-presets">
        {PRESETS.map(p => (
          <Button
            key={p.id}
            variant={inForce === p.id ? 'contained' : 'outlined'}
            aria-pressed={inForce === p.id}
            disabled={disabled}
            onClick={() => onChange(preset(rows, p.id))}
            data-testid={`key-preset-${p.id}`}
            sx={tapTargetSx}
          >
            {p.label}
          </Button>
        ))}
      </ButtonGroup>

      <Box role="group" aria-label="Permissions">
        <Box sx={{ ...ROW_SX, minHeight: 28, [PHONE]: { display: 'none' } }} aria-hidden>
          <span />
          {GRID_ACTIONS.map(action => (
            <Typography key={action} variant="caption" sx={PERMISSION_HEADING_SX}>{ACTION_LABELS[action]}</Typography>
          ))}
        </Box>
        {rows.map(row => (
          <Box key={row.feature} role="group" aria-label={row.label} data-testid="key-permission-row"
            data-feature={row.feature} sx={{ ...ROW_SX, ...PERMISSION_ROW_RULE_SX }}>
            <Typography sx={LABEL_SX} data-testid="key-permission-label">{row.label}</Typography>
            {GRID_ACTIONS.map(action => {
              const key = row.keys[action]
              if (!key) return <span key={action} />
              return (
                <Box key={action} component="label" sx={CELL_SX}>
                  <SettingCheckbox
                    name={key}
                    label={`${ACTION_LABELS[action]} ${row.label.toLowerCase()}`}
                    checked={value.ticked.has(key)}
                    disabled={disabled}
                    onChange={checked => toggle(key, checked)}
                    labelHidden
                    testId={`key-permission-${key}`}
                  />
                  <Typography component="span" variant="body2" sx={CELL_WORD_SX}>{ACTION_LABELS[action]}</Typography>
                </Box>
              )
            })}
          </Box>
        ))}
        <Box role="group" aria-label="Administration" data-testid="key-permission-org-admin-row"
          sx={{ ...ROW_SX, ...PERMISSION_ROW_RULE_SX }}>
          <Typography component="span" sx={LABEL_SX}>
            <HelpLabel label="Administration" help={ORG_ADMIN_HELP} />
          </Typography>
          <Box component="label" sx={CELL_SX}>
            <SettingCheckbox
              name="org_admin"
              label="Administration"
              help={ORG_ADMIN_HELP}
              checked={value.orgAdmin}
              disabled={disabled}
              onChange={checked => onChange({ ...value, orgAdmin: checked })}
              labelHidden
              testId="key-permission-org-admin"
            />
            <Typography component="span" variant="body2" sx={CELL_WORD_SX}>Allowed</Typography>
          </Box>
        </Box>
      </Box>
    </Box>
  )
}
