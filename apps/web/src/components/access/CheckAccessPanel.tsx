import { useMemo, useRef, useState, type JSX } from 'react'
import Autocomplete from '@mui/material/Autocomplete'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../api'
import { useApiRead } from '../../hooks/useApiRead'
import { absoluteTime, shortDay } from '../../lib/relativeTime'
import { Banner } from '../../ui/Banner'
import SectionCard from '../../ui/SectionCard'
import { StatusChip } from '../../ui/StatusChip'
import StatusDot from '../../ui/StatusDot'
import SubmitButton from '../../ui/SubmitButton'
import { checkAccess, listActions, type CatalogAction, type ChainNode, type CheckResult, type WireScope } from './api'
import ScopePicker, { SCOPE_TYPE_LABELS, useScopeOptions, type ScopeOption } from './ScopePicker'
import { NO_AUTOFILL_INPUT } from '../../ui/noAutofill'

// Check access: may this user do one thing to one resource? Pick the resource and the action (a route
// class of the catalog, by its human label — never its key) and `POST /api/access/check` answers with the
// deciding chain, or "no path" and the nearest missing link. Expired bindings are listed apart, marked
// expired: they confer nothing and are never drawn as the path.

/* The refused Check's face, by hand: the button stays live (`aria-disabled`, as `ui/BulkActionBar`'s verbs) so
   its reason is reachable. */
const REFUSED_SX = {
  color: 'text.disabled',
  bgcolor: 'action.disabledBackground',
  boxShadow: 'none',
  cursor: 'default',
  '&:hover': { bgcolor: 'action.disabledBackground', boxShadow: 'none' },
  '& .MuiTouchRipple-root': { display: 'none' },
} as const

const scopeText = (s: WireScope) => `${SCOPE_TYPE_LABELS[s.type] ?? s.type} ${s.name}`

/** The catalog label without its closing period, so it reads inside a sentence. */
const actionLabel = (a: { description: string }) => a.description.replace(/\.$/, '')

function stepText(node: ChainNode): { label: string; text: string } {
  switch (node.kind) {
    case 'user':
      return { label: 'User', text: node.name }
    case 'group':
      return { label: 'Member of group', text: node.name }
    case 'operator':
      return { label: 'Operator', text: `${node.name} passes every check` }
    case 'owner':
      return { label: 'Owner', text: `${node.name} owns this personal account` }
    case 'binding':
      return { label: 'Holds role', text: `${node.role} on ${scopeText(node.scope)}` }
    case 'inherited':
      return { label: 'Inherited from', text: scopeText(node.via) }
    case 'resource':
      return { label: 'Reaches', text: scopeText(node) }
  }
}

function Chain({ nodes, testId }: { nodes: ChainNode[]; testId: string }) {
  return (
    <Box component="ol" data-testid={testId} sx={{ listStyle: 'none', m: 0, p: 0 }}>
      {nodes.map((node, i) => {
        const { label, text } = stepText(node)
        const expired = node.kind === 'binding' && node.status === 'expired'
        const last = i === nodes.length - 1
        return (
          <Box
            component="li"
            key={i}
            data-testid="chain-step"
            data-kind={node.kind}
            data-status={node.kind === 'binding' ? node.status : undefined}
            sx={{ display: 'flex', gap: 1, minWidth: 0 }}
          >
            {/* The rail: a dot per step joined by a line, so the chain reads top to bottom at any width. */}
            <Box aria-hidden="true" sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', flex: 'none', width: 12 }}>
              <StatusDot size={10} color={expired ? 'text.disabled' : 'primary.main'} sx={{ mt: 0.75 }} />
              {!last && <Box sx={{ flex: 1, width: 2, bgcolor: 'divider', my: 0.25 }} />}
            </Box>
            <Box sx={{ minWidth: 0, pb: last ? 0 : 1 }}>
              <Typography variant="caption" sx={{ color: 'text.secondary' }} component="div">{label}</Typography>
              <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 0.75 }}>
                <Typography
                  variant="body2"
                  component="span"
                  sx={{ overflowWrap: 'anywhere', ...(expired && { color: 'text.disabled', textDecoration: 'line-through' }) }}
                >
                  {text}
                </Typography>
                {expired && <StatusChip label="Expired" tone="warning" />}
                {node.kind === 'binding' && node.expires_at && (
                  <Typography variant="caption" sx={{ color: 'text.secondary' }} component="span" title={absoluteTime(node.expires_at)}>
                    {expired ? 'on' : 'until'} {shortDay(node.expires_at)}
                  </Typography>
                )}
              </Box>
            </Box>
          </Box>
        )
      })}
    </Box>
  )
}

function Result({ result }: { result: CheckResult }) {
  return (
    <Stack spacing={1.5} data-testid="check-result" data-allowed={String(result.allowed)}>
      {result.allowed ? (
        <Banner severity="success">Allowed: {actionLabel({ description: result.action_label })}.</Banner>
      ) : (
        <Banner severity="warning">
          <Box component="span" sx={{ fontWeight: 600 }}>No path.</Box>{' '}
          <span data-testid="check-missing">{result.missing?.message ?? 'Nothing allows this.'}</span>
        </Banner>
      )}
      {result.chain.length > 0 && <Chain nodes={result.chain} testId="check-chain" />}
      {result.expired.length > 0 && (
        <Box>
          <Typography variant="body2" sx={{ fontWeight: 600, mb: 0.5 }}>
            Expired — {result.expired.length === 1 ? 'this binding confers' : 'these bindings confer'} nothing
          </Typography>
          <Stack spacing={1}>
            {result.expired.map((nodes, i) => <Chain key={i} nodes={nodes} testId="check-expired" />)}
          </Stack>
        </Box>
      )}
    </Stack>
  )
}

export default function CheckAccessPanel({ userId }: { userId: string }): JSX.Element {
  const { options, loading } = useScopeOptions()
  const actions = useApiRead(['check-access-actions'], listActions, 'Could not load the action list.')
  const [resource, setResource] = useState<ScopeOption | null>(null)
  const [action, setAction] = useState<CatalogAction | null>(null)
  const [result, setResult] = useState<CheckResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // Why Check cannot run yet; '' = it can. On `title` and the accessible name, as the bulk bar's reasons.
  const refused = !resource ? (action ? 'Pick a resource' : 'Pick a resource and an action')
    : !action ? 'Pick an action' : ''
  const actionOptions = useMemo(() => actions.data ?? [], [actions.data])
  // A check answered after its question changed must paint nothing (the `requestGenRef` pattern).
  const gen = useRef(0)
  const retire = () => { gen.current += 1; setBusy(false); setResult(null) }

  async function run() {
    if (!resource || !action) return
    const mine = ++gen.current
    setBusy(true)
    setError('')
    setResult(null)
    try {
      const answer = await checkAccess({
        user_id: userId,
        resource: { type: resource.type, id: resource.id },
        action: { feature: action.feature, action: action.action },
      })
      if (mine === gen.current) setResult(answer)
    } catch (err) {
      if (mine === gen.current) setError(apiDetail(err, 'Could not check access.'))
    } finally {
      if (mine === gen.current) setBusy(false)
    }
  }

  return (
    <SectionCard>
      <Stack spacing={2} component="form" onSubmit={e => { e.preventDefault(); void run() }}>
        {actions.error && <Banner severity="error">{actions.error}</Banner>}
        <ScopePicker
          idPrefix="check"
          options={options}
          loading={loading}
          value={resource}
          onChange={next => { setResource(next); retire() }}
        />
        <Autocomplete
          id="check-action"
          options={actionOptions}
          value={action}
          onChange={(_, next) => { setAction(next); retire() }}
          loading={actions.loading}
          isOptionEqualToValue={(a, b) => a.key === b.key}
          getOptionLabel={actionLabel}
          getOptionKey={a => a.key}
          renderInput={params => <TextField {...params} label="Action" slotProps={{ ...params.slotProps, htmlInput: { ...params.slotProps.htmlInput, ...NO_AUTOFILL_INPUT } }} />}
        />
        <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
          {/* Not `disabled` while a pick is missing: that would hide the reason. `run` refuses the press. */}
          <SubmitButton
            busy={busy}
            busyLabel="Checking…"
            aria-disabled={refused ? true : undefined}
            title={refused || undefined}
            aria-label={refused ? `Check — ${refused}` : undefined}
            sx={refused ? REFUSED_SX : undefined}
            testId="check-run"
          >
            Check
          </SubmitButton>
        </Box>
        {error && <Banner severity="error">{error}</Banner>}
        {result && <Result result={result} />}
      </Stack>
    </SectionCard>
  )
}
