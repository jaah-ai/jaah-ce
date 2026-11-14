import { useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MuiLink from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import Banner from '../../ui/Banner'
import FormDialog from '../../ui/FormDialog'
import FormTextField from '../../ui/FormTextField'
import LoadingState from '../../ui/LoadingState'

import type { Explain } from '../../ui/explain'
import RepoUrlLookup, { PROFILE_GIT_PATH } from '../../ui/RepoUrlLookup'
import { tapTargetSx } from '../../ui/listStyles'
import { type AgentRegistration, ENV_NAME, refusalSentences } from './api'

/* The agent page's and the Catalog tab's shared pieces: the page notice (a server refusal is a LIST of sentences, each
 * its own item), the repo + sub-path + ref fields both submit through, an agent's env rows, and the operator's
 * Publish dialog (`CatalogPage`). An agent itself is created and edited on its page (`AgentEditPage`). */

const GITHUB_ONLY = ['github'] as const

/* The fields' explanations (`ui/HelpLabel`): what `agent_bundles` does with each value on submit. */
const AGENTS_DOCS = 'using-jaah/agents/how-agents-work'
const REF_HELP: Explain = {
  tip: 'Branch, tag or commit; Jaah pins the commit it names.',
  more: 'A branch or tag is pinned again to its newest commit within about 15 minutes of a push. Enter a commit to keep the agent on that exact code.',
  docs: AGENTS_DOCS,
}
export const ENV_HELP: Explain = {
  tip: "Variables set on the agent's process when a task runs.",
  more: "For example the model a connector uses. Not for secrets: a name ending _TOKEN, _API_KEY, _SECRET or PASSWORD is refused. Fetch a secret as a credential.",
  docs: 'using-jaah/agents/agent-sdk-reference',
}

export type Notice = { severity: 'success' | 'error'; lines: string[] }

const SentenceList = ({ lines }: { lines: string[] }) => (
  <Box component="ul" sx={{ m: 0, pl: 2.5 }}>{lines.map((line, i) => <li key={i}>{line}</li>)}</Box>
)

export function NoticeBanner({ notice, onDismiss, testId }: { notice: Notice | null; onDismiss: () => void; testId: string }) {
  if (!notice) return null
  return (
    <Banner severity={notice.severity} onDismiss={onDismiss} testId={testId}>
      {notice.lines.length === 1 ? notice.lines[0] : <SentenceList lines={notice.lines} />}
    </Banner>
  )
}

/** A fully blank env row is ignored; any other needs a valid name, once. The check of one row against the rest. */
export type EnvRow = { name: string; value: string }
export const envRowsOf = (env: Record<string, string> | undefined): EnvRow[] =>
  Object.entries(env ?? {}).map(([name, value]) => ({ name, value }))
export const filledEnv = (rows: EnvRow[]): EnvRow[] => rows.filter(row => row.name.trim() !== '' || row.value !== '')
export function envRowError(rows: EnvRow[], row: EnvRow): string | undefined {
  const name = row.name.trim()
  if (name === '' && row.value === '') return undefined
  if (!ENV_NAME.test(name)) return 'Letters, digits and _; not starting with a digit'
  if (filledEnv(rows).filter(other => other.name.trim() === name).length > 1) return 'Already set above'
  return undefined
}
export const envMap = (rows: EnvRow[]): Record<string, string> =>
  Object.fromEntries(filledEnv(rows).map(row => [row.name.trim(), row.value]))

/** Where an agent or connector comes from: the repo (Agents are GitHub-only, `agent_bundles.parse_repo`), the folder
 *  holding its manifest, and the ref (a connector's: an exact reviewed commit). The agent page's Source section and
 *  the operator's Publish dialog draw the same three fields. */
export function SourceFields({ form, onChange, connector = false, repoEditable = true, readOnly = false, busy }: {
  form: AgentRegistration
  onChange: (patch: Partial<AgentRegistration>) => void
  connector?: boolean
  repoEditable?: boolean
  readOnly?: boolean
  busy: boolean
}) {
  const idPrefix = connector ? 'connector' : 'agent'
  return (
    <>
      {/* A connector is read with the operator's App, not this person's connection, so a repo missing from their
          list says nothing there. */}
      <RepoUrlLookup
        id={`${idPrefix}-repo-url`}
        label="Repository URL"
        forges={GITHUB_ONLY}
        warnUnlisted={!connector && repoEditable}
        placeholder={connector ? undefined : 'https://github.com/acme/agents'}
        value={form.repo_url}
        onChange={v => onChange({ repo_url: v })}
        disabled={busy || !repoEditable || readOnly}
        required
        help={connector
          ? 'GitHub repo holding the connector; fetched and checked on publish.'
          : 'GitHub repo holding the agent; fetched and checked on save.'}
        hint={!connector && repoEditable && (
          <span data-testid="agent-dialog-git">
            Reads the repo with your Git connection (
            <MuiLink component={RouterLink} to={PROFILE_GIT_PATH}>Profile › Git</MuiLink>).
          </span>
        )}
      />
      <FormTextField
        id={`${idPrefix}-subpath`}
        label="Sub-path"
        placeholder={connector ? 'connectors/report-sample' : 'agents/report-bot'}
        value={form.subpath}
        onChange={v => onChange({ subpath: v })}
        help="Folder holding jaah-agent.toml; blank for the repo root."
        disabled={busy || readOnly}
        fullWidth
      />
      <FormTextField
        id={`${idPrefix}-ref`}
        label={connector ? 'Commit' : 'Ref'}
        placeholder={connector ? '40-character commit sha' : 'main'}
        value={form.ref}
        onChange={v => onChange({ ref: v })}
        help={connector ? 'Exact reviewed commit to publish; anything else is refused.' : REF_HELP}
        disabled={busy || readOnly}
        required
        fullWidth
      />
    </>
  )
}

/** An agent's env: one Name/Value pair per variable, set on its process at launch. Not secrets. The host titles it
 *  (with `ENV_HELP`). */
export function EnvFields({ rows, onChange, readOnly = false, busy }: {
  rows: EnvRow[]
  onChange: (rows: EnvRow[]) => void
  readOnly?: boolean
  busy: boolean
}) {
  const setRow = (index: number, patch: Partial<EnvRow>) => onChange(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }} data-testid="agent-env">
      {rows.length === 0 && readOnly && <Typography variant="body2" sx={{ color: 'text.secondary' }}>No variables.</Typography>}
      {rows.map((row, i) => (
        <Box key={i} sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
          <FormTextField
            id={`agent-env-name-${i}`}
            label="Name"
            value={row.name}
            onChange={v => setRow(i, { name: v })}
            help="Variable name; secret-like names are refused."
            error={envRowError(rows, row)}
            disabled={busy || readOnly}
            sx={{ flex: 1, minWidth: 0 }}
          />
          <FormTextField
            id={`agent-env-value-${i}`}
            label="Value"
            value={row.value}
            onChange={v => setRow(i, { value: v })}
            help="Set as-is on the agent's process."
            disabled={busy || readOnly}
            sx={{ flex: 1, minWidth: 0 }}
          />
          {!readOnly && (
            <Button
              aria-label={`Remove ${row.name.trim() || 'variable'}`}
              onClick={() => onChange(rows.filter((_, j) => j !== i))}
              disabled={busy}
              sx={[{ minWidth: 0, mt: 3 }, tapTargetSx]}
            >
              ✕
            </Button>
          )}
        </Box>
      ))}
      {!readOnly && (
        <Box>
          <Button size="small" onClick={() => onChange([...rows, { name: '', value: '' }])} disabled={busy} sx={tapTargetSx}>
            Add variable
          </Button>
        </Box>
      )}
    </Box>
  )
}

/** A refusal's sentences, one item each (a 422 is a LIST, `refusalSentences`). */
export function RefusalBanner({ lines, testId }: { lines: string[]; testId: string }) {
  if (lines.length === 0) return null
  return <Banner severity="error" testId={testId}><SentenceList lines={lines} /></Banner>
}

/** The operator's Publish of a reviewed catalog connector at an exact commit (no env, no Git-connection read; the
 *  server refuses anything else). A release act, not a record editor: the published connector shows as its Catalog
 *  card. The server fetches and validates on submit, which takes seconds: the submit spins, and a refusal's
 *  sentences are listed in the dialog. */
export function PublishConnectorDialog({ initial, onClose, onSave }: {
  initial: AgentRegistration
  onClose: () => void
  onSave: (body: AgentRegistration) => Promise<void>
}) {
  const [form, setForm] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [errors, setErrors] = useState<string[]>([])
  const incomplete = form.repo_url.trim() === '' || form.ref.trim() === ''

  async function submit() {
    setBusy(true)
    setErrors([])
    try {
      await onSave({ repo_url: form.repo_url.trim(), subpath: form.subpath.trim(), ref: form.ref.trim() })
      onClose()
    } catch (err) {
      setErrors(refusalSentences(err, 'Could not publish the connector'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <FormDialog
      title="Publish connector"
      submitLabel="Publish"
      busyLabel="Checking…"
      busy={busy}
      submitDisabled={incomplete}
      onSubmit={() => void submit()}
      onClose={onClose}
      testId="publish-dialog"
      fullScreenOnPhone
    >
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
        <SourceFields connector form={form} onChange={patch => setForm(f => ({ ...f, ...patch }))} busy={busy} />
        {busy && <LoadingState label="Fetching the repository and checking its manifest…" />}
        <RefusalBanner lines={errors} testId="publish-dialog-errors" />
      </Box>
    </FormDialog>
  )
}
