import { useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import MuiLink from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import Banner from '../../ui/Banner'
import FormDialog from '../../ui/FormDialog'
import FormTextField from '../../ui/FormTextField'
import RepoUrlLookup from '../../ui/RepoUrlLookup'
import { type AgentRegistration, ENV_NAME, refusalSentences } from './api'

/* The Agents and Catalog tabs' shared pieces: the page notice (a server refusal is a LIST of sentences, each
 * its own item) and the snapshot dialog both tabs submit a repo + sub-path + ref through. */

const GITHUB_ONLY = ['github'] as const

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

/** Register or re-point an agent, and set its env — or, with `connector`, the operator's Publish of a reviewed
 *  catalog connector at an exact commit (no env, no Git-connection read; the server refuses anything else).
 *  The server fetches and validates on submit, which takes seconds: the submit spins, and a refusal's sentences
 *  are listed in the dialog. An agent's repo is read as the caller (`agent_bundles` → their Git connection). */
export function SnapshotDialog({ title, submitLabel, initial, repoEditable = true, connector = false, onClose, onSave }: {
  title: string
  submitLabel: string
  initial: AgentRegistration
  repoEditable?: boolean
  connector?: boolean
  onClose: () => void
  onSave: (body: AgentRegistration) => Promise<void>
}) {
  const [form, setForm] = useState(initial)
  const [envRows, setEnvRows] = useState(() => Object.entries(initial.env ?? {}).map(([name, value]) => ({ name, value })))
  const [busy, setBusy] = useState(false)
  const [errors, setErrors] = useState<string[]>([])
  const testId = connector ? 'publish-dialog' : 'agent-dialog'
  const idPrefix = connector ? 'connector' : 'agent'
  // A fully blank row is ignored; any other needs a valid name, once.
  const filled = envRows.filter(row => row.name.trim() !== '' || row.value !== '')
  const envError = (row: { name: string; value: string }): string | undefined => {
    const name = row.name.trim()
    if (name === '' && row.value === '') return undefined
    if (!ENV_NAME.test(name)) return 'Letters, digits and _; not starting with a digit'
    if (filled.filter(other => other.name.trim() === name).length > 1) return 'Already set above'
    return undefined
  }
  const incomplete = form.repo_url.trim() === '' || form.ref.trim() === '' || filled.some(row => envError(row) !== undefined)
  const setEnvRow = (index: number, patch: Partial<{ name: string; value: string }>) =>
    setEnvRows(rows => rows.map((row, i) => (i === index ? { ...row, ...patch } : row)))

  async function submit() {
    setBusy(true)
    setErrors([])
    try {
      const env = Object.fromEntries(filled.map(row => [row.name.trim(), row.value]))
      await onSave({
        repo_url: form.repo_url.trim(), subpath: form.subpath.trim(), ref: form.ref.trim(),
        ...(filled.length > 0 || Object.keys(initial.env ?? {}).length > 0 ? { env } : {}),
      })
      onClose()
    } catch (err) {
      setErrors(refusalSentences(err, connector ? 'Could not publish the connector' : 'Could not save the agent'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <FormDialog
      title={title}
      submitLabel={submitLabel}
      busyLabel="Checking…"
      busy={busy}
      submitDisabled={incomplete}
      onSubmit={() => void submit()}
      onClose={onClose}
      testId={testId}
      fullScreenOnPhone
    >
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
        {/* Agents are GitHub-only (`agent_bundles.parse_repo`). A connector is read with the operator's App, not
            this person's connection, so a repo missing from their list says nothing there. */}
        <RepoUrlLookup
          id={`${idPrefix}-repo-url`}
          label="Repository URL"
          forges={GITHUB_ONLY}
          warnUnlisted={!connector}
          placeholder={connector ? undefined : 'https://github.com/acme/agents'}
          value={form.repo_url}
          onChange={v => setForm(f => ({ ...f, repo_url: v }))}
          disabled={busy || !repoEditable}
          required
          hint={!connector && (
            <span data-testid="agent-dialog-git">
              Reads the repo with your Git connection (
              <MuiLink component={RouterLink} to="/profile?tab=git-accounts">Profile › Git</MuiLink>).
            </span>
          )}
        />
        <FormTextField
          id={`${idPrefix}-subpath`}
          label="Sub-path"
          placeholder={connector ? 'connectors/report-sample' : 'agents/report-bot'}
          value={form.subpath}
          onChange={v => setForm(f => ({ ...f, subpath: v }))}
          disabled={busy}
          fullWidth
        />
        <FormTextField
          id={`${idPrefix}-ref`}
          label={connector ? 'Commit' : 'Ref'}
          placeholder={connector ? '40-character commit sha' : 'main'}
          value={form.ref}
          onChange={v => setForm(f => ({ ...f, ref: v }))}
          disabled={busy}
          required
          fullWidth
        />
        {!connector && (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }} data-testid="agent-env">
            <Typography variant="subtitle2" component="h3">Environment</Typography>
            <Typography variant="caption" sx={{ color: 'text.secondary', mt: -1 }}>
              Set on the agent's process, e.g. a connector's model. Not for secrets — use credentials.
            </Typography>
            {envRows.map((row, i) => (
              <Box key={i} sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
                <FormTextField
                  id={`agent-env-name-${i}`}
                  label="Name"
                  value={row.name}
                  onChange={v => setEnvRow(i, { name: v })}
                  error={envError(row)}
                  disabled={busy}
                  sx={{ flex: 1, minWidth: 0 }}
                />
                <FormTextField
                  id={`agent-env-value-${i}`}
                  label="Value"
                  value={row.value}
                  onChange={v => setEnvRow(i, { value: v })}
                  disabled={busy}
                  sx={{ flex: 1, minWidth: 0 }}
                />
                <Button
                  aria-label={`Remove ${row.name.trim() || 'variable'}`}
                  onClick={() => setEnvRows(rows => rows.filter((_, j) => j !== i))}
                  disabled={busy}
                  sx={{ minWidth: 0, mt: 3 }}
                >
                  ✕
                </Button>
              </Box>
            ))}
            <Box>
              <Button size="small" onClick={() => setEnvRows(rows => [...rows, { name: '', value: '' }])} disabled={busy}>
                Add variable
              </Button>
            </Box>
          </Box>
        )}
        {busy && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, color: 'text.secondary' }} data-testid={`${testId}-spinner`}>
            <CircularProgress size={16} />
            <Typography variant="body2">Fetching the repository and checking its manifest…</Typography>
          </Box>
        )}
        {errors.length > 0 && (
          <Banner severity="error" testId={`${testId}-errors`}>
            <SentenceList lines={errors} />
          </Banner>
        )}
      </Box>
    </FormDialog>
  )
}
