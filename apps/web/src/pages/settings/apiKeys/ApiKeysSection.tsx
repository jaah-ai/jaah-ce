import { type JSX } from 'react'
import Box from '@mui/material/Box'
import { useAuth } from '../../../auth'
import { useSettingsRows } from '../../../hooks/useSettingsRows'
import { absoluteTime } from '../../../lib/relativeTime'
import Banner from '../../../ui/Banner'
import ListStates from '../../../ui/feed/ListStates'
import type { FeedRowSpec, OverflowItem } from '../../../ui/feed/types'
import { contentSlotWidth } from '../../../ui/feed/geometry'
import { tapTargetSx } from '../../../ui/listStyles'
import { type ApiKey, apiKeyUrl, listApiKeys, NEW_API_KEY_URL, revokeApiKey } from './api'
import { expiryText, isLive, lastUsedText, ownerName, revokeConfirm, STATUS } from './keyFacts'
import { grantsSummary } from './keyPermissions'
import { useGridRows } from './useGridRows'
import { FONT_WEIGHT } from '../../../theme'
import NewButton from '../../../ui/NewButton'

/* Settings › API keys: every key of the org for an Org Owner or Admin, a member's own for anyone else (never a
 * secret; the server decides, `routes/api_keys.py`). A row opens the key (`ApiKeyDetailPage.tsx`); ⋯ revokes it.
 * Every member creates keys of their own, the operator too — acting as an org, the key runs as its Owner seat
 * (`app/api_keys.py`). */

/* The Settings pane is ~700px at a 1280px window: the name and owner wrap to two lines rather than cut to a few
 * characters. The grants read on one line ("12 of 44 permissions"): their column sizes to the widest summary shown,
 * up to `GRANTS_MAX_CQW` of the list, and ellipsises past it (the hover holds the whole summary). Last used and
 * Expires are priced to their values ("Expires in 365 days" is ~98px at the cluster's 11px) so the name keeps its
 * floor (`ui/feed/fit.ts`) with every slot at 1280px. */
const DISPLAY_LEAD = 84
const OWNER_SLOT = 120
const GRANTS_SLOT = 72
const GRANTS_MAX_CQW = 24
const LAST_USED_SLOT = 116
const EXPIRES_SLOT = 104

/** The owner slot's value; a bare address may wrap after its "@", never mid-word. */
function ownerNode(label: string): JSX.Element | string {
  const shown = ownerName(label)
  const at = shown.indexOf('@')
  return at < 0 || shown.includes(' ') ? shown : <>{shown.slice(0, at + 1)}<wbr />{shown.slice(at + 1)}</>
}

/** Whether this viewer may create a key: any member of an org, the operator (acting or not) included. */
export function useCanCreateKey(): boolean {
  const { admin } = useAuth()
  // The server's own create rule (`routes/api_keys.py::create_api_key`): never a tenant org's superadmin.
  return Boolean(admin?.org?.id) && !(admin?.is_superadmin === true && admin.is_operator !== true)
}

export function ApiKeysSection(): JSX.Element {
  const canCreate = useCanCreateKey()
  const { rows: grid } = useGridRows()
  const { rows: keys, loadError, rowError, busyIds, rowWrite, confirm, confirmDialog } = useSettingsRows<ApiKey>({
    fetch: async () => ({ rows: await listApiKeys() }),
    failed: 'Failed to load the API keys.',
    shapeError: 'The API keys came back in an unexpected shape.',
  })

  async function revoke(key: ApiKey) {
    if (!(await confirm(revokeConfirm(key)))) return
    await rowWrite(key.id, `Could not revoke ${key.name}. Retry.`, () => revokeApiKey(key.id))
  }

  const createButton = (worded = false) => canCreate ? (
    <NewButton label="New key" to={NEW_API_KEY_URL} testId="api-keys-create" worded={worded} sx={tapTargetSx} />
  ) : undefined

  const grantsOf = (key: ApiKey): string | null => (grid ? grantsSummary(grid, key) : null)
  const grantsWidth = contentSlotWidth((keys ?? []).flatMap(key => grantsOf(key) ?? []), GRANTS_SLOT, GRANTS_MAX_CQW)

  const feedRows: FeedRowSpec[] = (keys ?? []).map((key): FeedRowSpec => {
    const grants = grantsOf(key)
    const actions: OverflowItem[] = [
      { key: 'open', label: 'Open', ariaLabel: `Open ${key.name}`, to: apiKeyUrl(key.id) },
    ]
    if (key.can_revoke) {
      actions.push({
        key: 'revoke', icon: '✕', label: 'Revoke', ariaLabel: `Revoke ${key.name}`, danger: true,
        testId: 'api-key-revoke', onClick: () => void revoke(key),
      })
    }
    const status = STATUS[key.status]
    return {
      key: key.id,
      indicator: { ...status, data: { status: key.status } },
      title: <Box component="span" sx={{ fontWeight: FONT_WEIGHT.semibold }}>{key.name}</Box>,
      titleText: key.name,
      titleLines: 2,
      // Its visible characters, never the secret (`jaah_…Ab3d`).
      lead: { text: key.display, width: DISPLAY_LEAD, align: 'left', title: `Key ${key.display}` },
      href: apiKeyUrl(key.id),
      busy: busyIds.has(key.id),
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
          key: 'grants', width: grantsWidth, gate: 'meta', align: 'right',
          title: grants ? `Permissions: ${grants}` : 'Permissions', node: grants, testId: 'api-key-grants',
        },
        {
          key: 'owner', width: OWNER_SLOT, gate: 'id', align: 'right',
          lines: 2, title: `Owner: ${key.owner.label}`, node: ownerNode(key.owner.label), testId: 'api-key-owner',
        },
        {
          key: 'expires', width: EXPIRES_SLOT, align: 'right',
          title: key.expires_at ? absoluteTime(key.expires_at) : 'No expiration',
          node: key.status === 'revoked' || key.status === 'suspended' ? status.label : expiryText(key),
          data: { status: key.status }, testId: 'api-key-expires',
        },
      ],
      actions,
    }
  })

  // A failed load (a refusal, or a malformed answer the hook settled to `[]`) is its banner and nothing below.
  const failed = keys === null || (Boolean(loadError) && keys.length === 0)
  const empty = !failed && keys.length === 0

  return (
    <Box component="section" data-testid="api-keys-section" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      {!empty && canCreate && <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>{createButton()}</Box>}
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {rowError && <Banner severity="error">{rowError}</Banner>}
      <ListStates
        loading={keys === null && !loadError}
        skeleton={{}}
        failed={failed}
        empty={empty}
        noMatch={false}
        emptyTitle="No API keys yet"
        emptyAction={createButton(true)}
        feed={{ ariaLabel: 'API keys', testId: 'api-keys-list', sections: [{ key: 'keys', rows: feedRows }] }}
      />
      {confirmDialog}
    </Box>
  )
}

export default ApiKeysSection
