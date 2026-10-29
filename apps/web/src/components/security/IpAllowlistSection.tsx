import { useEffect, useRef, useState, type JSX, type KeyboardEvent } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { usePolicyQuery } from '../../hooks/usePolicyQuery'
import { useSubmit } from '../../hooks/useSubmit'
import { getIpRanges, ipAllowlistKey, putIpRanges, type IpRange } from '../../lib/ipAllowlist'
import { usePlanSwitch } from '../../lib/planSwitches'
import Banner from '../../ui/Banner'
import { CloseIcon } from '../../ui/icons'
import { monoSx, tapTargetSx } from '../../ui/listStyles'
import SavedNote from '../../ui/SavedNote'
import SubmitButton from '../../ui/SubmitButton'
import RelativeTime from '../../ui/RelativeTime'
import { useConfirm } from '../ConfirmDialog'

/* An org's IP allowlist (`lib/ipAllowlist.ts`), edited as one list and saved whole. Rendered by the tenant's
 * own org page (`pages/settings/ownOrg/OwnOrgPanels.tsx`) and the operator's customer page (`pages/customers/SecurityTab`).
 * A tenant whose plan leaves it out gets nothing drawn and no fetch; the operator is never refused, and
 * alone gets "Clear list" — the break-glass for an org that locked itself out, and no "Your address": the
 * operator is never refused, so their address is beside the point. */

/** A draft row; `saved` is the stored range's last change, absent on a row not saved yet. */
type Row = { key: number; cidr: string; label: string; saved?: Pick<IpRange, 'updated_at' | 'updated_by' | 'updated_by_email'> }

/** Rows as saved: an empty range is dropped, both fields trimmed. */
const cleaned = (rows: readonly { cidr: string; label: string }[]) =>
  rows.map(r => ({ cidr: r.cidr.trim(), label: r.label.trim() })).filter(r => r.cidr)

const sameRows = (a: readonly { cidr: string; label: string }[], b: readonly { cidr: string; label: string }[]) =>
  a.length === b.length && a.every((r, i) => r.cidr === b[i].cidr && r.label === b[i].label)

/* A range is a long unbroken token (an IPv6 /128 is 43 characters): wrap anywhere, never ellipsize. */
const WRAP_SX = { '& textarea': { overflowWrap: 'anywhere', wordBreak: 'break-all' } } as const

export function IpAllowlistSection({ orgId, operator = false }: { orgId: string; operator?: boolean }): JSX.Element {
  const on = usePlanSwitch('ip_allowlist_enabled')
  if (!operator && !on) return <></>
  return <Editor orgId={orgId} operator={operator} />
}

/** Who changed a range: their name, with the email on hover when the name is not already it. */
function ChangedBy({ name, email }: { name: string; email: string | null }): JSX.Element {
  return <Box component="span" title={email && email !== name ? email : undefined}>{name}</Box>
}

function Editor({ orgId, operator }: { orgId: string; operator: boolean }): JSX.Element {
  const client = useQueryClient()
  const { gate, data } = usePolicyQuery({ queryKey: ipAllowlistKey(orgId), queryFn: () => getIpRanges(orgId), operator, rows: 2, failure: 'Could not load the IP allowlist.' })
  const nextKey = useRef(0)
  const toRows = (ranges: readonly IpRange[]): Row[] =>
    ranges.map(r => ({ key: nextKey.current++, cidr: r.cidr, label: r.label, saved: { updated_at: r.updated_at, updated_by: r.updated_by, updated_by_email: r.updated_by_email } }))
  const [draft, setDraft] = useState<Row[]>([])
  const saved = data?.ranges
  useEffect(() => setDraft(saved ? toRows(saved) : []), [saved])
  const { busy, formError, run, saved: justSaved, clearSaved } = useSubmit()
  const [confirm, confirmDialog] = useConfirm()

  if (gate) return gate

  const ranges = data.ranges
  const yourIp = data.your_ip
  const toSave = cleaned(draft)
  const changed = !sameRows(toSave, cleaned(ranges))
  const edited = !sameRows(draft, ranges)
  const listed = yourIp !== null && draft.some(r => [yourIp, `${yourIp}/32`, `${yourIp}/128`].includes(r.cidr.trim()))

  function edit(next: (rows: Row[]) => Row[]) {
    setDraft(next)
    clearSaved()
  }
  const addRow = (cidr: string) => edit(rows => [...rows, { key: nextKey.current++, cidr, label: '' }])
  const setRow = (key: number, patch: Partial<Row>) => edit(rows => rows.map(r => (r.key === key ? { ...r, ...patch } : r)))

  const store = (next: { cidr: string; label: string }[]) =>
    run('Could not save the IP allowlist.', async () => {
      client.setQueryData(ipAllowlistKey(orgId), await putIpRanges(orgId, next))
    })

  async function clearList() {
    const ok = await confirm({
      title: 'Clear the IP allowlist?',
      message: `Removes ${ranges.length === 1 ? 'the 1 range' : `all ${ranges.length} ranges`}. Members can then sign in from any address.`,
      confirmLabel: 'Clear',
      destructive: true,
    })
    if (ok) await store([])
  }

  // A range field is a textarea so a long range wraps; Enter saves instead of breaking the line.
  function submitOnEnter(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key !== 'Enter') return
    e.preventDefault()
    ;(e.target as HTMLTextAreaElement).form?.requestSubmit()
  }

  return (
    <Stack
      component="form"
      noValidate
      spacing={1.5}
      data-testid="ip-allowlist"
      onSubmit={e => {
        e.preventDefault()
        if (changed) void store(toSave)
      }}
      sx={{ maxWidth: 640, minWidth: 0 }}
    >
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        Empty allows any address. Members outside these ranges are refused; nodes and runners never are.
        Members on the tailnet arrive from the subnet router’s address, so list that address for them.
      </Typography>

      {yourIp && !operator && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1 }}>
          <Typography variant="body2" data-testid="ip-allowlist-your-ip" sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>
            Your address: <Box component="span" sx={monoSx}>{yourIp}</Box>
          </Typography>
          {!listed && (
            <Button size="small" disabled={busy} onClick={() => addRow(yourIp)} data-testid="ip-allowlist-add-mine" sx={tapTargetSx}>
              Add my address
            </Button>
          )}
        </Box>
      )}

      {draft.length > 0 && (
        <Stack component="ul" aria-label="Ranges" spacing={1.5} sx={{ listStyle: 'none', m: 0, p: 0 }}>
          {draft.map((row, i) => (
            <Box
              component="li"
              key={row.key}
              data-testid="ip-allowlist-row"
              sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-start', gap: 1 }}
            >
              <TextField
                size="small"
                label="Range"
                multiline
                value={row.cidr}
                onChange={e => setRow(row.key, { cidr: e.target.value })}
                onKeyDown={submitOnEnter}
                disabled={busy}
                placeholder="203.0.113.0/24"
                slotProps={{ htmlInput: { 'aria-label': `Range ${i + 1}`, spellCheck: false, autoCapitalize: 'off' } }}
                sx={[{ flex: '2 1 14rem', minWidth: 0 }, WRAP_SX, theme => ({ '& textarea': monoSx(theme) })]}
              />
              <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1, flex: '1 1 10rem', minWidth: 0 }}>
                <TextField
                  size="small"
                  label="Label"
                  value={row.label}
                  onChange={e => setRow(row.key, { label: e.target.value })}
                  disabled={busy}
                  placeholder="Office"
                  slotProps={{ htmlInput: { 'aria-label': `Label ${i + 1}`, maxLength: 100 } }}
                  sx={{ flex: 1, minWidth: 0 }}
                />
                <IconButton
                  aria-label={`Remove ${row.cidr.trim() || `range ${i + 1}`}`}
                  disabled={busy}
                  onClick={() => edit(rows => rows.filter(r => r.key !== row.key))}
                  sx={tapTargetSx}
                >
                  <CloseIcon size={16} />
                </IconButton>
              </Box>
              {row.saved && (
                <Typography
                  variant="caption"
                  data-testid="ip-allowlist-changed"
                  sx={{ color: 'text.secondary', flexBasis: '100%', mt: -0.5, minWidth: 0, overflowWrap: 'anywhere' }}
                >
                  Changed{row.saved.updated_by && <> by <ChangedBy name={row.saved.updated_by} email={row.saved.updated_by_email} /></>}
                  {' · '}<RelativeTime iso={row.saved.updated_at} />
                </Typography>
              )}
            </Box>
          ))}
        </Stack>
      )}

      <Box>
        <Button size="small" disabled={busy} onClick={() => addRow('')} data-testid="ip-allowlist-add" sx={tapTargetSx}>
          Add range
        </Button>
      </Box>

      {formError && <Banner severity="error" testId="ip-allowlist-error">{formError}</Banner>}

      {/* Save and Cancel only while the list is edited (as Sign-in's and HIPAA's): a disabled full-width pair
          cost a phone two rows. */}
      <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1.5, '&:empty': { display: 'none' } }}>
        {edited && (
          <>
            <SubmitButton busy={busy} disabled={!changed} testId="ip-allowlist-save">Save</SubmitButton>
            <Button
              disabled={busy}
              onClick={() => edit(() => toRows(ranges))}
              data-testid="ip-allowlist-cancel"
              sx={tapTargetSx}
            >
              Cancel
            </Button>
          </>
        )}
        <SavedNote show={justSaved} />
        {operator && ranges.length > 0 && (
          <Button
            color="error"
            disabled={busy}
            onClick={() => void clearList()}
            data-testid="ip-allowlist-clear"
            sx={[tapTargetSx, { ml: 'auto' }]}
          >
            Clear list
          </Button>
        )}
      </Box>
      {confirmDialog}
    </Stack>
  )
}

export default IpAllowlistSection
