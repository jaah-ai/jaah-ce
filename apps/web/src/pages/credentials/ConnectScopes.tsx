import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import HelpLabel, { ExplainMoreButton } from '../../ui/HelpLabel'
import SettingCheckbox from '../../ui/SettingCheckbox'
import type { OAuthConnectScope } from './api'
import { FONT_WEIGHT } from '../../theme'

/* The scopes a connect asks for (`OAuthConnectSpec.scopes`): the required ones listed in the legend's pop-up (More), each
 * optional one a box. Controlled — the page owns the picks and sends the ticked names with the start POST.
 * Drawn only for a catalogue with an optional entry: with none there is nothing to choose. */

/** The optional scopes a connect opens with: all of them, or — once a token is stored — exactly those in its
 *  saved `scope` (space- or comma-separated), so a reconnect does not quietly widen a narrowed grant. */
export function initialScopePicks(scopes: OAuthConnectScope[], savedScope: string | undefined): Set<string> {
  const optional = scopes.filter(scope => scope.optional).map(scope => scope.name)
  const saved = savedScope?.trim() ? new Set(savedScope.split(/[\s,]+/)) : null
  return new Set(saved ? optional.filter(name => saved.has(name)) : optional)
}

/** The catalogue when it offers a choice (an optional scope), else null: nothing to draw, nothing to send. */
export function scopeChoiceOf(scopes: OAuthConnectScope[] | undefined): OAuthConnectScope[] | null {
  return scopes?.some(scope => scope.optional) ? scopes : null
}

export default function ConnectScopes({ scopes, picked, onChange, disabled }: {
  scopes: OAuthConnectScope[]
  picked: Set<string>
  onChange: (next: Set<string>) => void
  disabled: boolean
}) {
  const required = scopes.filter(scope => !scope.optional)
  const optional = scopes.filter(scope => scope.optional)
  const help = {
    tip: 'Unticked ones are not asked of the vendor.',
    more: required.length > 0 ? `Always included: ${required.map(scope => scope.label).join(', ')}` : undefined,
  }

  function toggle(name: string, on: boolean) {
    const next = new Set(picked)
    if (on) next.add(name)
    else next.delete(name)
    onChange(next)
  }

  return (
    <Box
      component="fieldset"
      data-testid="credential-connect-scopes"
      sx={{ m: 0, mt: 1, p: 0, border: 0, minWidth: 0, position: 'relative' }}
    >
      <Typography
        component="legend"
        variant="caption"
        sx={{ p: 0, fontWeight: FONT_WEIGHT.semibold, color: 'text.secondary', display: 'flex', alignItems: 'center', gap: 0.5 }}
      >
        <HelpLabel label="Permissions" help={help} />
      </Typography>
      <ExplainMoreButton label="Permissions" help={help} />
      <Box sx={{ display: 'flex', flexWrap: 'wrap', columnGap: 2, minWidth: 0 }}>
        {optional.map(scope => (
          <SettingCheckbox
            key={scope.name}
            name={`connect-scope-${scope.name}`}
            label={scope.label}
            help="Ticked: Connect asks the vendor for this permission."
            checked={picked.has(scope.name)}
            disabled={disabled}
            onChange={on => toggle(scope.name, on)}
            labelPlacement="end"
            testId="credential-connect-scope"
          />
        ))}
      </Box>
    </Box>
  )
}
