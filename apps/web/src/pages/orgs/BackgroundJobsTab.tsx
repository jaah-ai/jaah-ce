import { type JSX, useState } from 'react'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import { grid } from '../../theme'
import { useSubmit } from '../../hooks/useSubmit'
import { searchUsers, userOption } from '../../lib/users'
import type { Org } from '../../types'
import AsyncLookup, { type LookupOption } from '../../ui/AsyncLookup'
import Banner from '../../ui/Banner'
import FormField from '../../ui/FormField'
import SubmitButton from '../../ui/SubmitButton'
import { updateOrg } from './api'

/* WHO AN ORG'S BACKGROUND JOBS RUN AS (`routes/orgs.py::OrgUpdateIn.run_jobs_as_id`): the outcome scan, the merge
 * lane's bot-PR task and the benchmark review act as this member unless a project names its own (the project's
 * Advanced tab) or an automation its own "Run as". Unset: the org's first live Owner/Admin
 * (`git_access.project_forge_person`). An Org Owner/Admin's card in Settings › Organizations, and the
 * operator's on the customer's Settings tab. The server refuses someone who is not an enabled member of this org. */

const initial = (org: Org): LookupOption | null =>
  org.run_jobs_as_id ? { key: org.run_jobs_as_id, label: org.run_jobs_as_name ?? org.run_jobs_as_id } : null

export default function BackgroundJobsTab({ org, onOrgSaved }: { org: Org; onOrgSaved: (org: Org) => void }): JSX.Element {
  const [picked, setPicked] = useState<LookupOption | null>(() => initial(org))
  const save = useSubmit()
  const changed = (picked?.key ?? null) !== (org.run_jobs_as_id ?? null)

  const submit = () => save.run('Failed to save who jobs run as.', async () => {
    onOrgSaved(await updateOrg(org.id, { run_jobs_as_id: picked?.key ?? null }))
  })

  return (
    <Box data-testid="org-background-jobs" sx={{ maxWidth: grid(100), minWidth: 0 }}>
      <Stack spacing={1.5}>
        <FormField
          label="Run jobs as"
          help={{
            tip: 'Member the outcome scan, merge lane and benchmarks act as.',
            more: 'Empty, jobs act as the first Org Owner or Org Admin. A project\'s own Run jobs as, '
              + 'or an automation\'s Run as, takes its place there.',
            docs: 'administration/security-and-audit/settings',
          }}
        >
          <AsyncLookup
            id="org-run-jobs-as"
            value={picked}
            onChange={setPicked}
            fetchOptions={async (query, signal) =>
              (await searchUsers(query, signal, { enabled: true, org: org.id })).map(userOption)}
            fetchOnFocus
            stacked
            placeholder="First Owner/Admin"
            ariaLabel="Run jobs as"
            noOptionsText="No matching member"
            disabled={save.busy}
          />
        </FormField>
        {save.formError && <Banner severity="error" testId="org-background-jobs-error">{save.formError}</Banner>}
        <Box>
          <SubmitButton busy={save.busy} disabled={!changed} onClick={() => void submit()} testId="org-background-jobs-save">
            {save.saved && !changed ? 'Saved' : 'Save'}
          </SubmitButton>
        </Box>
      </Stack>
    </Box>
  )
}
