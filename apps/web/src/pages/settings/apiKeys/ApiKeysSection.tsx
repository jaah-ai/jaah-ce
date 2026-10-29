import { type JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { apiDetail } from '../../../api'
import { useAuth } from '../../../auth'
import { useConfirm } from '../../../components/ConfirmDialog'
import { useRowActions } from '../../../hooks/useBusyIds'
import { isOrgAdminSeat } from '../../../lib/orgAdmin'
import { absoluteTime } from '../../../lib/relativeTime'
import Banner from '../../../ui/Banner'
import ListStates from '../../../ui/feed/ListStates'
import type { FeedRowSpec, OverflowItem } from '../../../ui/feed/types'
import { tapTargetSx } from '../../../ui/listStyles'
import { API_KEYS_KEY, type ApiKey, apiKeyUrl, listApiKeys, NEW_API_KEY_URL, revokeApiKey } from './api'
import { expiryText, isLive, lastUsedText, ownerName, revokeConfirm, STATUS_GLYPH, STATUS_LABEL, STATUS_TONE } from './keyFacts'
import { grantsSummary } from './keyPermissions'
import { useGridRows } from './useGridRows'

/* Settings › API keys: every key of the org, for every Org Owner and Org Admin (never a secret). A row opens the key
 * (`ApiKeyDetailPage.tsx`); ⋯ revokes it. Every one of them creates keys of their own, the operator too — acting as an
 * org, the key runs as its Owner seat (`app/api_keys.py`). */

/* The Settings pane is ~700px at a 1280px window: the name, owner and grants wrap to two lines rather than cut to a
 * few characters ("All permissions" over two lines in a 72px slot). */
const DISPLAY_LEAD = 84
const OWNER_SLOT = 120
const GRANTS_SLOT = 72
const LAST_USED_SLOT = 132
const EXPIRES_SLOT = 124

/** The owner slot's value; a bare address may wrap after its "@", never mid-word. */
function ownerNode(label: string): JSX.Element | string {
  const shown = ownerName(label)
  const at = shown.indexOf('@')
  return at < 0 || shown.includes(' ') ? shown : <>{shown.slice(0, at + 1)}<wbr />{shown.slice(at + 1)}</>
}

/** Whether this viewer may create a key: any Org Owner or Admin, the operator (acting or not) included. */
export function useCanCreateKey(): boolean {
  const { admin } = useAuth()
  // The server's own create rule (`routes/api_keys.py::create_api_key`): never a tenant org's superadmin.
  return isOrgAdminSeat(admin) && Boolean(admin?.org?.id) && !(admin?.is_superadmin === true && admin.is_operator !== true)
}

export function ApiKeysSection(): JSX.Element {
  const queryClient = useQueryClient()
  const canCreate = useCanCreateKey()
  const { rows: grid } = useGridRows()
  const query = useQuery({
    queryKey: API_KEYS_KEY,
    refetchOnMount: 'always',
    queryFn: async () => {
      const next: unknown = await listApiKeys()
      if (!Array.isArray(next)) throw new Error('The API keys came back in an unexpected shape.')
      return next as ApiKey[]
    },
  })
  const keys = query.data ?? null
  const loadError = query.error ? apiDetail(query.error, 'Failed to load the API keys.') : ''
  const { busy, run, error: rowError } = useRowActions<string>()
  const [confirm, confirmDialog] = useConfirm()

  async function revoke(key: ApiKey) {
    if (!(await confirm(revokeConfirm(key)))) return
    await run(key.id, `Could not revoke ${key.name}. Retry.`, async () => {
      await revokeApiKey(key.id)
      await queryClient.invalidateQueries({ queryKey: API_KEYS_KEY })
    })
  }

  const createButton = canCreate ? (
    <Button component={RouterLink} to={NEW_API_KEY_URL} variant="contained" size="small"
      data-testid="api-keys-create" sx={tapTargetSx}>
      Create key
    </Button>
  ) : undefined

  const feedRows: FeedRowSpec[] = (keys ?? []).map((key): FeedRowSpec => {
    const actions: OverflowItem[] = [
      { key: 'open', label: 'Open', ariaLabel: `Open ${key.name}`, to: apiKeyUrl(key.id) },
    ]
    if (key.can_revoke) {
      actions.push({
        key: 'revoke', icon: '✕', label: 'Revoke', ariaLabel: `Revoke ${key.name}`, danger: true,
        testId: 'api-key-revoke', onClick: () => void revoke(key),
      })
    }
    const status = STATUS_LABEL[key.status]
    return {
      key: key.id,
      indicator: { tone: STATUS_TONE[key.status], glyph: STATUS_GLYPH[key.status], label: status, data: { status: key.status } },
      title: <Box component="span" sx={{ fontWeight: 600 }}>{key.name}</Box>,
      titleText: key.name,
      titleLines: 2,
      // Its visible characters, never the secret (`jaah_…Ab3d`).
      lead: { text: key.display, width: DISPLAY_LEAD, align: 'left', title: `Key ${key.display}` },
      href: apiKeyUrl(key.id),
      busy: busy.has(key.id),
      muted: !isLive(key),
      data: { status: key.status },
      notes: key.status === 'suspended'
        ? [{ key: 'suspended', tone: 'warning', text: `${key.owner.label} is suspended: the key is refused until they are back.` }]
        : [],
      slots: [
        {
          key: 'last-used', width: LAST_USED_SLOT, gate: 'meta', align: 'right', dim: true,
          title: key.last_used_at ? `Last used ${absoluteTime(key.last_used_at)}` : 'Never used',
          node: lastUsedText(key), testId: 'api-key-last-used',
        },
        {
          key: 'grants', width: GRANTS_SLOT, gate: 'meta', align: 'right', lines: 2,
          title: 'Permissions', node: grid ? grantsSummary(grid, key) : null, testId: 'api-key-grants',
        },
        {
          key: 'owner', width: OWNER_SLOT, gate: 'id', align: 'right',
          lines: 2, title: `Owner: ${key.owner.label}`, node: ownerNode(key.owner.label), testId: 'api-key-owner',
        },
        {
          key: 'expires', width: EXPIRES_SLOT, align: 'right',
          title: key.expires_at ? absoluteTime(key.expires_at) : 'No expiration',
          node: key.status === 'revoked' || key.status === 'suspended' ? status : expiryText(key),
          data: { status: key.status }, testId: 'api-key-expires',
        },
      ],
      actions,
    }
  })

  const empty = keys !== null && keys.length === 0

  return (
    <Box component="section" data-testid="api-keys-section" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      {!empty && createButton && <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>{createButton}</Box>}
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {rowError && <Banner severity="error">{rowError}</Banner>}
      {/* No keys once loaded = the load failed: its banner is above, nothing below. */}
      <ListStates
        loading={keys === null && !loadError}
        skeleton={{}}
        failed={keys === null}
        empty={empty}
        noMatch={false}
        emptyTitle="No API keys yet"
        emptyAction={createButton}
        feed={{ ariaLabel: 'API keys', testId: 'api-keys-list', sections: [{ key: 'keys', rows: feedRows }] }}
      />
      {confirmDialog}
    </Box>
  )
}

export default ApiKeysSection
