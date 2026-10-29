import { useCallback, useEffect, useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../api'
import { useSubmit } from '../../hooks/useSubmit'
import type { Org } from '../../types'
import Banner from '../../ui/Banner'
import EmptyState from '../../ui/EmptyState'
import FormTextField from '../../ui/FormTextField'
import LoadingState from '../../ui/LoadingState'
import SubmitButton from '../../ui/SubmitButton'
import { type OrgDomain, addOrgDomain, listOrgDomains, removeOrgDomain, updateOrg } from './api'

/* One org's email-domain joins: the role a domain join grants (an Org Owner/Admin may change it) and the
 * domains themselves, added and removed by the operator alone (`routes/orgs.py`). Rendered by the tenant's
 * own-org cards in Settings (`pages/settings/ownOrg/OwnOrgPanels.tsx`) and the operator's customer page (`pages/customers/AccessTab.tsx`). */

// `access.DEFAULT_ROLE_SLUGS`; '' stands for none (a native select's value is a string).
const DEFAULT_ROLE_OPTIONS = [
  { value: '', label: 'None' },
  { value: 'project_viewer', label: 'Project Viewer' },
  { value: 'project_developer', label: 'Project Developer' },
] as const

export default function DomainsTab({ org, isOperator, onOrgSaved }: {
  org: Org
  isOperator: boolean
  onOrgSaved: (org: Org) => void
}) {
  const { busy: savingRole, formError: roleError, run: runRole, saved: roleSaved } = useSubmit()
  const [domains, setDomains] = useState<OrgDomain[] | null>(null)
  const [draft, setDraft] = useState('')
  const { busy, formError: domainError, setFormError: setDomainError, run } = useSubmit()

  const fetchDomains = useCallback(async () => {
    try {
      const rows = await listOrgDomains(org.id)
      setDomains(Array.isArray(rows) ? rows : [])
    } catch (err) {
      setDomains([])
      setDomainError(apiDetail(err, 'Failed to load domains.'))
    }
  }, [org.id])

  useEffect(() => {
    void fetchDomains()
  }, [fetchDomains])

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
      await fetchDomains()
    })
  }

  const remove = (domain: string) => run(`Failed to remove ${domain}.`, async () => {
    await removeOrgDomain(org.id, domain)
    await fetchDomains()
  })

  return (
    <Stack spacing={2} sx={{ maxWidth: 560 }}>
      <FormTextField
        label="Default role"
        error={roleError || undefined}
        hint={roleSaved ? 'Saved.' : undefined}
        id="org-default-role"
        slotProps={{ inputLabel: { shrink: true } }}
        value={org.default_role ?? ''}
        disabled={savingRole}
        onChange={v => void saveRole(v)}
        options={DEFAULT_ROLE_OPTIONS}
      />

      {domainError && <Banner severity="error" testId="org-domain-error">{domainError}</Banner>}

      {isOperator && (
        <Stack direction="row" spacing={1} component="form" onSubmit={e => { e.preventDefault(); void add() }}>
          <TextField
            size="small"
            label="Domain"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            sx={{ flex: 1 }}
          />
          <SubmitButton busy={busy} busyLabel="Adding…" disabled={!draft.trim()}>Add</SubmitButton>
        </Stack>
      )}

      <Paper variant="outlined">
        {domains === null ? (
          <LoadingState rows={2} />
        ) : domains.length === 0 ? (
          /* Names how one gets added: the operator adds join domains, unlike SSO domains. */
          <EmptyState title={isOperator ? 'No domains. Add one above.' : 'No domains. Your operator adds them.'} />
        ) : (
          <Box component="ul" aria-label="Join domains" sx={{ listStyle: 'none', m: 0, p: 0 }}>
            {domains.map(d => (
              <Box
                component="li"
                key={d.domain}
                sx={{ display: 'flex', alignItems: 'center', px: 1.5, py: 0.75, borderTop: 1, borderColor: 'divider', '&:first-of-type': { borderTop: 0 } }}
              >
                <Typography sx={{ flex: 1 }}>{d.domain}</Typography>
                {isOperator && (
                  <Button size="small" color="error" disabled={busy} aria-label={`Remove ${d.domain}`} onClick={() => void remove(d.domain)}>
                    Remove
                  </Button>
                )}
              </Box>
            ))}
          </Box>
        )}
      </Paper>
    </Stack>
  )
}
