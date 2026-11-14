import { useState } from 'react'
import Stack from '@mui/material/Stack'
import { useApiRead } from '../../hooks/useApiRead'
import { useSubmit } from '../../hooks/useSubmit'
import type { Org } from '../../types'
import Banner from '../../ui/Banner'
import FeedList from '../../ui/feed/FeedList'
import type { FeedRowSpec } from '../../ui/feed/types'
import FormTextField from '../../ui/FormTextField'
import SubmitButton from '../../ui/SubmitButton'
import { type OrgDomain, addOrgDomain, listOrgDomains, removeOrgDomain, updateOrg } from './api'
import { grid } from '../../theme'

/* One org's email-domain joins: the role a domain join grants (an Org Owner/Admin may change it) and the
 * domains themselves, one `ui/feed/FeedList` row each, added and removed (the row's ⋯) by the operator alone
 * (`routes/orgs.py`). Rendered by the tenant's own-org cards in Settings (`pages/settings/ownOrg/OwnOrgPanels.tsx`)
 * and the operator's customer page (`pages/customers/AccessTab.tsx`). */

// `access.DEFAULT_ROLE_SLUGS`; '' stands for none (a native select's value is a string).
const DEFAULT_ROLE_OPTIONS = [
  { value: '', label: 'None', help: 'Joiners reach nothing until someone grants a role.' },
  { value: 'project_viewer', label: 'Project Viewer', help: 'Joiners can view every project.' },
  { value: 'project_developer', label: 'Project Developer', help: 'Joiners can work on every project.' },
] as const

const DEFAULT_ROLE_HELP = {
  tip: 'Role new members get across the organization.',
  more: 'Granted across the whole organization to each new member: one who joins through a join domain, signs up, or arrives by SSO. It saves as soon as you pick it.',
  docs: 'administration/people-and-orgs/orgs',
}

export default function DomainsTab({ org, isOperator, onOrgSaved }: {
  org: Org
  isOperator: boolean
  onOrgSaved: (org: Org) => void
}) {
  const { busy: savingRole, formError: roleError, run: runRole, saved: roleSaved } = useSubmit()
  const [draft, setDraft] = useState('')
  const { busy, formError: domainError, run } = useSubmit()
  const { data, error: loadError, refresh } = useApiRead(
    ['org-domains', org.id],
    () => listOrgDomains(org.id).then(rows => (Array.isArray(rows) ? rows : [])),
    'Failed to load domains.',
  )
  // A failed read shows the empty list under its error, not a skeleton that never resolves.
  const domains: OrgDomain[] | null = data ?? (loadError ? [] : null)

  function saveRole(value: string) {
    return runRole('Failed to save the default role.', async () => {
      onOrgSaved(await updateOrg(org.id, { default_role: value || null }))
    })
  }

  async function add() {
    const domain = draft.trim()
    if (!domain) return
    await run(`Failed to add ${domain}.`, async () => {
      await addOrgDomain(org.id, domain)
      setDraft('')
      await refresh()
    })
  }

  const remove = (domain: string) => run(`Failed to remove ${domain}.`, async () => {
    await removeOrgDomain(org.id, domain)
    await refresh()
  })

  const rows: FeedRowSpec[] = (domains ?? []).map((d): FeedRowSpec => ({
    key: d.domain,
    title: d.domain,
    titleText: d.domain,
    href: null,
    busy,
    actions: isOperator
      ? [{ key: 'remove', label: 'Remove', ariaLabel: `Remove ${d.domain}`, danger: true, onClick: () => void remove(d.domain) }]
      : undefined,
  }))

  return (
    <Stack spacing={2} sx={{ maxWidth: grid(93) }}>
      <FormTextField
        label="Default role"
        help={DEFAULT_ROLE_HELP}
        error={roleError || undefined}
        hint={roleSaved ? 'Saved.' : undefined}
        id="org-default-role"
        slotProps={{ inputLabel: { shrink: true } }}
        value={org.default_role ?? ''}
        disabled={savingRole}
        onChange={v => void saveRole(v)}
        options={DEFAULT_ROLE_OPTIONS}
      />

      {(domainError || loadError) && <Banner severity="error" testId="org-domain-error">{domainError || loadError}</Banner>}

      {isOperator && (
        <Stack direction="row" spacing={1} component="form" onSubmit={e => { e.preventDefault(); void add() }}>
          <FormTextField
            label="Domain"
            help="Verified Google sign-ins on it join this org."
            value={draft}
            onChange={setDraft}
            sx={{ flex: 1 }}
          />
          <SubmitButton busy={busy} busyLabel="Adding…" disabled={!draft.trim()}>Add</SubmitButton>
        </Stack>
      )}

      <FeedList
        ariaLabel="Join domains"
        testId="org-domains"
        loading={domains === null}
        skeletonRows={2}
        /* Names how one gets added: the operator adds join domains, unlike SSO domains. */
        emptyMessage={isOperator ? 'No domains. Add one above.' : 'No domains. Ask Jaah support to add one.'}
        sections={rows.length > 0 ? [{ key: 'domains', rows, rail: false }] : []}
      />
    </Stack>
  )
}
